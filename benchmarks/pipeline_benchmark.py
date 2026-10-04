"""GraphRAG Pipeline Benchmark — measures all 10 stages against the authoritative LadybugDB."""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

# Ensure sandbox_engine is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ladybug as lb

from sandbox_engine.config import FINGRAPH_DATA_DIR
from sandbox_engine.coldstart_extract import ColdStartExtractor
from sandbox_engine.coldstart_synthesis import ColdStartSynthesizer
from sandbox_engine.entity_resolver import ConceptRegistry
from sandbox_engine.parser import stable_id
from sandbox_engine.query_ui import KnowledgeGraph, ask_rag, route_query
from sandbox_engine.router import COMPANY_ALIASES, EntityRoute, resolve_company_name
from sandbox_engine.stitch import InMemoryOverlayGraph, stitch_coldstart_payload
from sandbox_engine.traversal import HybridGraphTraverser, format_provenance_ledger
from sandbox_engine.tier1_fetch import SECRuntimeFetcher
from sandbox_engine.tier1_clean import clean_and_truncate_section

# ─────────────────────────────────────────────────────────────────────────────
# Test Queries covering all routes and scenarios
# ─────────────────────────────────────────────────────────────────────────────

QUERIES = [
    # KNOWN companies (exist in backbone)
    {"query": "What are Apple's net sales for FY2025?", "route": "KNOWN", "ticker": "AAPL"},
    {"query": "Microsoft Azure revenue growth YoY", "route": "KNOWN", "ticker": "MSFT"},
    {"query": "NVIDIA H100 supply chain dependencies", "route": "KNOWN", "ticker": "NVDA"},
    # COLD_START companies (not in backbone)
    {"query": "Analyze supply chain risks for $PLTR", "route": "COLD_START", "ticker": "PLTR"},
    {"query": "Snowflake competitive moat analysis", "route": "COLD_START", "ticker": "SNOW"},
    # AMBIGUOUS queries
    {"query": "What is the current net sales trend?", "route": "AMBIGUOUS", "ticker": None},
    {"query": "How to calculate operating margin?", "route": "AMBIGUOUS", "ticker": None},
]

# Cold-start specific test queries (require full pipeline)
COLDSTART_QUERIES = [
    "Analyze supply chain dependencies for Palantir Technologies",
    "What are the key suppliers and risks for Snowflake?",
]

# ─────────────────────────────────────────────────────────────────────────────
# Benchmark Data Structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StageMeasurement:
    """Single measurement of a pipeline stage."""
    stage_name: str
    latency_ms: float
    success: bool
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QueryResult:
    """Result of running a full query through the pipeline."""
    query: str
    expected_route: str
    actual_route: str | None
    expected_ticker: str | None
    actual_ticker: str | None
    stage_measurements: list[StageMeasurement]
    total_latency_ms: float
    success: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "stage_measurements": [m.to_dict() for m in self.stage_measurements],
        }


@dataclass
class AggregateStats:
    """Aggregated statistics across multiple runs."""
    stage: str
    count: int
    mean_ms: float
    median_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    min_ms: float
    max_ms: float
    success_rate: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Benchmark Harness
# ─────────────────────────────────────────────────────────────────────────────

