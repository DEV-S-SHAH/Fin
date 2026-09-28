#!/usr/bin/env python3
"""QA evaluation harness for the UFGS graph deployment.

Runs ~55 questions against the deployed ``/api/ask`` endpoint, grades each
answer against a ground-truth token (loaded from the LadybugDB database) when
one is known, and flags answers that claim the fact is absent. Output is a JSON
report plus a Markdown table written under ``sandbox_engine/_run/``.

Usage:
    .venv/bin/python -m sandbox_engine.qa_eval [--base http://127.0.0.1:9000]
"""

from __future__ import annotations

import argparse
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:9000"
OUT = Path(__file__).resolve().parent / "_run" / "qa_eval"

_NEGATION = re.compile(
    r"not (?:available|provided|found|included|described|stated|specified|covered|part of the context)"
    r"|cannot (?:be determined|answer)|can't (?:be determined|answer)|no information|out of (?:the )?context"
    r"|does not (?:mention|include|provide|contain|describe)|is not (?:present|included|described)"
    r"|not enough (?:information|context)|not in (?:the )?(?:context|corpus|database|graph)|unavailable"
    r"|no data|not part of|no record|nothing in the context|based on the context.*?(?:not|no )",
)

# (category, question, expected-token or None, is-negative-control)
QUESTIONS = [
    # -- single-hop financial facts --------------------------------------
    ("fact", "What were Apple's net sales for fiscal year 2025?", "416161", False),
    ("fact", "What was Apple's net income for fiscal year 2025?", "112010", False),
    ("fact", "What was Apple's operating income in fiscal year 2025?", "133050", False),
    ("fact", "What was Apple's research and development expense for fiscal year 2025?", "34550", False),
    ("fact", "What was Apple's operating cash flow in fiscal year 2025?", "111482", False),
    ("fact", "What was Apple's diluted earnings per share for fiscal year 2025?", "746", False),
    ("fact", "What were Apple's total assets as of September 27, 2025?", "359241", False),
    ("fact", "What were Apple's total liabilities as of September 27, 2025?", "285508", False),
    ("fact", "What was Apple's total shareholders' equity as of September 27, 2025?", "73733", False),
    ("fact", "How much revenue did Apple's iPhone bring in during fiscal year 2025?", "209586", False),
    ("fact", "How much revenue did Apple's Services segment generate in fiscal year 2025?", "109158", False),
    ("fact", "What were Apple's net sales in Greater China for fiscal year 2025?", "64377", False),
    ("fact", "What were Apple's net sales in the Americas for fiscal year 2025?", "178353", False),
    ("fact", "What were Apple's net sales in Europe for fiscal year 2025?", "111032", False),
    ("fact", "What were Apple's Mac net sales for fiscal year 2025?", "33708", False),
    ("fact", "What were Apple's iPad net sales for fiscal year 2025?", "28023", False),
    # -- quarterly facts ---------------------------------------------------
    ("fact", "What were Apple's net sales for the three months ended March 28, 2026 (Q2 FY2026)?", "111184", False),
    ("fact", "What were Apple's net sales for the three months ended June 27, 2026 (Q3 FY2026)?", "109417", False),
    ("fact", "What was Apple's net income for the three months ended June 27, 2026?", "29789", False),
    ("fact", "What was Apple's diluted EPS for the three months ended March 28, 2026?", "201", False),
    ("fact", "What was Apple's research and development expense for the three months ended June 27, 2026?", "11729", False),
    # -- multihop / relational ----------------------------------------------
    ("multihop", "Which product segment contributed the most to Apple's net sales in fiscal year 2025?", "iPhone", False),
    ("multihop", "Which geographic segment had the largest net sales for Apple in fiscal year 2025?", "Americas", False),
    ("multihop", "Services revenue was roughly what percentage of Apple's total net sales in fiscal year 2025?", "26", False),
    ("multihop", "What was Apple's operating margin in fiscal year 2025 (operating income as a percentage of net sales)?", "32", False),
    ("multihop", "Compare Q2 and Q3 of fiscal year 2026: which three-month period had higher net sales?", "Q2", False),
    ("multihop", "Which 8-K filing in the corpus reported Apple's fiscal third quarter 2026 results?", "July 30", False),
    ("multihop", "How many 8-K filings in the corpus disclose results of operations under Item 2.02?", "3", False),
    ("multihop", "Which 8-K filing concerns the transition of Tim Cook?", "Tim Cook", False),
    ("multihop", "Which form types are present in this corpus?", "10-Q", False),
    ("multihop", "Is Apple's operating cash flow for fiscal year 2025 larger or smaller than its net income?", "smaller", False),
    ("multihop", "Did Apple's three-month net sales grow between Q2 and Q3 of fiscal year 2026?", "declined", False),
    # -- structural / section taxonomy ---------------------------------------
    ("structural", "Which 10-K item contains Apple's risk factor disclosures?", "1A", False),
    ("structural", "Which 10-K item contains Apple's business overview?", "Item 1", False),
    ("structural", "Does Apple's FY2025 10-K include a cybersecurity disclosure item?", "1C", False),
    ("structural", "How many financial statement sections were extracted from Apple's 10-Q filings?", "4", False),
    ("structural", "What is the title of 10-K Item 7?", "Management Analysis", False),
    ("structural", "Does the corpus include a section for 'Management's Discussion and Analysis'?", "yes", False),
    # -- narrative / risk / causal -------------------------------------------
    ("narrative", "Does Apple's 10-K disclose competition as a risk to its business?", "compet", False),
    ("narrative", "Does Apple cite dependence on component suppliers and logistics as a risk factor?", "supplier", False),
    ("narrative", "Does Apple's risk factor narrative mention global economic or macro conditions?", "macro", False),
    ("causal", "Which causal relation type links suppliers to risk exposure in the extracted causal layer?", "CREATES_EXPOSURE", False),
    ("causal", "Does the causal extraction attribute revenue growth to competitive or customer factors?", "customer", False),
    ("causal", "Does Apple's narrative describe foreign currency exchange rate exposure?", "exchange rate", False),
    ("causal", "Which entity type is used for a named rival company in the causal layer?", "Competitor", False),
    # -- negative / out-of-context ---------------------------------------------
    ("negative", "What was NVIDIA's research and development expense for fiscal year 2025?", None, True),
    ("negative", "What was Tesla's revenue in 2025?", None, True),
    ("negative", "What were Apple's net sales for fiscal year 2018?", None, True),
    ("negative", "What was JPMorgan's net interest income?", None, True),
    ("negative", "How many employees did Apple have at the end of fiscal year 2025?", None, True),
    ("negative", "What dividend per share will Apple pay in fiscal year 2027?", None, True),
    ("negative", "What is Apple's current market capitalization?", None, True),
    ("negative", "What was Samsung's revenue in 2025?", None, True),
    ("negative", "What were Apple's quarterly results for fiscal year 2022?", None, True),
    ("negative", "What was ExxonMobil's total revenue?", None, True),
]


