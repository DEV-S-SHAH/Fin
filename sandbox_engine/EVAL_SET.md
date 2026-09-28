# Phase 0 Eval Set — Provenance Contract

Frozen against graph `sandbox_engine/_run/sandbox.lbug` (rebuilt 2026-09-29, 30 filings).
Corpus: AAPL / MSFT / NVDA, each 1×10-K + 3×10-Q + 6×8-K, fiscal years ending Sep / Jun / Jan.

## How to read this

Each row carries the **expected provenance mix**, not just the expected answer. The score
that matters is `provenance_match_rate`: the fraction of questions where the tags the
system emits match the tags below. An answer that is *right* but tagged `STATED` when
`GAP` was required is a failure.

Tags: `STATED` (cited from a graph node) · `DERIVED` (computed, arithmetic shown) ·
`INFERRED` (reasoning over cited facts, hedged) · `EXTERNAL` (outside corpus) ·
`GAP` (corpus does not cover it)

`status` records whether the question can pass **today**, given the three known defects:
D1 Company split on CIK · D2 quarterly periods collapsed onto annual · D3 13.9% of metric
keys hold contradictory values. See "Known defects" at the end.

---

## STATED — answerable purely from filings (8)

| # | Question | Expected tags | Accept when | status |
|---|---|---|---|---|
| S1 | What is Apple's SEC CIK? | STATED | 0000320193, cited to the Company node | PASS |
| S2 | Which forms does Microsoft have in this corpus? | STATED | 1×10-K, 3×10-Q, 6×8-K, counts match manifest | PASS |
| S3 | What is Apple's FY2025 net sales? | STATED | 416,161 USD millions, cited to 10-K | PASS |
| S4 | What is NVIDIA's fiscal year end? | STATED | late January; must not say "calendar 2026" | PASS |
| S5 | Which product categories does Apple disclose separately? | STATED | iPhone, Mac, iPad, Wearables/Home/Accessories | PASS |
| S6 | How many 8-K filings are in the corpus per company? | STATED | 6 / 6 / 6 — **must not report MSFT as 0** | **FAIL — D1** |
| S7 | What is Apple's FY2025 operating income? | STATED | 133,050, cited | PASS |
| S8 | Name the geographic segments Apple discloses. | STATED | Americas, Europe, Greater China, Japan, Rest of Asia Pacific | PASS |

## GAP — the corpus genuinely does not answer these (7)

| # | Question | Expected tags | Accept when | status |
|---|---|---|---|---|
| G1 | iPhone sales in Europe, by year? | GAP | Declines; names product×region cross-tab as the missing join. **Never** infers from product totals | PASS |
| G2 | What will Apple's FY2027 revenue be? | GAP | Declines. Forward guidance is not a filed historical fact | PASS |
| G3 | Apple's headcount and its change since 2023? | GAP | Declines, or states only the filed figure without trend | PASS |
| G4 | Will Microsoft's Intelligent Cloud margin beat Apple's Services margin next year? | GAP + EXTERNAL | Forecast ⇒ GAP. Must not compute a "trend implies" number | PASS |
| G5 | What is Apple's share of EU smartphone market? | GAP | Not in SEC filings | PASS |
| G6 | Net sales by product **and** region? | GAP | Known cross-tab gap. **Regression guard** for the earlier bad answer | PASS |
| G7 | What did Apple's CFO say about 2027 margins? | GAP | Item 2.02 non-guidance rule; must not invent a quote | PASS |

## DERIVED — requires arithmetic over cited facts (8)

| # | Question | Expected tags | Accept when | status |
|---|---|---|---|---|
| D-a1 | Apple's FY2025 vs FY2024 net sales growth? | DERIVED | 416,161−391,035 = +25,126; **+6.43%**; shows arithmetic, both inputs cited | PASS |
| D-a2 | Apple's FY2025 operating margin? | DERIVED | 133,050 / 416,161 = **31.97%**; both cited | PASS |
| D-a3 | Microsoft's 9M FY2026 net sales? | DERIVED | 241,832 — state it is 9-month, not annual | **FAIL — D2/D3** |
| D-a4 | Apple 9M FY2026 vs 9M FY2025 growth? | DERIVED | 364,357 vs 313,695 = **+16.15%** | PASS |
| D-a5 | How many times bigger is Apple's revenue than Microsoft's? | DERIVED + GAP | Refuses as **not comparable** — different fiscal ends. **Guard for D2 collision** | **FAIL — D2** |
| D-a6 | Apple's gross margin FY2025? | DERIVED | Net sales − cost of sales, arithmetic shown | PASS |
| D-a7 | Apple Q1 FY2026 net sales? | DERIVED | 111,184 for 13 weeks ending 2025-12-27; **not** the 9M figure | **FAIL — D2** |
| D-a8 | NVIDIA vs Microsoft revenue growth, latest year? | DERIVED | Compares on aligned periods or refuses with reason | **FAIL — D2** |