class PipelineBenchmark:
    """Runs the GraphRAG pipeline against the authoritative LadybugDB and measures each stage."""

    def __init__(self, db_path: str | Path, iterations: int = 3, warmup: int = 1):
        self.db_path = Path(db_path)
        self.iterations = iterations
        self.warmup = warmup
        self.kg: KnowledgeGraph | None = None
        self.results: list[QueryResult] = []

    def setup(self) -> None:
        """Initialize the KnowledgeGraph connection to the authoritative DB."""
        print(f"[SETUP] Connecting to LadybugDB: {self.db_path}")
        self.kg = KnowledgeGraph(str(self.db_path))
        # Verify connection
        try:
            result = self.kg.execute("MATCH (c:Company) RETURN count(c) AS n")
            count = result[0][0] if result else 0
            print(f"[SETUP] Database has {count} Company nodes")
        except Exception as e:
            print(f"[SETUP] Warning: Could not verify DB: {e}")

    def teardown(self) -> None:
        if self.kg:
            self.kg.close()

    # ── Stage Measurement Helpers ──────────────────────────────────────────

    def _measure(self, stage_name: str, fn: Callable[[], Any]) -> StageMeasurement:
        """Time a single stage execution."""
        start = time.perf_counter()
        try:
            result = fn()
            latency_ms = (time.perf_counter() - start) * 1000
            return StageMeasurement(
                stage_name=stage_name,
                latency_ms=round(latency_ms, 2),
                success=True,
                details={"result_type": type(result).__name__},
            )
        except Exception as e:
            latency_ms = (time.perf_counter() - start) * 1000
            return StageMeasurement(
                stage_name=stage_name,
                latency_ms=round(latency_ms, 2),
                success=False,
                details={"error": str(e), "error_type": type(e).__name__},
            )

    # ── Stage 1: Graph Node Lookup ─────────────────────────────────────────

    def measure_node_lookup(self, ticker: str | None, entity_name: str | None) -> StageMeasurement:
        """Measure entity resolution and node lookup."""
        if not ticker and not entity_name:
            return StageMeasurement("node_lookup", 0.0, False, {"error": "no entity to resolve"})

        def _lookup():
            results = {}
            if ticker:
                # Check if company exists in backbone
                exists = self.kg.has_company(ticker.upper()) if hasattr(self.kg, "has_company") else False
                results["ticker"] = ticker
                results["exists_in_backbone"] = exists
            if entity_name:
                resolved = resolve_company_name(entity_name)
                results["resolved_ticker"] = resolved[0]
                results["resolved_name"] = resolved[1]
            return results

        return self._measure("node_lookup", _lookup)

    # ── Stage 2: Edge Traversal (Backbone) ─────────────────────────────────

    def measure_edge_traversal(self, ticker: str, hops: int = 2) -> StageMeasurement:
        """Measure backbone edge traversal from a seed node."""
        def _traverse():
            # LadybugDB returns relationship as dict with _LABEL key
            cypher = """
            MATCH (a:Company {ticker: $ticker})-[r]-(b)
            RETURN a.ticker, r, b.ticker, b.name, labels(b)
            LIMIT 100
            """
            rows = self.kg.execute(cypher, {"ticker": ticker.upper()})
            edges = []
            for row in rows:
                if len(row) >= 3 and isinstance(row[1], dict):
                    rel_dict = row[1]
                    rel_type = rel_dict.get("_LABEL", "UNKNOWN")
                    edges.append({
                        "source": row[0],
                        "relation": rel_type,
                        "target": row[2],
                        "target_name": row[3] if len(row) > 3 else "",
                        "target_labels": row[4] if len(row) > 4 else [],
                    })
            return {"edges_found": len(edges), "edges": edges[:10]}

        return self._measure(f"edge_traversal_{hops}hop", _traverse)

    # ── Stage 3: 1-hop Retrieval ──────────────────────────────────────────

    def measure_1hop_retrieval(self, ticker: str) -> StageMeasurement:
        """Measure 1-hop retrieval from seed."""
        def _retrieve():
            cypher = """
            MATCH (a:Company {ticker: $ticker})-[r]-(b)
            RETURN a.ticker, r, b.ticker, b.name, labels(b), properties(r)
            LIMIT 50
            """
            rows = self.kg.execute(cypher, {"ticker": ticker.upper()})
            neighbors = []
            for row in rows:
                if len(row) >= 3 and isinstance(row[1], dict):
                    rel_dict = row[1]
                    rel_type = rel_dict.get("_LABEL", "UNKNOWN")
                    neighbors.append({
                        "source": row[0],
                        "relation": rel_type,
                        "target": row[2],
                        "target_name": row[3] if len(row) > 3 else "",
                        "target_labels": row[4] if len(row) > 4 else [],
                        "properties": row[5] if len(row) > 5 else {},
                    })
            return {"neighbors": len(neighbors), "sample": neighbors[:5]}

        return self._measure("1hop_retrieval", _retrieve)

    # ── Stage 4: 2-hop Retrieval ──────────────────────────────────────────

    def measure_2hop_retrieval(self, ticker: str) -> StageMeasurement:
        """Measure 2-hop retrieval from seed."""
        def _retrieve():
            cypher = """
            MATCH (a:Company {ticker: $ticker})-[r1]-(m)-[r2]-(b)
            WHERE m <> a AND b <> a AND b <> m
            RETURN a.ticker, r1, m.ticker, m.name, r2, b.ticker, b.name, labels(b)
            LIMIT 100
            """
            rows = self.kg.execute(cypher, {"ticker": ticker.upper()})
            paths = []
            for row in rows:
                if len(row) >= 7 and isinstance(row[1], dict) and isinstance(row[4], dict):
                    rel1 = row[1].get("_LABEL", "UNKNOWN")
                    rel2 = row[4].get("_LABEL", "UNKNOWN")
                    paths.append({
                        "hop1": {"source": row[0], "relation": rel1, "target": row[2], "target_name": row[3]},
                        "hop2": {"source": row[2], "relation": rel2, "target": row[5], "target_name": row[6], "target_labels": row[7] if len(row) > 7 else []},
                    })
            return {"paths_found": len(paths), "sample": paths[:5]}

        return self._measure("2hop_retrieval", _retrieve)

    # ── Stage 5: Multi-hop Retrieval (3+) ──────────────────────────────────

    def measure_multihop_retrieval(self, ticker: str, max_hops: int = 3) -> StageMeasurement:
        """Measure multi-hop retrieval (3+ hops)."""
        def _retrieve():
            # Use a simpler approach: just do 3-hop with fixed pattern
            cypher = """
            MATCH (a:Company {ticker: $ticker})-[r1]-(m1)-[r2]-(m2)-[r3]-(b)
            WHERE m1 <> a AND m2 <> a AND m2 <> m1 AND b <> a AND b <> m1 AND b <> m2
            RETURN a.ticker, r1, m1.ticker, m1.name, r2, m2.ticker, m2.name, r3, b.ticker, b.name, labels(b)
            LIMIT 100
            """
            rows = self.kg.execute(cypher, {"ticker": ticker.upper()})
            count = 0
            for row in rows:
                for i in [1, 4, 7]:  # positions of r1, r2, r3
                    if i < len(row) and isinstance(row[i], dict):
                        count += 1
            return {"paths_found": len(rows), "relationships": count}

        return self._measure(f"{max_hops}hop_retrieval", _retrieve)

    # ── Stage 6: Metric/Entity Resolution ──────────────────────────────────

    def measure_entity_resolution(self, entity_names: list[str]) -> StageMeasurement:
        """Measure ConceptRegistry entity resolution."""
        def _resolve():
            registry = ConceptRegistry(stable_id)
            resolutions = []
            for name in entity_names:
                res = registry.register("Company", name)
                resolutions.append({
                    "input": name,
                    "canonical_id": res.canonical_id,
                    "decision": res.decision,
                    "evidence": res.evidence,
                })
            return {"resolved": len(resolutions), "details": resolutions}

        return self._measure("entity_resolution", _resolve)

    # ── Stage 7: Known-Company Query (Full KNOWN Route) ────────────────────

    def measure_known_query(self, query: str, ticker: str) -> list[StageMeasurement]:
        """Run full KNOWN route query and measure stages."""
        measurements = []

        # Routing
        measurements.append(self._measure("routing", lambda: route_query(query, self.kg)))

        # Full ask_rag (includes traversal, evidence building, LLM synthesis)
        def _ask():
            return ask_rag(self.kg, query)

        measurements.append(self._measure("full_known_pipeline", _ask))
        return measurements

    # ── Stage 8: Cold-Start Query (Full COLD_START Route) ──────────────────

    def measure_coldstart_query(self, query: str, ticker: str) -> list[StageMeasurement]:
        """Run full COLD_START pipeline and measure each stage (using mocks for speed)."""
        measurements = []

        # Routing
        measurements.append(self._measure("routing", lambda: route_query(query, self.kg)))

        # Fetching (SEC EDGAR) - use mock to avoid network calls
        def _fetch():
            # Mock HTML that clean_and_truncate_section can process
            mock_html = """
            <html><body>
            <div>Item 1. Business</div>
            <p>We source semiconductors from Taiwan Semiconductor Manufacturing Company.</p>
            <p>Our key suppliers include Samsung Electronics and Qualcomm.</p>
            <p>We compete with other AI companies in the market.</p>
            <div>Item 1A. Risk Factors</div>
            <p>Geopolitical risks in Taiwan could disrupt our supply chain.</p>
            </body></html>
            """
            cleaned = clean_and_truncate_section(mock_html, form_type="10-K", max_tokens=6000)
            return {"html_len": len(mock_html), "cleaned_len": len(cleaned), "cleaned_text": cleaned[:200]}

        measurements.append(self._measure("fetching", _fetch))

        # Extraction
        def _extract():
            extractor = ColdStartExtractor(provider="mock")
            mock_html = "<html><body>Item 1. Business. We source from TSMC.</body></html>"
            cleaned = clean_and_truncate_section(mock_html, form_type="10-K", max_tokens=6000)
            payload = extractor.extract(cleaned, ticker)
            return {"entities": len(payload.entities), "relationships": len(payload.relationships), "status": payload.metadata.get("status")}

        measurements.append(self._measure("extraction", _extract))

        # Stitching
        def _stitch():
            extractor = ColdStartExtractor(provider="mock")
            mock_html = "<html><body>Item 1. Business. We source from TSMC.</body></html>"
            cleaned = clean_and_truncate_section(mock_html, form_type="10-K", max_tokens=6000)
            payload = extractor.extract(cleaned, ticker)
            overlay = InMemoryOverlayGraph(kg_connection=self.kg)
            result = stitch_coldstart_payload(overlay, payload, target_ticker=ticker)
            return {"new_nodes": result["new_nodes"], "stitched_edges": result["stitched_backbone_edges"], "ephemeral_edges": result["ephemeral_edges"]}

        measurements.append(self._measure("stitching", _stitch))

        # Traversal (2-hop)
        def _traverse():
            extractor = ColdStartExtractor(provider="mock")
            mock_html = "<html><body>Item 1. Business. We source from TSMC.</body></html>"
            cleaned = clean_and_truncate_section(mock_html, form_type="10-K", max_tokens=6000)
            payload = extractor.extract(cleaned, ticker)
            overlay = InMemoryOverlayGraph(kg_connection=self.kg)
            stitch_coldstart_payload(overlay, payload, target_ticker=ticker)
            traverser = HybridGraphTraverser(overlay)
            subgraph = traverser.traverse_neighborhood(ticker, max_hops=2)
            return {"nodes": len(subgraph["nodes"]), "paths": len(subgraph["paths"])}

        measurements.append(self._measure("traversal_2hop", _traverse))

        # Synthesis
        def _synthesize():
            extractor = ColdStartExtractor(provider="mock")
            mock_html = "<html><body>Item 1. Business. We source from TSMC.</body></html>"
            cleaned = clean_and_truncate_section(mock_html, form_type="10-K", max_tokens=6000)
            payload = extractor.extract(cleaned, ticker)
            overlay = InMemoryOverlayGraph(kg_connection=self.kg)
            stitch_coldstart_payload(overlay, payload, target_ticker=ticker)
            traverser = HybridGraphTraverser(overlay)
            subgraph = traverser.traverse_neighborhood(ticker, max_hops=2)
            synthesizer = ColdStartSynthesizer()
            context = {
                "target_ticker": ticker,
                "query": query,
                "paths": subgraph["paths"],
                "filing_text": cleaned,
            }
            tokens = list(synthesizer.stream_synthesis(context))
            return {"tokens": len(tokens), "answer_len": len("".join(tokens))}

        measurements.append(self._measure("synthesis", _synthesize))

        return measurements

    # ── Stage 9: Graph+Vector Retrieval (Name Search) ──────────────────────

    def measure_graph_vector_retrieval(self, search_term: str) -> StageMeasurement:
        """Measure graph-based entity search (approximates vector retrieval)."""
        def _search():
            # Use the graph's lookup_by_name which does fuzzy matching
            # This is the closest we have to vector retrieval in the current stack
            results = self.kg.execute(
                "MATCH (c:Company) WHERE toLower(c.name) CONTAINS toLower($term) OR toLower(c.ticker) CONTAINS toLower($term) RETURN c.ticker, c.name LIMIT 20",
                {"term": search_term}
            )
            matches = []
            for row in results:
                if len(row) >= 2:
                    matches.append({"ticker": row[0], "name": row[1]})
            return {"matches": len(matches), "sample": matches[:5]}

        return self._measure("graph_vector_retrieval", _search)

    # ── Stage 10: LLM Synthesis ────────────────────────────────────────────

    def measure_llm_synthesis(self, query: str, context_size: int = 1000) -> StageMeasurement:
        """Measure LLM synthesis stage (using mock to avoid API calls)."""
        def _synthesize():
            synthesizer = ColdStartSynthesizer()
            # Create mock context
            paths = [[{"source": "TARGET", "relation": "SOURCES_FROM", "target": "SUPPLIER", "evidence_quote": "Sources from supplier"}]]
            context = {
                "target_ticker": "TEST",
                "query": query,
                "paths": paths,
                "filing_text": "Sample filing text " * 100,
            }
            tokens = list(synthesizer.stream_synthesis(context))
            return {"tokens": len(tokens), "total_chars": len("".join(tokens))}

        return self._measure("llm_synthesis", _synthesize)

    # ── Run Full Query Benchmark ───────────────────────────────────────────

    def run_query_benchmark(self, query_spec: dict[str, Any]) -> QueryResult:
        """Run a single query through the appropriate pipeline and measure all stages."""
        query = query_spec["query"]
        expected_route = query_spec.get("route")
        expected_ticker = query_spec.get("ticker")

        print(f"\n[QUERY] {query}")
        print(f"  Expected: route={expected_route}, ticker={expected_ticker}")

        stage_measurements = []
        actual_route = None
        actual_ticker = None
        error = None
        success = True

        try:
            # Route first to determine path
            routing_result = route_query(query, self.kg)
            actual_route = routing_result.route.value
            actual_ticker = routing_result.ticker

            # Measure routing
            stage_measurements.append(self._measure("routing", lambda: routing_result))

            if actual_route == "COLD_START" and actual_ticker:
                # Full cold-start pipeline
                stage_measurements.extend(self.measure_coldstart_query(query, actual_ticker))
            elif actual_route == "KNOWN" and actual_ticker:
                # Full known pipeline
                stage_measurements.extend(self.measure_known_query(query, actual_ticker))
            else:
                # AMBIGUOUS - just measure routing
                pass

        except Exception as e:
            error = str(e)
            success = False
            print(f"  ERROR: {e}")

        total_latency = sum(m.latency_ms for m in stage_measurements)

        result = QueryResult(
            query=query,
            expected_route=expected_route,
            actual_route=actual_route,
            expected_ticker=expected_ticker,
            actual_ticker=actual_ticker,
            stage_measurements=stage_measurements,
            total_latency_ms=round(total_latency, 2),
            success=success,
            error=error,
        )

        print(f"  Result: route={actual_route}, ticker={actual_ticker}, total={total_latency:.1f}ms, success={success}")
        for m in stage_measurements:
            status = "OK" if m.success else "FAIL"
            print(f"    {m.stage_name}: {m.latency_ms:.1f}ms [{status}]")

        return result

    def run_stage_benchmarks(self) -> dict[str, list[StageMeasurement]]:
        """Run isolated stage benchmarks for detailed analysis."""
        print("\n[STAGE BENCHMARKS] Running isolated stage measurements...")

        # Get a known ticker from the DB
        test_ticker = "AAPL"
        stage_results: dict[str, list[StageMeasurement]] = {}

        # Stage 1: Node Lookup
        print("  Stage 1: Node Lookup")
        stage_results["node_lookup"] = [
            self.measure_node_lookup("AAPL", "Apple Inc.") for _ in range(self.iterations)
        ]

        # Stage 2: Edge Traversal (backbone)
        print("  Stage 2: Edge Traversal (backbone)")
        stage_results["edge_traversal"] = [
            self.measure_edge_traversal("AAPL", 2) for _ in range(self.iterations)
        ]

        # Stage 3: 1-hop Retrieval
        print("  Stage 3: 1-hop Retrieval")
        stage_results["1hop_retrieval"] = [
            self.measure_1hop_retrieval("AAPL") for _ in range(self.iterations)
        ]

        # Stage 4: 2-hop Retrieval
        print("  Stage 4: 2-hop Retrieval")
        stage_results["2hop_retrieval"] = [
            self.measure_2hop_retrieval("AAPL") for _ in range(self.iterations)
        ]

        # Stage 5: Multi-hop (3-hop)
        print("  Stage 5: 3-hop Retrieval")
        stage_results["3hop_retrieval"] = [
            self.measure_multihop_retrieval("AAPL", 3) for _ in range(self.iterations)
        ]

        # Stage 6: Entity Resolution
        print("  Stage 6: Entity Resolution")
        test_entities = list(COMPANY_ALIASES.keys())[:10]
        stage_results["entity_resolution"] = [
            self.measure_entity_resolution(test_entities) for _ in range(self.iterations)
        ]

        # Stage 9: Graph+Vector Retrieval
        print("  Stage 9: Graph+Vector Retrieval (name search)")
        stage_results["graph_vector_retrieval"] = [
            self.measure_graph_vector_retrieval("apple") for _ in range(self.iterations)
        ]

        # Stage 10: LLM Synthesis
        print("  Stage 10: LLM Synthesis (mock)")
        stage_results["llm_synthesis"] = [
            self.measure_llm_synthesis("Analyze supply chain", 1000) for _ in range(self.iterations)
        ]

        return stage_results

    def run_full_benchmarks(self) -> list[QueryResult]:
        """Run full query benchmarks across all test queries."""
        print(f"\n[FULL BENCHMARK] Running {len(QUERIES)} queries x {self.iterations} iterations...")

        # Warmup
        for _ in range(self.warmup):
            for q in QUERIES[:3]:
                self.run_query_benchmark(q)

        # Actual runs
        all_results = []
        for iteration in range(self.iterations):
            print(f"\n--- Iteration {iteration + 1}/{self.iterations} ---")
            for q in QUERIES:
                result = self.run_query_benchmark(q)
                all_results.append(result)

        self.results = all_results
        return all_results

    # ── Aggregation & Reporting ────────────────────────────────────────────

    def aggregate_stage_results(self, stage_results: dict[str, list[StageMeasurement]]) -> list[AggregateStats]:
        """Compute aggregate statistics for each stage."""
        aggregates = []
        for stage_name, measurements in stage_results.items():
            successful = [m for m in measurements if m.success]
            latencies = [m.latency_ms for m in successful]

            if latencies:
                sorted_lat = sorted(latencies)
                aggregates.append(AggregateStats(
                    stage=stage_name,
                    count=len(measurements),
                    mean_ms=round(statistics.mean(latencies), 2),
                    median_ms=round(statistics.median(latencies), 2),
                    p50_ms=round(sorted_lat[len(sorted_lat) // 2], 2),
                    p95_ms=round(sorted_lat[int(len(sorted_lat) * 0.95)], 2),
                    p99_ms=round(sorted_lat[int(len(sorted_lat) * 0.99)], 2) if len(sorted_lat) > 1 else sorted_lat[0],
                    min_ms=round(min(latencies), 2),
                    max_ms=round(max(latencies), 2),
                    success_rate=round(len(successful) / len(measurements) * 100, 1),
                ))
            else:
                aggregates.append(AggregateStats(
                    stage=stage_name,
                    count=len(measurements),
                    mean_ms=0, median_ms=0, p50_ms=0, p95_ms=0, p99_ms=0, min_ms=0, max_ms=0,
                    success_rate=0.0,
                ))
        return aggregates

    def aggregate_query_results(self) -> dict[str, AggregateStats]:
        """Aggregate results by query route type."""
        by_route: dict[str, list[float]] = {}
        for result in self.results:
            route = result.actual_route or "UNKNOWN"
            if route not in by_route:
                by_route[route] = []
            by_route[route].append(result.total_latency_ms)

        aggregates = {}
        for route, latencies in by_route.items():
            if latencies:
                sorted_lat = sorted(latencies)
                aggregates[route] = AggregateStats(
                    stage=f"full_query_{route}",
                    count=len(latencies),
                    mean_ms=round(statistics.mean(latencies), 2),
                    median_ms=round(statistics.median(latencies), 2),
                    p50_ms=round(sorted_lat[len(sorted_lat) // 2], 2),
                    p95_ms=round(sorted_lat[int(len(sorted_lat) * 0.95)], 2),
                    p99_ms=round(sorted_lat[int(len(sorted_lat) * 0.99)], 2) if len(sorted_lat) > 1 else sorted_lat[0],
                    min_ms=round(min(latencies), 2),
                    max_ms=round(max(latencies), 2),
                    success_rate=round(sum(1 for r in self.results if r.actual_route == route and r.success) / len([r for r in self.results if r.actual_route == route]) * 100, 1),
                )
        return aggregates

    def print_summary(self, stage_aggregates: list[AggregateStats], query_aggregates: dict[str, AggregateStats]) -> None:
        """Print formatted benchmark summary."""
        print("\n" + "=" * 80)
        print("GRAPHRAG PIPELINE BENCHMARK SUMMARY")
        print("=" * 80)

        print("\n📊 STAGE-LEVEL METRICS (isolated measurements)")
        print("-" * 80)
        print(f"{'Stage':<30} {'Count':>5} {'Mean(ms)':>10} {'P50(ms)':>10} {'P95(ms)':>10} {'P99(ms)':>10} {'Success%':>8}")
        print("-" * 80)
        for agg in stage_aggregates:
            print(f"{agg.stage:<30} {agg.count:>5} {agg.mean_ms:>10.1f} {agg.p50_ms:>10.1f} {agg.p95_ms:>10.1f} {agg.p99_ms:>10.1f} {agg.success_rate:>7.1f}%")

        print("\n📈 FULL QUERY LATENCY BY ROUTE")
        print("-" * 80)
        print(f"{'Route':<20} {'Count':>5} {'Mean(ms)':>10} {'P50(ms)':>10} {'P95(ms)':>10} {'P99(ms)':>10} {'Success%':>8}")
        print("-" * 80)
        for route, agg in query_aggregates.items():
            print(f"{route:<20} {agg.count:>5} {agg.mean_ms:>10.1f} {agg.p50_ms:>10.1f} {agg.p95_ms:>10.1f} {agg.p99_ms:>10.1f} {agg.success_rate:>7.1f}%")

        # Identify bottlenecks
        print("\n🔍 BOTTLENECK ANALYSIS")
        print("-" * 80)
        if stage_aggregates:
            slowest = max(stage_aggregates, key=lambda a: a.mean_ms)
            print(f"Slowest stage (mean): {slowest.stage} at {slowest.mean_ms:.1f}ms")
            slowest_p99 = max(stage_aggregates, key=lambda a: a.p99_ms)
            print(f"Highest tail latency (P99): {slowest_p99.stage} at {slowest_p99.p99_ms:.1f}ms")
            lowest_success = min(stage_aggregates, key=lambda a: a.success_rate)
            if lowest_success.success_rate < 100:
                print(f"Lowest success rate: {lowest_success.stage} at {lowest_success.success_rate:.1f}%")

        # Route correctness
        print("\n✅ ROUTING CORRECTNESS")
        print("-" * 80)
        correct = sum(1 for r in self.results if r.expected_route == r.actual_route and r.expected_ticker == r.actual_ticker)
        total = len(self.results)
        print(f"Correct route+ticker: {correct}/{total} ({correct/total*100:.1f}%)")

        for r in self.results:
            if r.expected_route != r.actual_route or r.expected_ticker != r.actual_ticker:
                print(f"  MISMATCH: '{r.query[:50]}...' expected ({r.expected_route}, {r.expected_ticker}) got ({r.actual_route}, {r.actual_ticker})")

    def export_json(self, stage_aggregates: list[AggregateStats], query_aggregates: dict[str, AggregateStats], output_path: str | Path) -> None:
        """Export full benchmark results to JSON."""
        data = {
            "metadata": {
                "db_path": str(self.db_path),
                "iterations": self.iterations,
                "warmup": self.warmup,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            },
            "stage_aggregates": [a.to_dict() for a in stage_aggregates],
            "query_aggregates": {k: v.to_dict() for k, v in query_aggregates.items()},
            "query_results": [r.to_dict() for r in self.results],
        }
        with open(output_path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"\n[EXPORT] Results written to {output_path}")


# ─────────────────────────────────────────────────────────────────────────────
# Main Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    import argparse

    parser = argparse.ArgumentParser(description="GraphRAG Pipeline Benchmark")
    parser.add_argument("--db", default=str(Path(__file__).resolve().parents[1] / "sandbox_engine" / "_run" / "sandbox.lbug"), help="Path to LadybugDB")
    parser.add_argument("--iterations", type=int, default=3, help="Iterations per query")
    parser.add_argument("--warmup", type=int, default=1, help="Warmup iterations")
    parser.add_argument("--output", default="benchmark_results.json", help="Output JSON path")
    parser.add_argument("--stages-only", action="store_true", help="Run only isolated stage benchmarks")
    parser.add_argument("--queries-only", action="store_true", help="Run only full query benchmarks")

    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: Database not found at {db_path}")
        sys.exit(1)

    benchmark = PipelineBenchmark(db_path, iterations=args.iterations, warmup=args.warmup)

    try:
        benchmark.setup()

        if args.stages_only:
            stage_results = benchmark.run_stage_benchmarks()
            stage_aggregates = benchmark.aggregate_stage_results(stage_results)
            benchmark.print_summary(stage_aggregates, {})
            benchmark.export_json(stage_aggregates, {}, args.output)
        elif args.queries_only:
            benchmark.run_full_benchmarks()
            query_aggregates = benchmark.aggregate_query_results()
            benchmark.print_summary([], query_aggregates)
            benchmark.export_json([], query_aggregates, args.output)
        else:
            # Run both
            stage_results = benchmark.run_stage_benchmarks()
            benchmark.run_full_benchmarks()
            stage_aggregates = benchmark.aggregate_stage_results(stage_results)
            query_aggregates = benchmark.aggregate_query_results()
            benchmark.print_summary(stage_aggregates, query_aggregates)
            benchmark.export_json(stage_aggregates, query_aggregates, args.output)

    finally:
        benchmark.teardown()


if __name__ == "__main__":
    main()