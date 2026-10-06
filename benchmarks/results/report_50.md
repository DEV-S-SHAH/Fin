# Graph RAG — 50-Query Evaluation Report

- **Mode:** `mock-llm`
- **Started:** 2026-10-06 06:29:55
- **Finished:** 2026-10-06 06:29:56
- **Wall clock:** 0.3s
- **Cases:** 50

## 1. Executive Scorecard

| Metric | Target | Actual | Status |
| --- | ---: | ---: | :---: |
| Routing Accuracy | 100% | 100.0% | PASS |
| Cold Start Success | 90% | 100.0% | PASS |
| Isolation Score | 100% | 100.0% | PASS |
| Multi Hop Reachability | 80% | 100.0% | PASS |
| Groundedness | 85% | n/a | n/a |
| Overall Pass Rate | — | 100.0% | PASS |

> **Content metrics not measured in this mode.** `--mock-llm` serves one canned answer for all 50 cases, so groundedness and required-concept matching would score the fixture rather than the system. Routing, isolation, hop depth and latency below are real. Run `--live` to gate the content metrics.

## 2. Category Breakdown

| Category | Cases | Pass | Route OK | Isolation | Hops OK | Grounded | Avg depth | P50 total (ms) | P95 total (ms) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A. Seeded Backbone Multi-Hop Reasoning | 10 | 100% | 100% | 100% | 100% | n/a | 2.0 | 0.2 | 70.5 |
| B. Cold-Start JIT Dynamic Ingestion | 15 | 100% | 100% | 100% | 100% | n/a | 2.0 | 0.2 | 6.4 |
| C. Executive Transitions & Governance Shocks | 10 | 100% | 100% | 100% | 100% | n/a | 2.0 | 0.1 | 0.1 |
| D. Cross-Entity Contagion & Second-Order Shocks | 10 | 100% | 100% | 100% | 100% | n/a | 2.0 | 0.1 | 0.2 |
| E. Ambiguity, Edge Cases & Negative Controls | 5 | 100% | 100% | 100% | n/a | n/a | n/a | n/a | n/a |

## 3. Latency Distribution

Nearest-rank percentiles over observed values. With 50 samples an interpolated P99 would imply precision the sample cannot support.

| Stage | n | P50 (ms) | P90 (ms) | P95 (ms) | P99 (ms) |
| --- | ---: | ---: | ---: | ---: | ---: |
| extract_ms | 22 | 0.0 | 0.0 | 0.0 | 0.0 |
| fetch_ms | 22 | 0.0 | 0.1 | 0.1 | 0.1 |
| routing_ms | 50 | 0.0 | 0.1 | 0.1 | 0.1 |
| stitch_ms | 45 | 0.1 | 0.1 | 0.1 | 0.5 |
| synthesis_ms | 45 | 0.0 | 0.0 | 0.0 | 0.0 |
| total_ms | 45 | 0.1 | 0.2 | 0.2 | 70.5 |
| traversal_ms | 45 | 0.0 | 0.0 | 0.0 | 0.0 |

## 4. Per-Query Results