def norm(s: str) -> str:
    return re.sub(r"[^0-9a-z]", "", (s or "").lower())


def is_unanswerable(text: str) -> bool:
    low = text.lower()
    return bool(_NEGATION.search(low))


def grade(expect: str | None, negative: bool, text: str, grounded: bool) -> tuple[str, str]:
    """Return ``(verdict, note)``.

    Negative controls pass when the answer refuses to fabricate (ungrounded or
    explicitly says the fact is absent). Known-fact questions pass when the
    answer is grounded and contains the expected token.
    """
    low = text.lower()
    if negative:
        if grounded and not is_unanswerable(text):
            return "FAIL", "model answered an out-of-context fact"
        return "PASS", ("ungrounded refuse" if not grounded else "explicit absent")
    if not grounded:
        return "FAIL", "no citations"
    if expect is not None and norm(expect) not in norm(text):
        return "FAIL", f"missing {expect!r}"
    return "PASS", "matches"


def ask(base: str, question: str, timeout: int = 180) -> dict:
    body = json.dumps({"question": question}).encode()
    req = urllib.request.Request(
        base + "/api/ask", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            return json.loads(raw)
        except Exception:
            return {"error": f"HTTP {e.code}: {raw[:200]}"}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    for i, (cat, q, expect, negative) in enumerate(QUESTIONS, 1):
        t0 = time.perf_counter()
        resp = ask(args.base, q)
        took = time.perf_counter() - t0
        text = resp.get("text", "") or ""
        grounded = bool(resp.get("grounded"))
        verdict, note = grade(expect, negative, text, grounded)
        results.append({
            "n": i,
            "category": cat,
            "question": q,
            "expect": expect,
            "negative": negative,
            "verdict": verdict,
            "note": note,
            "grounded": grounded,
            "cited": resp.get("used_tags", []),
            "unanswerable": is_unanswerable(text),
            "elapsed": round(took, 1),
            "answer": text,
        })
        flag = "!" if verdict == "FAIL" else " "
        print(f"{i:>2}{flag} [{verdict:4}] {cat:10} {q[:70]}")
        if verdict == "FAIL" or negative:
            print(f"      -> grounded={grounded} note={note}")
            print(f"      -> {text[:160]}")
        time.sleep(0.5)

    passed = sum(1 for r in results if r["verdict"] == "PASS")
    stats = {
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "grounded_ratio": round(
            sum(1 for r in results if r["grounded"]) / len(results), 3
        ),
        "by_category": {},
    }
    for cat in sorted({r["category"] for r in results}):
        subset = [r for r in results if r["category"] == cat]
        ps = sum(1 for r in subset if r["verdict"] == "PASS")
        stats["by_category"][cat] = {"total": len(subset), "passed": ps}

    (OUT / "report.json").write_text(
        json.dumps({"stats": stats, "results": results}, indent=2, default=str),
        encoding="utf-8",
    )

    md = [
        "# UFGS QA Evaluation",
        "",
        f"- total: **{len(results)}**, passed: **{passed}**, failed: **{len(results)-passed}**",
        f"- grounded ratio: **{stats['grounded_ratio']}**",
        "",
        "| # | cat | verdict | grounded | note | question |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        md.append(
            f"| {r['n']} | {r['category']} | {r['verdict']} | {r['grounded']} | "
            f"{r['note']} | {r['question']} |"
        )
    (OUT / "report.md").write_text("\n".join(md), encoding="utf-8")

    print("\n==== STATS ====")
    print(json.dumps(stats, indent=2))
    print(f"report: {OUT / 'report.json'}")
    print(f"table : {OUT / 'report.md'}")


if __name__ == "__main__":
    main()