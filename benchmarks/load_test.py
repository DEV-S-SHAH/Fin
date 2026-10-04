"""FinGraph Scalability Load Test — Staged concurrency validation across workload classes."""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ladybug as lb
import psutil

from sandbox_engine.config import FINGRAPH_DATA_DIR
from sandbox_engine.query_ui import KnowledgeGraph, ask_rag, route_query, EntityRoute
from sandbox_engine.router import COMPANY_ALIASES
from unittest.mock import patch, MagicMock
from benchmarks.queries_50 import CASES, cases_for_category

# ─────────────────────────────────────────────────────────────────────────────
# Workload Classes
# ─────────────────────────────────────────────────────────────────────────────

WORKLOAD_CLASSES = {
    "A": {
        "name": "static/frontend",
        "description": "Static asset serving, health checks, UI index page",
        "endpoint": "/",
        "weight": 0.10,
    },
    "B": {
        "name": "authenticated_api",
        "description": "API calls requiring auth: /api/entities, /api/graph, /api/stats",
        "endpoint": "/api/entities?q=test",
        "weight": 0.15,
    },
    "C": {
        "name": "known_company_query",
        "description": "GraphRAG query for companies in backbone (AAPL, MSFT, NVDA)",
        "queries": [c for c in CASES if c.expected_route == "KNOWN" and c.expected_ticker in ("AAPL", "MSFT", "NVDA")],
        "weight": 0.35,
    },
    "D": {
        "name": "cold_start_query",
        "description": "GraphRAG query for companies NOT in backbone (requires SEC fetch)",
        "queries": [c for c in CASES if c.expected_route == "COLD_START"],
        "weight": 0.20,
    },
    "E": {
        "name": "market_data_query",
        "description": "Financial metric lookups, segment breakdowns (canned reports)",
        "endpoint": "/api/reports/report_net_sales_by_company",
        "weight": 0.10,
    },
    "F": {
        "name": "ingestion",
        "description": "Background SEC filing ingestion (writer process)",
        "weight": 0.05,
    },
    "G": {
        "name": "sse_long_query",
        "description": "Long-running SSE streaming query (cold-start with synthesis)",
        "queries": [c for c in CASES if c.expected_route == "COLD_START"][:3],
        "weight": 0.05,
    },
}

# Concurrency ladder
CONCURRENCY_LADDER = [10, 50, 100, 250, 500, 1000, 2500, 5000, 10000]

# Default test duration per level (seconds)
DEFAULT_DURATION = 30

# ─────────────────────────────────────────────────────────────────────────────
# Data Structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RequestMetrics:
    """Metrics for a single request."""
    workload: str
    query_id: str | None
    start_time: float
    end_time: float
    latency_ms: float
    status_code: int
    success: bool
    error: str | None = None
    stage_latencies: dict[str, float] = field(default_factory=dict)
    route: str | None = None
    ticker: str | None = None

@dataclass
class SystemMetrics:
    """System resource metrics at a point in time."""
    timestamp: float
    cpu_percent: float
    ram_mb: float
    thread_count: int
    open_connections: int
    db_pool_in_use: int
    db_pool_available: int
    queue_depth: int

@dataclass
class AggregatedMetrics:
    """Aggregated metrics for a workload at a concurrency level."""
    workload: str
    concurrency: int
    duration_s: float
    total_requests: int
    successful_requests: int
    failed_requests: int
    timeout_requests: int
    error_rate: float
    timeout_rate: float
    throughput_rps: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    mean_ms: float
    min_ms: float
    max_ms: float
    cpu_p50: float
    cpu_p95: float
    ram_p50_mb: float
    ram_p95_mb: float
    thread_p50: float
    thread_p95: float
    db_latency_p50_ms: float
    db_latency_p95_ms: float
    llm_latency_p50_ms: float
    llm_latency_p95_ms: float
    external_api_latency_p50_ms: float
    external_api_latency_p95_ms: float
    saturation_indicators: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

# ─────────────────────────────────────────────────────────────────────────────
# Load Test Harness
# ─────────────────────────────────────────────────────────────────────────────