| ID | Category | Route (exp/act) | Pass | Depth | Isolation | Grounded |
| --- | --- | --- | :---: | ---: | :---: | :---: |
| A01 | seeded_multi_hop | KNOWN/KNOWN | Y | 2 | OK | n/a |
| A02 | seeded_multi_hop | KNOWN/KNOWN | Y | 2 | OK | n/a |
| A03 | seeded_multi_hop | KNOWN/KNOWN | Y | 2 | OK | n/a |
| A04 | seeded_multi_hop | KNOWN/KNOWN | Y | 2 | OK | n/a |
| A05 | seeded_multi_hop | KNOWN/KNOWN | Y | 2 | OK | n/a |
| A06 | seeded_multi_hop | KNOWN/KNOWN | Y | 2 | OK | n/a |
| A07 | seeded_multi_hop | KNOWN/KNOWN | Y | 2 | OK | n/a |
| A08 | seeded_multi_hop | KNOWN/KNOWN | Y | 2 | OK | n/a |
| A09 | seeded_multi_hop | KNOWN/KNOWN | Y | 2 | OK | n/a |
| A10 | seeded_multi_hop | KNOWN/KNOWN | Y | 2 | OK | n/a |
| B01 | cold_start | COLD_START/COLD_START | Y | 2 | OK | n/a |
| B02 | cold_start | COLD_START/COLD_START | Y | 2 | OK | n/a |
| B03 | cold_start | COLD_START/COLD_START | Y | 2 | OK | n/a |
| B04 | cold_start | COLD_START/COLD_START | Y | 2 | OK | n/a |
| B05 | cold_start | COLD_START/COLD_START | Y | 2 | OK | n/a |
| B06 | cold_start | COLD_START/COLD_START | Y | 2 | OK | n/a |
| B07 | cold_start | COLD_START/COLD_START | Y | 2 | OK | n/a |
| B08 | cold_start | COLD_START/COLD_START | Y | 2 | OK | n/a |
| B09 | cold_start | COLD_START/COLD_START | Y | 2 | OK | n/a |
| B10 | cold_start | COLD_START/COLD_START | Y | 2 | OK | n/a |
| B11 | cold_start | COLD_START/COLD_START | Y | 2 | OK | n/a |
| B12 | cold_start | COLD_START/COLD_START | Y | 2 | OK | n/a |
| B13 | cold_start | COLD_START/COLD_START | Y | 2 | OK | n/a |
| B14 | cold_start | COLD_START/COLD_START | Y | 2 | OK | n/a |
| B15 | cold_start | COLD_START/COLD_START | Y | 2 | OK | n/a |
| C01 | executive_transition | KNOWN/KNOWN | Y | 2 | OK | n/a |
| C02 | executive_transition | KNOWN/KNOWN | Y | 2 | OK | n/a |
| C03 | executive_transition | KNOWN/KNOWN | Y | 2 | OK | n/a |
| C04 | executive_transition | KNOWN/KNOWN | Y | 2 | OK | n/a |
| C05 | executive_transition | KNOWN/KNOWN | Y | 2 | OK | n/a |
| C06 | executive_transition | KNOWN/KNOWN | Y | 2 | OK | n/a |
| C07 | executive_transition | KNOWN/KNOWN | Y | 2 | OK | n/a |
| C08 | executive_transition | KNOWN/KNOWN | Y | 2 | OK | n/a |
| C09 | executive_transition | KNOWN/KNOWN | Y | 2 | OK | n/a |
| C10 | executive_transition | KNOWN/KNOWN | Y | 2 | OK | n/a |
| D01 | contagion | COLD_START/COLD_START | Y | 2 | OK | n/a |
| D02 | contagion | KNOWN/KNOWN | Y | 2 | OK | n/a |
| D03 | contagion | KNOWN/KNOWN | Y | 2 | OK | n/a |
| D04 | contagion | KNOWN/KNOWN | Y | 2 | OK | n/a |
| D05 | contagion | COLD_START/COLD_START | Y | 2 | OK | n/a |
| D06 | contagion | COLD_START/COLD_START | Y | 2 | OK | n/a |
| D07 | contagion | COLD_START/COLD_START | Y | 2 | OK | n/a |
| D08 | contagion | COLD_START/COLD_START | Y | 2 | OK | n/a |
| D09 | contagion | COLD_START/COLD_START | Y | 2 | OK | n/a |
| D10 | contagion | COLD_START/COLD_START | Y | 2 | OK | n/a |
| E01 | negative_control | AMBIGUOUS/AMBIGUOUS | Y | 0 | OK | n/a |
| E02 | negative_control | AMBIGUOUS/AMBIGUOUS | Y | 0 | OK | n/a |
| E03 | negative_control | AMBIGUOUS/AMBIGUOUS | Y | 0 | OK | n/a |
| E04 | negative_control | AMBIGUOUS/AMBIGUOUS | Y | 0 | OK | n/a |
| E05 | negative_control | AMBIGUOUS/AMBIGUOUS | Y | 0 | OK | n/a |

## 5. Failure Log

No failures. All cases passed every scored metric.