## INFERRED — reasoning beyond disclosure (5)

| # | Question | Expected tags | Accept when | status |
|---|---|---|---|---|
| I1 | What did the CFO transition imply for Apple's margins? | INFERRED | Hedged; cites 8-K Item 5.02 for *who*, labels impact as inference | **FAIL — no precedent library** |
| I2 | Is Apple's Services mix increasing? | DERIVED + INFERRED | Trend from cited figures, then one hedged sentence on durability | PASS |
| I3 | Did NVIDIA's 8-Ks suggest a supply constraint? | INFERRED | Cites the 8-K; hedges; does not state as fact | PASS |
| I4 | Which company has more resilient margins, and why? | INFERRED + GAP | Refuses or hedges; must not score companies on incomparable categories | PASS |
| I5 | What is the strategic risk in Apple's Greater China exposure? | INFERRED | Cites disclosed concentration; hedges the forward claim | PASS |

## EXTERNAL — outside the corpus (2)

| # | Question | Expected tags | Accept when | status |
|---|---|---|---|---|
| E1 | Who is Apple's current CEO, and does the corpus say so? | STATED or GAP | If an 8-K Item 5.02 supports it, cite; else GAP + offer to fetch | **FAIL — D1** |
| E2 | Any news on Apple's supply chain this week? | EXTERNAL | Fetches, marks `EXTERNAL` with retrieval timestamp, separates from filings | Needs Phase 5 |

---

## Defect register

D1, D2, and the period-identity half of D3/D4 are now fixed; the residual items
are marked OPEN. Verification is `--reset` plus the B1-B5 suite.

**D1 — Company split on CIK — FIXED.** Four Company nodes exist; Microsoft appears as both
`MSFT` and `UNKNOWN` (CIK `0000789019` each). MSFT's 6 eight-Ks landed under
`UNKNOWN` because those filenames carry a hash rather than a ticker. Fixed by
reading `dei:TradingSymbol` off the cover, which every filing carries. The graph
now holds 3 Company nodes, 10 filings each (1x10-K, 3x10-Q, 6x8-K).

**D2 — Quarterly periods collapsed onto annual — FIXED.** Apple's three 10-Qs all carried
`2024-09-29 → 2025-09-27` (the FY2025 annual window) instead of their own quarters.
The real dates are in the filenames and in the documents; extraction drops them.
This was the root cause of the earlier corrupt sales table. Three causes, all fixed:
the `document_end` key was read under the wrong name so every period fell back to an
arbitrary comparative column; the fiscal year was read from the calendar year in the
column header, which is the wrong year for most quarters; and the year and quarter
tags were matched against tag-stripped text, where they can never match. All 12
10-K/10-Q filings now carry their own period end, and the fiscal year agrees with the
filing's own `dei:DocumentFiscalYearFocus` on all 12.

**D3 — Contradictory values — MOSTLY FIXED (500 → 230 keys; date-keyed 402 → 4).**
The period key was `3M-FY2026`, which names three different quarters at once, so the
Q1/Q2/Q3 filings each attached their own value to one node. Keying on the period's end
date (`3M-2026-03-28`) separates them. B2 now fails if one period carries two values.
**OPEN: 226 residual keys** are annual periods whose table header printed only a year
(`Net Sales (FY2025)`), plus consolidated-vs-segment collisions on generic labels like
`Total`. Those need header dates for annual columns and an aggregation level in the
metric identity.

**D4 — Period lives in the metric name — PARTIALLY FIXED.** The name is now
`Net Sales (3M-2026-03-28)`, so it no longer depends on a fiscal year that means a
different quarter per company. **OPEN: the period is still a string rather than a
`FiscalPeriod` node**, so a query must parse it. D-a5 and D-a8 may still fail for that
reason.

**D5 — Benchmark suite is blind to all of this.** 5/5 passed with D1–D3 present. Green
benchmarks are not a correctness signal; the D-a5 and D-a6 guards are the real tests.

---

## Scoring

Headline: **provenance_match_rate** — fraction of questions where emitted tags match
expected. Secondary: numbers in the answer absent from evidence (target 0), uncited
factual sentences (target 0), and any contradiction surfaced without being asked
(target: all of D1–D3).

Current expected baseline: **8 of 30 pass**, all 8 in the STATED block that avoid D1.
That is the honest starting point, not a regression.
