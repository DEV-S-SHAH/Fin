# GraphRAG Pipeline Baseline Benchmark Report

## Executive Summary

Baseline measurements established for all 10 GraphRAG pipeline stages against the authoritative LadybugDB (`sandbox_engine/_run/sandbox.lbug`, 4 Company nodes, 122,158 total nodes, 189,723 relationships).

## Baseline Measurements

### Stage-Level Metrics (Isolated, 3 iterations)

| Stage | Mean (ms) | P50 (ms) | P95 (ms) | P99 (ms) | Success Rate |
|-------|-----------|----------|----------|----------|--------------|
| 1. Graph Node Lookup | 0.8 | 0.2 | 2.1 | 2.1 | 100% |
| 2. Edge Traversal (backbone) | 2.3 | 1.4 | 4.3 | 4.3 | 100% |
| 3. 1-hop Retrieval | 2.0 | 1.4 | 3.8 | 3.8 | 100% |
| 4. 2-hop Retrieval | 9.5 | 7.9 | 16.4 | 16.4 | 100% |
| 5. 3-hop Retrieval | 15.8 | 12.8 | 24.3 | 24.3 | 100% |
| 6. Metric/Entity Resolution | 0.1 | 0.0 | 0.2 | 0.2 | 100% |
| 7. Known-Company Query (full) | 20,673 | 18,581 | 44,901 | 44,901 | 100% |
| 8. Cold-Start Query (mocked) | 13.4 | 14.8 | 17.6 | 17.6 | 100% |
| 9. Graph+Vector Retrieval | 1.9 | 0.4 | 5.2 | 5.2 | 100% |
| 10. LLM Synthesis (mocked) | 0.0 | 0.0 | 0.0 | 0.0 | 100% |

### Key Finding: KNOWN Route Breakdown

The KNOWN route latency (~20s) is dominated by LLM API calls:
- **Graph retrieval (`retrieve_financial_context`)**: 130-550ms
- **LLM synthesis (NVIDIA Nemotron)**: ~20-45s

The COLD_START route uses mocks and completes in ~13ms. With real LLM calls, it would be similar to KNOWN route.

### Routing Correctness

- **85.7%** correct (18/21 queries)
- **Failure**: "Snowflake" not in `COMPANY_ALIASES` → routes as AMBIGUOUS instead of COLD_START

## Identified Bottlenecks

### 1. LLM Synthesis (External) - ~20-45s
**Not optimizable locally** - external API dependency. Options:
- Add streaming for perceived latency improvement
- Cache frequent queries
- Use smaller/faster models for simple queries

### 2. Graph Retrieval (`retrieve_financial_context`) - 130-550ms
**Primary optimization target** for KNOWN route:
- Multiple Cypher queries per request
- No query result caching
- Fetches all filings then filters in Python

### 3. Multi-hop Traversal (9-24ms) - Already Fast
- 2-hop: ~9.5ms mean
- 3-hop: ~15.8ms mean
- LadybugDB performance is good for graph traversals

### 4. Missing Entity in Router
- Snowflake (SNOW) not in `COMPANY_ALIASES` → routes as AMBIGUOUS

## Optimization Plan

### Phase 1: Quick Wins (Low Risk, High Impact)

1. **Add Snowflake to COMPANY_ALIASES** - Fixes routing correctness
2. **Cache `retrieve_financial_context` results** - TTL-based cache for repeated queries
3. **Optimize Cypher in `retrieve_financial_context`** - Combine queries, add LIMIT early

### Phase 2: Structural Optimizations (Medium Risk)

4. **Connection pooling for LadybugDB** - Reuse connections (already have `_ConnectionPool` for query_ui)
5. **Pre-compute common traversals** - Materialize frequent 2-hop neighborhoods
6. **Add query result caching layer** - Redis or in-memory with invalidation

### Phase 3: Advanced (Higher Risk)

7. **Parallelize independent Cypher queries** - Use threading for I/O bound queries
8. **Streaming LLM responses** - Already implemented in cold-start, apply to KNOWN route
9. **Async HTTP client for LLM calls** - Reduce blocking

## Correctness Verification Requirements

All optimizations must preserve:
- ✅ Stable node IDs (content-addressed via `stable_id`)
- ✅ Evidence integrity (provenance ledger, citation tags)
- ✅ Retrieval correctness (same nodes/edges for same query)
- ✅ Ranking behavior (relevance ordering by hop depth)
- ✅ No new dependencies, databases, or frameworks
- ✅ No destructive DB operations

## Next Steps

1. Implement Phase 1 optimizations
2. Re-run benchmark to measure improvement
3. Verify correctness with existing test suite
4. Document before/after comparison