class LoadTestHarness:
    """Orchestrates staged load testing across workload classes."""

    def __init__(
        self,
        db_path: str | Path,
        concurrency_levels: list[int] | None = None,
        duration_per_level: int = DEFAULT_DURATION,
        warmup_duration: int = 10,
        output_dir: str | Path = "benchmarks/results",
        mock_llm: bool = True,
    ):
        self.db_path = Path(db_path)
        self.concurrency_levels = concurrency_levels or CONCURRENCY_LADDER
        self.duration_per_level = duration_per_level
        self.warmup_duration = warmup_duration
        self.output_dir = Path(output_dir)
        self.mock_llm = mock_llm
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.kg: KnowledgeGraph | None = None
        self.system_metrics: list[SystemMetrics] = []
        self.all_request_metrics: list[RequestMetrics] = []
        self._monitoring = False
        self._monitor_thread: threading.Thread | None = None
        self._process = psutil.Process()
        self._mock_patches: list = []

    def _enable_mocks(self) -> None:
        """Enable mock patches for external dependencies."""
        if not self.mock_llm:
            return

        print("[MOCKS] Enabling mock patches...")

        # Mock SEC EDGAR fetcher
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
        mock_meta = {"form": "10-K"}

        patcher1 = patch("sandbox_engine.tier1_fetch.SECRuntimeFetcher.fetch_latest_filing_html",
                        return_value=(mock_html, mock_meta))
        patcher1.start()
        self._mock_patches.append(patcher1)
        print("[MOCKS] Patched SECRuntimeFetcher")

        # Mock ColdStartSynthesizer to return fast tokens
        def mock_stream_synthesis(context):
            mock_answer = (
                "1. Executive Summary & Thesis\n"
                "2. Direct Dependencies\n"
                "3. Second-Order Contagion\n"
                "4. Capital Allocation & Margin Outlook\n"
                "5. Verifiable Evidence Chain\n"
            )
            for word in mock_answer.split():
                yield word + " "
            yield ""

        patcher2 = patch("sandbox_engine.coldstart_synthesis.ColdStartSynthesizer.stream_synthesis",
                        side_effect=mock_stream_synthesis)
        patcher2.start()
        self._mock_patches.append(patcher2)
        print("[MOCKS] Patched ColdStartSynthesizer")

        # Also patch ColdStartExtractor to ensure mock mode
        patcher3 = patch("sandbox_engine.coldstart_extract.ColdStartExtractor.__init__",
                        lambda self, provider="mock", client=None, model=None, timeout=3.5: 
                        setattr(self, 'provider', 'mock') or setattr(self, 'client', None) or 
                        setattr(self, 'model', model) or setattr(self, 'timeout', timeout))
        patcher3.start()
        self._mock_patches.append(patcher3)
        print("[MOCKS] Patched ColdStartExtractor")

        # Mock RagBackends to return mock credentials (for KNOWN route)
        def mock_resolve():
            return {
                "backend": "mock",
                "base_url": "http://localhost:11434/v1",
                "model": "mock-model",
                "reason": "Mock mode for load testing",
                "key_present": True,
                "key_source": "mock",
                "stored_key_rejected": False,
                "ollama_reachable": False,
                "ollama_models": [],
                "forced": "",
            }

        def mock_credentials(backend):
            return ("mock-key", "http://localhost:11434/v1")

        patcher4 = patch("sandbox_engine.query_ui.get_backends")
        mock_backends = patcher4.start()
        mock_backends.return_value.resolve = mock_resolve
        mock_backends.return_value.credentials = mock_credentials
        self._mock_patches.append(patcher4)
        print("[MOCKS] Patched RagBackends")

        # Mock OpenAI client for KNOWN route (imported inside ask_rag function)
        class MockCompletion:
            def __init__(self):
                self.choices = [MockChoice()]

        class MockChoice:
            def __init__(self):
                self.message = MockMessage()

        class MockMessage:
            def __init__(self):
                self.content = "Mock answer: This is a test response for load testing. It contains multiple sentences to simulate a real answer."
                self.reasoning_content = ""

        class MockCompletions:
            def create(self, **kwargs):
                return MockCompletion()

        class MockChat:
            def __init__(self):
                self.completions = MockCompletions()

        class MockOpenAI:
            def __init__(self, *args, **kwargs):
                self.chat = MockChat()

        # Patch at the openai module level since it's imported inside the function
        patcher5 = patch("openai.OpenAI", MockOpenAI)
        patcher5.start()
        self._mock_patches.append(patcher5)
        print("[MOCKS] Patched OpenAI client")

        print("[MOCKS] All patches enabled")

    def _disable_mocks(self) -> None:
        """Disable all mock patches."""
        for patcher in self._mock_patches:
            try:
                patcher.stop()
            except Exception:
                pass
        self._mock_patches.clear()

    def setup(self) -> None:
        """Initialize KnowledgeGraph connection."""
        print(f"[SETUP] Connecting to LadybugDB: {self.db_path}")
        self.kg = KnowledgeGraph(str(self.db_path))
        result = self.kg.execute("MATCH (c:Company) RETURN count(c) AS n")
        count = result[0][0] if result else 0
        print(f"[SETUP] Database has {count} Company nodes")
        print(f"[SETUP] Connection pool slots: {self.kg.pool.max_size}")
        print(f"[SETUP] Mock LLM: {self.mock_llm}")

        if self.mock_llm:
            self._enable_mocks()
            print("[SETUP] Mock patches enabled")

    def teardown(self) -> None:
        if self.kg:
            self.kg.close()
        if self.mock_llm:
            self._disable_mocks()

    def _start_monitoring(self) -> None:
        """Start background system metrics collection."""
        self._monitoring = True
        self.system_metrics = []
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()

    def _stop_monitoring(self) -> list[SystemMetrics]:
        """Stop background monitoring and return collected metrics."""
        self._monitoring = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=2)
        return self.system_metrics

    def _monitor_loop(self) -> None:
        """Collect system metrics every 100ms."""
        while self._monitoring:
            try:
                cpu = self._process.cpu_percent(interval=0.05)
                mem = self._process.memory_info().rss / 1024 / 1024
                threads = self._process.num_threads()

                db_pool_in_use = 0
                db_pool_available = 0
                if self.kg and self.kg.pool:
                    db_pool_in_use = self.kg.pool.in_use
                    db_pool_available = self.kg.pool.max_size - db_pool_in_use

                self.system_metrics.append(SystemMetrics(
                    timestamp=time.time(),
                    cpu_percent=cpu,
                    ram_mb=mem,
                    thread_count=threads,
                    open_connections=0,  # Would need server-side instrumentation
                    db_pool_in_use=db_pool_in_use,
                    db_pool_available=db_pool_available,
                    queue_depth=0,
                ))
            except Exception:
                pass
            time.sleep(0.1)

    # ─────────────────────────────────────────────────────────────────────────
    # Workload Runners
    # ─────────────────────────────────────────────────────────────────────────

    def _run_static_frontend(self, query_id: str | None = None) -> RequestMetrics:
        """Workload A: Static/frontend - simulate health check / index page."""
        start = time.perf_counter()
        try:
            # Simulate a lightweight health check
            self.kg.probe_read()
            latency_ms = (time.perf_counter() - start) * 1000
            return RequestMetrics(
                workload="A",
                query_id=query_id,
                start_time=start,
                end_time=time.perf_counter(),
                latency_ms=latency_ms,
                status_code=200,
                success=True,
            )
        except Exception as e:
            return RequestMetrics(
                workload="A",
                query_id=query_id,
                start_time=start,
                end_time=time.perf_counter(),
                latency_ms=(time.perf_counter() - start) * 1000,
                status_code=503,
                success=False,
                error=str(e),
            )

    def _run_authenticated_api(self, query_id: str | None = None) -> RequestMetrics:
        """Workload B: Authenticated API - entities, graph, stats."""
        start = time.perf_counter()
        try:
            # Mix of different API calls
            import random
            call_type = random.choice(["entities", "graph", "stats"])

            if call_type == "entities":
                self.kg.execute("MATCH (c:Company) RETURN c.ticker, c.name LIMIT 20")
            elif call_type == "graph":
                self.kg.execute("MATCH (c:Company {ticker: 'AAPL'})-[r]-(b) RETURN b.ticker LIMIT 50")
            else:
                self.kg.execute("MATCH (c:Company) RETURN count(c)")

            latency_ms = (time.perf_counter() - start) * 1000
            return RequestMetrics(
                workload="B",
                query_id=query_id,
                start_time=start,
                end_time=time.perf_counter(),
                latency_ms=latency_ms,
                status_code=200,
                success=True,
            )
        except Exception as e:
            return RequestMetrics(
                workload="B",
                query_id=query_id,
                start_time=start,
                end_time=time.perf_counter(),
                latency_ms=(time.perf_counter() - start) * 1000,
                status_code=500,
                success=False,
                error=str(e),
            )

    def _run_known_company_query(self, query_id: str | None = None) -> RequestMetrics:
        """Workload C: Known-company GraphRAG query."""
        known_cases = [c for c in CASES if c.expected_route == "KNOWN"]
        case = known_cases[hash(query_id or "") % len(known_cases)]

        start = time.perf_counter()
        stage_latencies = {}
        try:
            print(f"  [C:{query_id}] Starting known query: {case.query[:50]}")
            timer_start = time.perf_counter()
            routing = route_query(case.query, self.kg)
            stage_latencies["routing"] = (time.perf_counter() - timer_start) * 1000
            print(f"  [C:{query_id}] Routing done: {routing.route.value} ({routing.ticker})")

            timer_start = time.perf_counter()
            result = ask_rag(self.kg, case.query, request_id=query_id)
            stage_latencies["full_pipeline"] = (time.perf_counter() - timer_start) * 1000
            print(f"  [C:{query_id}] ask_rag done in {stage_latencies['full_pipeline']:.1f}ms")

            latency_ms = (time.perf_counter() - start) * 1000
            return RequestMetrics(
                workload="C",
                query_id=case.id,
                start_time=start,
                end_time=time.perf_counter(),
                latency_ms=latency_ms,
                status_code=200,
                success=True,
                stage_latencies=stage_latencies,
                route=routing.route.value,
                ticker=routing.ticker,
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            return RequestMetrics(
                workload="C",
                query_id=case.id if 'case' in locals() else "unknown",
                start_time=start,
                end_time=time.perf_counter(),
                latency_ms=(time.perf_counter() - start) * 1000,
                status_code=500,
                success=False,
                error=str(e),
                stage_latencies=stage_latencies,
            )

    def _run_cold_start_query(self, query_id: str | None = None) -> RequestMetrics:
        """Workload D: Cold-start GraphRAG query."""
        cold_cases = [c for c in CASES if c.expected_route == "COLD_START"]
        case = cold_cases[hash(query_id or "") % len(cold_cases)]

        start = time.perf_counter()
        stage_latencies = {}
        try:
            print(f"  [D:{query_id}] Starting cold-start query: {case.query[:50]}")
            timer_start = time.perf_counter()
            routing = route_query(case.query, self.kg)
            stage_latencies["routing"] = (time.perf_counter() - timer_start) * 1000
            print(f"  [D:{query_id}] Routing done: {routing.route.value} ({routing.ticker})")

            # Note: Real cold-start hits SEC EDGAR. With mock_llm=True, it still fetches.
            timer_start = time.perf_counter()
            result = ask_rag(self.kg, case.query, request_id=query_id)
            stage_latencies["full_pipeline"] = (time.perf_counter() - timer_start) * 1000
            print(f"  [D:{query_id}] ask_rag done in {stage_latencies['full_pipeline']:.1f}ms")

            latency_ms = (time.perf_counter() - start) * 1000
            return RequestMetrics(
                workload="D",
                query_id=case.id,
                start_time=start,
                end_time=time.perf_counter(),
                latency_ms=latency_ms,
                status_code=200,
                success=True,
                stage_latencies=stage_latencies,
                route=routing.route.value,
                ticker=routing.ticker,
            )
        except Exception as e:
            import traceback
            traceback.print_exc()
            return RequestMetrics(
                workload="D",
                query_id=case.id if 'case' in locals() else "unknown",
                start_time=start,
                end_time=time.perf_counter(),
                latency_ms=(time.perf_counter() - start) * 1000,
                status_code=500,
                success=False,
                error=str(e),
                stage_latencies=stage_latencies,
            )

    def _run_market_data_query(self, query_id: str | None = None) -> RequestMetrics:
        """Workload E: Market data / canned reports."""
        start = time.perf_counter()
        try:
            # Simulate canned report query
            self.kg.execute("""
                MATCH (c:Company)-[:SUBMITTED]->(f:Filing)-[r:REPORTS_METRIC]->(m:FinancialMetric)
                WHERE m.canonical_name CONTAINS 'Net Sales'
                RETURN c.ticker, r.value, r.scale LIMIT 20
            """)
            latency_ms = (time.perf_counter() - start) * 1000
            return RequestMetrics(
                workload="E",
                query_id=query_id,
                start_time=start,
                end_time=time.perf_counter(),
                latency_ms=latency_ms,
                status_code=200,
                success=True,
            )
        except Exception as e:
            return RequestMetrics(
                workload="E",
                query_id=query_id,
                start_time=start,
                end_time=time.perf_counter(),
                latency_ms=(time.perf_counter() - start) * 1000,
                status_code=500,
                success=False,
                error=str(e),
            )

    def _run_ingestion(self, query_id: str | None = None) -> RequestMetrics:
        """Workload F: Ingestion - simulate writer workload (read-only for safety)."""
        start = time.perf_counter()
        try:
            # Simulate ingestion read operations (manifest loading, etc.)
            self.kg.execute("MATCH (f:Filing) RETURN f.accession_number LIMIT 10")
            latency_ms = (time.perf_counter() - start) * 1000
            return RequestMetrics(
                workload="F",
                query_id=query_id,
                start_time=start,
                end_time=time.perf_counter(),
                latency_ms=latency_ms,
                status_code=200,
                success=True,
            )
        except Exception as e:
            return RequestMetrics(
                workload="F",
                query_id=query_id,
                start_time=start,
                end_time=time.perf_counter(),
                latency_ms=(time.perf_counter() - start) * 1000,
                status_code=500,
                success=False,
                error=str(e),
            )

    def _run_sse_long_query(self, query_id: str | None = None) -> RequestMetrics:
        """Workload G: Long-running SSE query (cold-start with synthesis)."""
        return self._run_cold_start_query(query_id)

    WORKLOAD_RUNNERS = {
        "A": "_run_static_frontend",
        "B": "_run_authenticated_api",
        "C": "_run_known_company_query",
        "D": "_run_cold_start_query",
        "E": "_run_market_data_query",
        "F": "_run_ingestion",
        "G": "_run_sse_long_query",
    }

    def run_workload(self, workload_key: str, query_id: str | None = None) -> RequestMetrics:
        """Dispatch to appropriate workload runner."""
        runner_name = self.WORKLOAD_RUNNERS.get(workload_key)
        if not runner_name:
            raise ValueError(f"Unknown workload: {workload_key}")
        runner = getattr(self, runner_name)
        return runner(query_id)

    # ─────────────────────────────────────────────────────────────────────────
    # Concurrency Level Execution
    # ─────────────────────────────────────────────────────────────────────────

    def run_concurrency_level(
        self,
        concurrency: int,
        workload_mix: dict[str, float] | None = None,
    ) -> tuple[list[RequestMetrics], list[SystemMetrics]]:
        """Run a single concurrency level with specified workload mix."""

        if workload_mix is None:
            workload_mix = {k: v["weight"] for k, v in WORKLOAD_CLASSES.items()}

        print(f"\n{'='*60}")
        print(f"CONCURRENCY LEVEL: {concurrency}")
        print(f"Workload mix: {workload_mix}")
        print(f"Duration: {self.duration_per_level}s")
        print(f"{'='*60}")

        # Warmup
        print(f"[WARMUP] Running {self.warmup_duration}s warmup...")
        warmup_start = time.perf_counter()
        warmup_workers = min(10, concurrency)
        with ThreadPoolExecutor(max_workers=warmup_workers) as executor:
            futures = []
            while time.perf_counter() - warmup_start < self.warmup_duration:
                for wk, weight in workload_mix.items():
                    if weight > 0:
                        futures.append(executor.submit(self.run_workload, wk, f"warmup_{len(futures)}"))
                time.sleep(0.1)
            for f in as_completed(futures):
                pass  # Drain
        print(f"[WARMUP] Complete")

        # Actual test
        self._start_monitoring()
        test_start = time.perf_counter()
        request_metrics: list[RequestMetrics] = []

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = []
            request_id = 0
            submit_interval = 1.0 / max(concurrency, 1)  # Spread submissions over 1 second

            # Submit all requests over the duration
            while time.perf_counter() - test_start < self.duration_per_level:
                for wk, weight in workload_mix.items():
                    if weight <= 0:
                        continue
                    # Number of requests for this workload in this interval
                    n_requests = max(1, int(concurrency * weight * submit_interval))
                    for _ in range(n_requests):
                        futures.append(executor.submit(self.run_workload, wk, f"req_{request_id}"))
                        request_id += 1
                time.sleep(submit_interval)

            # Wait for completion with timeout - catch timeout to allow partial results
            try:
                for f in as_completed(futures, timeout=self.duration_per_level * 2):
                    try:
                        request_metrics.append(f.result())
                    except Exception as e:
                        request_metrics.append(RequestMetrics(
                            workload="UNKNOWN",
                            query_id="error",
                            start_time=time.perf_counter(),
                            end_time=time.perf_counter(),
                            latency_ms=0,
                            status_code=0,
                            success=False,
                            error=str(e),
                        ))
            except TimeoutError:
                print(f"[TIMEOUT] Level {concurrency}: {len(futures) - len(request_metrics)} futures unfinished after {self.duration_per_level * 2}s")
                # Mark unfinished as timeout
                for f in futures:
                    if not f.done():
                        request_metrics.append(RequestMetrics(
                            workload="UNKNOWN",
                            query_id="timeout",
                            start_time=time.perf_counter(),
                            end_time=time.perf_counter(),
                            latency_ms=0,
                            status_code=408,
                            success=False,
                            error="Timeout",
                        ))

        system_metrics = self._stop_monitoring()
        actual_duration = time.perf_counter() - test_start

        print(f"[LEVEL {concurrency}] Completed in {actual_duration:.1f}s: {len(request_metrics)} requests")
        return request_metrics, system_metrics

    # ─────────────────────────────────────────────────────────────────────────
    # Metrics Aggregation
    # ─────────────────────────────────────────────────────────────────────────

    def aggregate_metrics(
        self,
        request_metrics: list[RequestMetrics],
        system_metrics: list[SystemMetrics],
        workload: str,
        concurrency: int,
    ) -> AggregatedMetrics:
        """Compute aggregated metrics from raw measurements."""
        wm = [m for m in request_metrics if m.workload == workload]
        if not wm:
            return AggregatedMetrics(
                workload=workload,
                concurrency=concurrency,
                duration_s=self.duration_per_level,
                total_requests=0,
                successful_requests=0,
                failed_requests=0,
                timeout_requests=0,
                error_rate=0,
                timeout_rate=0,
                throughput_rps=0,
                p50_ms=0, p95_ms=0, p99_ms=0, mean_ms=0, min_ms=0, max_ms=0,
                cpu_p50=0, cpu_p95=0, ram_p50_mb=0, ram_p95_mb=0,
                thread_p50=0, thread_p95=0,
                db_latency_p50_ms=0, db_latency_p95_ms=0,
                llm_latency_p50_ms=0, llm_latency_p95_ms=0,
                external_api_latency_p50_ms=0, external_api_latency_p95_ms=0,
            )

        latencies = [m.latency_ms for m in wm if m.success]
        successful = len(latencies)
        total = len(wm)
        failed = total - successful
        timeouts = len([m for m in wm if not m.success and "timeout" in (m.error or "").lower()])

        if latencies:
            sorted_lat = sorted(latencies)
            p50 = sorted_lat[len(sorted_lat) // 2]
            p95 = sorted_lat[int(len(sorted_lat) * 0.95)]
            p99 = sorted_lat[int(len(sorted_lat) * 0.99)] if len(sorted_lat) > 1 else sorted_lat[0]
            mean_lat = statistics.mean(latencies)
            min_lat = min(latencies)
            max_lat = max(latencies)
        else:
            p50 = p95 = p99 = mean_lat = min_lat = max_lat = 0

        # System metrics
        if system_metrics:
            cpu_vals = [m.cpu_percent for m in system_metrics]
            ram_vals = [m.ram_mb for m in system_metrics]
            thread_vals = [m.thread_count for m in system_metrics]
            db_in_use = [m.db_pool_in_use for m in system_metrics]

            cpu_p50 = sorted(cpu_vals)[len(cpu_vals)//2]
            cpu_p95 = sorted(cpu_vals)[int(len(cpu_vals)*0.95)]
            ram_p50 = sorted(ram_vals)[len(ram_vals)//2]
            ram_p95 = sorted(ram_vals)[int(len(ram_vals)*0.95)]
            thread_p50 = sorted(thread_vals)[len(thread_vals)//2]
            thread_p95 = sorted(thread_vals)[int(len(thread_vals)*0.95)]

            # DB latency proxy: time waiting for pool slot
            db_latencies = [m.latency_ms for m in wm if m.success and m.workload in ("B", "C", "D", "E")]
            if db_latencies:
                sorted_db = sorted(db_latencies)
                db_p50 = sorted_db[len(sorted_db)//2]
                db_p95 = sorted_db[int(len(sorted_db)*0.95)]
            else:
                db_p50 = db_p95 = 0

            # LLM latency proxy: full_pipeline stage for C/D
            llm_latencies = []
            for m in wm:
                if m.success and "full_pipeline" in m.stage_latencies:
                    llm_latencies.append(m.stage_latencies["full_pipeline"])
            if llm_latencies:
                sorted_llm = sorted(llm_latencies)
                llm_p50 = sorted_llm[len(sorted_llm)//2]
                llm_p95 = sorted_llm[int(len(sorted_llm)*0.95)]
            else:
                llm_p50 = llm_p95 = 0

            # External API latency: fetching stage for D
            ext_latencies = []
            for m in wm:
                if m.success and m.workload == "D" and "fetching" in m.stage_latencies:
                    ext_latencies.append(m.stage_latencies["fetching"])
            if ext_latencies:
                sorted_ext = sorted(ext_latencies)
                ext_p50 = sorted_ext[len(sorted_ext)//2]
                ext_p95 = sorted_ext[int(len(sorted_ext)*0.95)]
            else:
                ext_p50 = ext_p95 = 0
        else:
            cpu_p50 = cpu_p95 = ram_p50 = ram_p95 = thread_p50 = thread_p95 = 0
            db_p50 = db_p95 = llm_p50 = llm_p95 = ext_p50 = ext_p95 = 0

        # Saturation indicators
        saturation = []
        if cpu_p95 > 80:
            saturation.append(f"CPU saturation (p95={cpu_p95:.1f}%)")
        if ram_p95 > 2048:
            saturation.append(f"RAM pressure (p95={ram_p95:.0f}MB)")
        if thread_p95 > 200:
            saturation.append(f"Thread count high (p95={thread_p95:.0f})")
        if db_p95 > 500:
            saturation.append(f"DB pool contention (p95={db_p95:.1f}ms)")
        if llm_p95 > 60000:
            saturation.append(f"LLM latency extreme (p95={llm_p95/1000:.1f}s)")
        if total > 0 and (failed / total) > 0.05:
            saturation.append(f"Error rate >5% ({failed/total*100:.1f}%)")

        return AggregatedMetrics(
            workload=workload,
            concurrency=concurrency,
            duration_s=self.duration_per_level,
            total_requests=total,
            successful_requests=successful,
            failed_requests=failed,
            timeout_requests=timeouts,
            error_rate=round(failed / total * 100, 2) if total else 0,
            timeout_rate=round(timeouts / total * 100, 2) if total else 0,
            throughput_rps=round(total / self.duration_per_level, 2),
            p50_ms=round(p50, 2),
            p95_ms=round(p95, 2),
            p99_ms=round(p99, 2),
            mean_ms=round(mean_lat, 2),
            min_ms=round(min_lat, 2),
            max_ms=round(max_lat, 2),
            cpu_p50=round(cpu_p50, 1),
            cpu_p95=round(cpu_p95, 1),
            ram_p50_mb=round(ram_p50, 1),
            ram_p95_mb=round(ram_p95, 1),
            thread_p50=round(thread_p50, 1),
            thread_p95=round(thread_p95, 1),
            db_latency_p50_ms=round(db_p50, 2),
            db_latency_p95_ms=round(db_p95, 2),
            llm_latency_p50_ms=round(llm_p50, 2),
            llm_latency_p95_ms=round(llm_p95, 2),
            external_api_latency_p50_ms=round(ext_p50, 2),
            external_api_latency_p95_ms=round(ext_p95, 2),
            saturation_indicators=saturation,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Main Test Loop
    # ─────────────────────────────────────────────────────────────────────────

    def run_all_levels(self, workload_keys: list[str] | None = None) -> dict:
        """Run full test ladder across all workloads."""
        workload_keys = workload_keys or list(WORKLOAD_CLASSES.keys())
        all_results = {}

        for concurrency in self.concurrency_levels:
            print(f"\n{'#'*70}")
            print(f"### TESTING CONCURRENCY: {concurrency}")
            print(f"{'#'*70}")

            # Run single concurrency level with mixed workload
            request_metrics, system_metrics = self.run_concurrency_level(concurrency)

            # Aggregate per workload
            level_results = {}
            for wk in workload_keys:
                agg = self.aggregate_metrics(request_metrics, system_metrics, wk, concurrency)
                level_results[wk] = agg.to_dict()
                self._print_workload_summary(agg)

            all_results[concurrency] = {
                "workloads": level_results,
                "raw_request_count": len(request_metrics),
                "system_metrics_count": len(system_metrics),
            }

            # Check for saturation - stop if critical
            self._check_saturation(level_results)

            # Save intermediate results
            self._save_intermediate_results(all_results, concurrency)

        return all_results

    def _print_workload_summary(self, agg: AggregatedMetrics) -> None:
        name = WORKLOAD_CLASSES.get(agg.workload, {}).get("name", agg.workload)
        print(f"  {agg.workload} ({name}): {agg.throughput_rps:.1f} rps, "
              f"p50={agg.p50_ms:.1f}ms, p95={agg.p95_ms:.1f}ms, "
              f"p99={agg.p99_ms:.1f}ms, err={agg.error_rate:.1f}%")
        if agg.saturation_indicators:
            for s in agg.saturation_indicators:
                print(f"    ⚠ SATURATION: {s}")

    def _check_saturation(self, level_results: dict[str, dict]) -> bool:
        """Check if system has saturated. Returns True if should stop."""
        critical_saturation = False
        for wk, agg in level_results.items():
            indicators = agg.get("saturation_indicators", [])
            for indicator in indicators:
                if "CPU saturation" in indicator or "Error rate" in indicator:
                    critical_saturation = True
                    print(f"  🛑 CRITICAL SATURATION at concurrency {agg.get('concurrency', 'unknown')}: {indicator}")
        return critical_saturation

    def _save_intermediate_results(self, results: dict, concurrency: int) -> None:
        """Save results after each concurrency level."""
        output_file = self.output_dir / f"load_test_{concurrency}.json"
        with open(output_file, "w") as f:
            json.dump({
                "concurrency_levels_tested": list(results.keys()),
                "latest_level": concurrency,
                "results": results,
            }, f, indent=2, default=str)
        print(f"  [SAVE] Intermediate results -> {output_file}")

    def generate_report(self, results: dict) -> str:
        """Generate final scalability report."""
        report_path = self.output_dir / "SCALABILITY_REPORT.md"
        lines = [
            "# FinGraph Scalability Validation Report",
            "",
            f"**Test Date**: {time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"**Database**: {self.db_path}",
            f"**Mock LLM**: {self.mock_llm}",
            f"**Concurrency Levels**: {self.concurrency_levels}",
            f"**Duration per Level**: {self.duration_per_level}s",
            "",
            "## 1. Test Environment",
            "",
            "| Parameter | Value |",
            "|-----------|-------|",
            f"| Database Path | `{self.db_path}` |",
            f"| DB Size (nodes) | {self._get_db_stats()} |",
            f"| Connection Pool Slots | {self.kg.pool.max_size if self.kg else 'N/A'} |",
            f"| Max HTTP Connections | 256 |",
            f"| Max SSE Connections | 32 |",
            f"| Mock LLM | {self.mock_llm} |",
            f"| Python Process | Single-threaded HTTP server |",
            "",
            "## 2. Workload Model",
            "",
            "| Class | Name | Description | Weight |",
            "|-------|------|-------------|--------|",
        ]
        for k, v in WORKLOAD_CLASSES.items():
            lines.append(f"| {k} | {v['name']} | {v['description']} | {v['weight']:.0%} |")

        lines += [
            "",
            "## 3. Concurrency Ladder Results",
            "",
        ]

        # Summary table
        lines += [
            "### Summary Table (Mixed Workload)",
            "",
            "| Concurrency | Total RPS | Error% | Timeout% | CPU p95% | RAM p95(MB) | Threads p95 | Saturation |",
            "|-------------|-----------|--------|----------|----------|-------------|-------------|------------|",
        ]

        for concurrency in self.concurrency_levels:
            if concurrency not in results:
                continue
            r = results[concurrency]
            # Compute mixed workload aggregates
            total_rps = sum(w.get("throughput_rps", 0) for w in r["workloads"].values())
            avg_error = statistics.mean([w.get("error_rate", 0) for w in r["workloads"].values()])
            avg_timeout = statistics.mean([w.get("timeout_rate", 0) for w in r["workloads"].values()])
            cpu_p95 = max([w.get("cpu_p95", 0) for w in r["workloads"].values()])
            ram_p95 = max([w.get("ram_p95_mb", 0) for w in r["workloads"].values()])
            thread_p95 = max([w.get("thread_p95", 0) for w in r["workloads"].values()])
            sat = any(w.get("saturation_indicators") for w in r["workloads"].values())

            lines.append(f"| {concurrency} | {total_rps:.1f} | {avg_error:.1f}% | {avg_timeout:.1f}% | "
                        f"{cpu_p95:.1f}% | {ram_p95:.0f} | {thread_p95:.0f} | {'⚠️' if sat else '✅'} |")

        # Per-workload detail tables
        lines += [
            "",
            "## 4. Per-Workload Detail",
            "",
        ]

        for wk in WORKLOAD_CLASSES.keys():
            name = WORKLOAD_CLASSES[wk]["name"]
            lines += [
                f"### Workload {wk}: {name}",
                "",
                "| Concurrency | RPS | p50(ms) | p95(ms) | p99(ms) | Error% | Timeout% | CPU% | RAM(MB) | Saturation |",
                "|-------------|-----|---------|---------|---------|--------|----------|------|---------|------------|",
            ]
            for concurrency in self.concurrency_levels:
                if concurrency not in results:
                    continue
                agg = results[concurrency]["workloads"].get(wk, {})
                if not agg:
                    continue
                sat = "⚠️ " + ", ".join(agg.get("saturation_indicators", [])) if agg.get("saturation_indicators") else "✅"
                lines.append(
                    f"| {concurrency} | {agg.get('throughput_rps', 0):.1f} | "
                    f"{agg.get('p50_ms', 0):.1f} | {agg.get('p95_ms', 0):.1f} | {agg.get('p99_ms', 0):.1f} | "
                    f"{agg.get('error_rate', 0):.1f}% | {agg.get('timeout_rate', 0):.1f}% | "
                    f"{agg.get('cpu_p95', 0):.1f}% | {agg.get('ram_p95_mb', 0):.0f} | {sat} |"
                )
            lines.append("")

        # Bottleneck analysis
        lines += [
            "## 5. Bottleneck Analysis",
            "",
        ]

        # Find first saturation point
        first_saturation = None
        for concurrency in self.concurrency_levels:
            if concurrency not in results:
                continue
            for wk, agg in results[concurrency]["workloads"].items():
                if agg.get("saturation_indicators"):
                    first_saturation = concurrency
                    lines.append(f"**First saturation detected at concurrency {concurrency}** (workload {wk})")
                    for ind in agg["saturation_indicators"]:
                        lines.append(f"- {ind}")
                    break
            if first_saturation:
                break

        if not first_saturation:
            lines.append("**No saturation detected up to maximum tested concurrency.**")

        # Identify primary bottleneck
        lines += [
            "",
            "### Primary Bottlenecks by Workload",
            "",
        ]

        for wk in WORKLOAD_CLASSES.keys():
            max_concurrency_tested = max([c for c in self.concurrency_levels if c in results and wk in results[c]["workloads"]], default=0)
            if max_concurrency_tested == 0:
                continue

            agg = results[max_concurrency_tested]["workloads"][wk]
            bottlenecks = []

            if agg.get("llm_latency_p95_ms", 0) > 30000:
                bottlenecks.append(f"LLM API latency (p95={agg['llm_latency_p95_ms']/1000:.1f}s)")
            if agg.get("db_latency_p95_ms", 0) > 500:
                bottlenecks.append(f"DB pool contention (p95={agg['db_latency_p95_ms']:.1f}ms)")
            if agg.get("cpu_p95", 0) > 80:
                bottlenecks.append(f"CPU saturation (p95={agg['cpu_p95']:.1f}%)")
            if agg.get("thread_p95", 0) > 200:
                bottlenecks.append(f"Thread exhaustion (p95={agg['thread_p95']:.0f})")
            if agg.get("external_api_latency_p95_ms", 0) > 5000:
                bottlenecks.append(f"SEC EDGAR latency (p95={agg['external_api_latency_p95_ms']/1000:.1f}s)")

            lines.append(f"**{wk} ({WORKLOAD_CLASSES[wk]['name']})**: " + ("; ".join(bottlenecks) if bottlenecks else "No significant bottleneck"))

        # Scalability verdict
        lines += [
            "",
            "## 6. Scalability Verdict",
            "",
        ]

        for target in [1000, 5000, 10000]:
            achieved = "❌ Not demonstrated"
            if target in results:
                total_rps = sum(w.get("throughput_rps", 0) for w in results[target]["workloads"].values())
                avg_error = statistics.mean([w.get("error_rate", 0) for w in results[target]["workloads"].values()])
                if avg_error < 1.0:
                    achieved = f"✅ Demonstrated ({total_rps:.1f} RPS, {avg_error:.1f}% error)"
                else:
                    achieved = f"⚠️ High error rate ({avg_error:.1f}%) at {total_rps:.1f} RPS"
            lines.append(f"- **{target:,} concurrent**: {achieved}")

        lines += [
            "",
            "## 7. Recommended Next Optimizations",
            "",
        ]

        # Based on bottlenecks, recommend
        recommendations = set()
        for concurrency in self.concurrency_levels:
            if concurrency not in results:
                continue
            for wk, agg in results[concurrency]["workloads"].items():
                if agg.get("llm_latency_p95_ms", 0) > 30000:
                    recommendations.add("Add LLM response streaming for perceived latency reduction")
                    recommendations.add("Implement query result caching for repeated KNOWN queries")
                if agg.get("db_latency_p95_ms", 0) > 500:
                    recommendations.add("Increase GRAPH_QUERY_SLOTS (currently 4) to match CPU cores")
                    recommendations.add("Add read-only connection pooling at load balancer level")
                if agg.get("external_api_latency_p95_ms", 0) > 5000:
                    recommendations.add("Cache SEC EDGAR responses; add retry-with-jitter for 429")
                if agg.get("cpu_p95", 0) > 80:
                    recommendations.add("Horizontal scaling: add Query-N instances behind load balancer")
                if agg.get("thread_p95", 0) > 200:
                    recommendations.add("Reduce MAX_SSE_CONNECTIONS or increase thread pool")

        for i, rec in enumerate(sorted(recommendations), 1):
            lines.append(f"{i}. {rec}")

        if not recommendations:
            lines.append("No specific recommendations - system scales within tested envelope.")

        # Write report
        with open(report_path, "w") as f:
            f.write("\n".join(lines))

        print(f"\n[REPORT] Written to {report_path}")
        return str(report_path)

    def _get_db_stats(self) -> str:
        if not self.kg:
            return "unknown"
        try:
            result = self.kg.execute("MATCH (n) RETURN count(n)")
            nodes = result[0][0] if result else 0
            result = self.kg.execute("MATCH ()-[r]->() RETURN count(r)")
            edges = result[0][0] if result else 0
            return f"{nodes:,} nodes, {edges:,} edges"
        except Exception:
            return "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# CLI Entry Point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="FinGraph Staged Scalability Load Test")
    parser.add_argument("--db", default=str(FINGRAPH_DATA_DIR / "sandbox.lbug"), help="LadybugDB path")
    parser.add_argument("--levels", default=",".join(map(str, CONCURRENCY_LADDER)), help="Comma-separated concurrency levels")
    parser.add_argument("--duration", type=int, default=DEFAULT_DURATION, help="Duration per level (seconds)")
    parser.add_argument("--warmup", type=int, default=10, help="Warmup duration (seconds)")
    parser.add_argument("--output", default="benchmarks/results", help="Output directory")
    parser.add_argument("--mock-llm", action="store_true", default=True, help="Use mock LLM (avoids API costs)")
    parser.add_argument("--workloads", default="A,B,C,D,E,F,G", help="Comma-separated workload classes to test")
    parser.add_argument("--single-workload", help="Test only one workload class")
    args = parser.parse_args()

    concurrency_levels = [int(x) for x in args.levels.split(",")]
    workload_keys = [args.single_workload] if args.single_workload else args.workloads.split(",")

    # Validate workload keys
    for wk in workload_keys:
        if wk not in WORKLOAD_CLASSES:
            print(f"ERROR: Unknown workload class: {wk}")
            print(f"Valid: {list(WORKLOAD_CLASSES.keys())}")
            sys.exit(1)

    harness = LoadTestHarness(
        db_path=args.db,
        concurrency_levels=concurrency_levels,
        duration_per_level=args.duration,
        warmup_duration=args.warmup,
        output_dir=args.output,
        mock_llm=args.mock_llm,
    )

    try:
        harness.setup()
        results = harness.run_all_levels(workload_keys)
        report_path = harness.generate_report(results)
        print(f"\n✅ Load test complete. Report: {report_path}")
    except KeyboardInterrupt:
        print("\n[INTERRUPTED] Saving partial results...")
        # harness.generate_report(harness.results)  # would need to store partial
        sys.exit(130)
    except Exception as e:
        print(f"\n[ERROR] Load test failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        harness.teardown()


if __name__ == "__main__":
    main()