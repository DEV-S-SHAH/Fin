"""Verification suite for the blueprint LadybugDB graph (Step 5).

Runs 5 Cypher validation queries and prints a side-by-side comparison
report at http://127.0.0.1:9000/ (separate from the existing engine on :8765).

Queries
-------
V1  Multi-Year Annual Trajectory  — NetSales, GrossProfit, GrossMargin from 10-K
V2  Quarterly Trajectory          — NetSales per quarter with period_end_date
V3  Multi-Dimensional Segments    — Revenue disaggregated by Product / Geography
V4  Form 8-K Event Audit          — All item codes, titles, summaries
V5  Anti-Hallucination Safety     — FY2011 must return 0 rows

Run::

    source .venv/bin/activate
    python -m sandbox_engine.verify_sandbox            # CLI only (no server)
    python -m sandbox_engine.verify_sandbox --serve    # CLI + Flask on :9000
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import ladybug as lb

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("verify_sandbox")

_HERE = Path(__file__).resolve().parent
_DB_PATH = _HERE / "_run2" / "blueprint.lbug"
_REPORT_PATH = _HERE / "_run2" / "verify_report.json"

ABSENT_YEAR = 2011     # deliberately outside the 3-filing scope
SERVER_PORT  = 9000    # separate from existing engine on :8765


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class VerifyResult:
    key:     str
    name:    str
    passed:  bool
    rows:    list[list[Any]]
    columns: list[str]
    detail:  dict[str, Any] = field(default_factory=dict)
    error:   str = ""
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "key":     self.key,
            "name":    self.name,
            "passed":  self.passed,
            "rows":    self.rows,
            "columns": self.columns,
            "detail":  self.detail,
            "error":   self.error,
            "seconds": round(self.seconds, 4),
        }


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def _exec(conn: Any, cypher: str, params: dict | None = None) -> list[list[Any]]:
    result = conn.execute(cypher, params or {})
    return [list(row) for row in result.get_all()]


def _col_names(conn: Any, cypher: str, params: dict | None = None) -> tuple[list[list], list[str]]:
    result = conn.execute(cypher, params or {})
    rows = [list(r) for r in result.get_all()]
    try:
        cols = result.get_column_names()
    except Exception:
        cols = [f"col{i}" for i in range(len(rows[0]) if rows else 0)]
    return rows, cols


# ---------------------------------------------------------------------------
# V1 — Multi-Year Annual Trajectory (10-K)
# ---------------------------------------------------------------------------

def v1_annual_trajectory(conn: Any) -> VerifyResult:
    key = "V1"
    name = "Multi-Year Annual Trajectory (10-K)"
    t0 = time.perf_counter()
    try:
        # Find the 10-K filing accession
        filings = _exec(
            conn,
            "MATCH (f:Filing) WHERE f.form_type = '10-K' RETURN f.accession_number, f.fiscal_year"
        )
        if not filings:
            return VerifyResult(key=key, name=name, passed=False, rows=[], columns=[],
                                error="No 10-K filing found", seconds=time.perf_counter()-t0)

        acc = filings[0][0]
        fy  = filings[0][1]

        rows, cols = _col_names(conn, """
            MATCH (f:Filing)-[r:REPORTS_METRIC]->(m:FinancialMetric)
            WHERE f.accession_number = $acc
              AND (m.metric_id STARTS WITH 'NetSales'
                OR m.metric_id STARTS WITH 'GrossProfit'
                OR m.metric_id STARTS WITH 'OperatingIncome')
            RETURN m.canonical_name AS metric,
                   r.period_type    AS period,
                   r.value          AS value,
                   r.currency       AS currency
            ORDER BY m.canonical_name, r.period_type
        """, {"acc": acc})

        passed = len(rows) > 0
        return VerifyResult(
            key=key, name=name, passed=passed, rows=rows, columns=cols,
            detail={"10k_accession": acc, "fiscal_year": fy, "row_count": len(rows)},
            seconds=time.perf_counter()-t0,
        )
    except Exception as exc:
        return VerifyResult(key=key, name=name, passed=False, rows=[], columns=[],
                            error=str(exc), seconds=time.perf_counter()-t0)


# ---------------------------------------------------------------------------
# V2 — Quarterly Trajectory (10-Q)
# ---------------------------------------------------------------------------

def v2_quarterly_trajectory(conn: Any) -> VerifyResult:
    key = "V2"
    name = "Quarterly Trajectory (10-Q)"
    t0 = time.perf_counter()
    try:
        filings = _exec(conn,
            "MATCH (f:Filing) WHERE f.form_type = '10-Q' "
            "RETURN f.accession_number, f.fiscal_year, f.period_end_date ORDER BY f.fiscal_year"
        )
        if not filings:
            return VerifyResult(key=key, name=name, passed=False, rows=[], columns=[],
                                error="No 10-Q filing found", seconds=time.perf_counter()-t0)

        acc = filings[-1][0]
        fy  = filings[-1][1]
        ped = filings[-1][2]

        rows, cols = _col_names(conn, """
            MATCH (f:Filing)-[r:REPORTS_METRIC]->(m:FinancialMetric)
            WHERE f.accession_number = $acc
              AND m.metric_id STARTS WITH 'NetSales'
            RETURN m.canonical_name AS metric,
                   r.period_type    AS period_type,
                   r.value          AS value,
                   r.currency       AS currency
            ORDER BY r.period_type
        """, {"acc": acc})

        passed = len(rows) > 0
        return VerifyResult(
            key=key, name=name, passed=passed, rows=rows, columns=cols,
            detail={
                "10q_accession":  acc,
                "fiscal_year":    fy,
                "period_end_date": str(ped),
                "row_count":      len(rows),
            },
            seconds=time.perf_counter()-t0,
        )
    except Exception as exc:
        return VerifyResult(key=key, name=name, passed=False, rows=[], columns=[],
                            error=str(exc), seconds=time.perf_counter()-t0)


# ---------------------------------------------------------------------------
# V3 — Multi-Dimensional Segments
# ---------------------------------------------------------------------------

def v3_segment_fanout(conn: Any) -> VerifyResult:
    key = "V3"
    name = "Multi-Dimensional Segments (Product + Geography)"
    t0 = time.perf_counter()
    try:
        rows, cols = _col_names(conn, """
            MATCH (m:FinancialMetric)-[d:DISAGGREGATED_BY]->(s:Segment)
            RETURN s.dimension_name AS segment,
                   s.dimension_type AS dim_type,
                   d.fiscal_year    AS fiscal_year,
                   d.fiscal_period  AS period,
                   d.value          AS value
            ORDER BY s.dimension_type, s.dimension_name, d.fiscal_year
            LIMIT 50
        """)

        dim_types = list({r[1] for r in rows}) if rows else []
        passed = len(rows) > 0
        return VerifyResult(
            key=key, name=name, passed=passed, rows=rows, columns=cols,
            detail={
                "segment_count": len(rows),
                "dimension_types": sorted(dim_types),
            },
            seconds=time.perf_counter()-t0,
        )
    except Exception as exc:
        return VerifyResult(key=key, name=name, passed=False, rows=[], columns=[],
                            error=str(exc), seconds=time.perf_counter()-t0)


# ---------------------------------------------------------------------------
# V4 — Form 8-K Event Audit
# ---------------------------------------------------------------------------

def v4_event_audit(conn: Any) -> VerifyResult:
    key = "V4"
    name = "Form 8-K Event Audit"
    t0 = time.perf_counter()
    try:
        # 8-K filing
        filings = _exec(conn,
            "MATCH (f:Filing) WHERE f.form_type = '8-K' RETURN f.accession_number"
        )
        if not filings:
            return VerifyResult(key=key, name=name, passed=False, rows=[], columns=[],
                                error="No 8-K filing found", seconds=time.perf_counter()-t0)

        acc = filings[0][0]

        rows, cols = _col_names(conn, """
            MATCH (f:Filing)-[:DISCLOSES_EVENT]->(e:DisclosureEvent)
            WHERE f.accession_number = $acc
            RETURN e.item_code  AS item_code,
                   e.item_title AS item_title,
                   e.event_date AS event_date,
                   e.summary    AS summary
            ORDER BY e.item_code
        """, {"acc": acc})

        passed = len(rows) > 0
        return VerifyResult(
            key=key, name=name, passed=passed, rows=rows, columns=cols,
            detail={
                "8k_accession": acc,
                "event_count":  len(rows),
                "item_codes":   sorted({r[0] for r in rows}),
            },
            seconds=time.perf_counter()-t0,
        )
    except Exception as exc:
        return VerifyResult(key=key, name=name, passed=False, rows=[], columns=[],
                            error=str(exc), seconds=time.perf_counter()-t0)


# ---------------------------------------------------------------------------
# V5 — Anti-Hallucination Safety Check (FY2011 must be absent)
# ---------------------------------------------------------------------------

def v5_anti_hallucination(conn: Any) -> VerifyResult:
    key = "V5"
    name = f"Anti-Hallucination Safety — FY{ABSENT_YEAR} must return 0 rows"
    t0 = time.perf_counter()
    try:
        rows, cols = _col_names(conn, """
            MATCH (f:Filing)
            WHERE f.fiscal_year = $yr
            RETURN f.accession_number, f.form_type, f.fiscal_year
        """, {"yr": ABSENT_YEAR})

        count = len(rows)
        passed = count == 0
        return VerifyResult(
            key=key, name=name, passed=passed, rows=rows, columns=cols,
            detail={
                "absent_year":   ABSENT_YEAR,
                "rows_returned": count,
                "verdict":       "SAFE — no hallucinated rows" if passed
                                 else f"UNSAFE — {count} spurious rows found",
            },
            seconds=time.perf_counter()-t0,
        )
    except Exception as exc:
        return VerifyResult(key=key, name=name, passed=False, rows=[], columns=[],
                            error=str(exc), seconds=time.perf_counter()-t0)


# ---------------------------------------------------------------------------
# Run all
# ---------------------------------------------------------------------------

def run_all() -> list[VerifyResult]:
    if not _DB_PATH.exists():
        log.error("Database not found at %s — run ingest_sandbox first.", _DB_PATH)
        sys.exit(1)

    db   = lb.Database(str(_DB_PATH))
    conn = lb.Connection(db)
    try:
        results = [
            v1_annual_trajectory(conn),
            v2_quarterly_trajectory(conn),
            v3_segment_fanout(conn),
            v4_event_audit(conn),
            v5_anti_hallucination(conn),
        ]
    finally:
        conn.close()
        db.close()

    return results


def _print_results(results: list[VerifyResult]) -> None:
    passed = sum(1 for r in results if r.passed)
    print(f"\n{'═'*70}")
    print(f"  Verification Suite — {passed}/{len(results)} passed")
    print(f"{'═'*70}")

    for r in results:
        icon = "✅" if r.passed else "❌"
        print(f"\n{icon} [{r.key}] {r.name}  ({r.seconds*1000:.1f}ms)")
        if r.error:
            print(f"    ERROR: {r.error}")
            continue
        if r.detail:
            for k, v in r.detail.items():
                print(f"    {k}: {v}")
        if r.rows:
            # Print header
            col_w = 28
            header = "  ".join(f"{c[:col_w]:<{col_w}}" for c in r.columns)
            print(f"\n    {'─'*min(len(header)+4, 100)}")
            print(f"    {header}")
            print(f"    {'─'*min(len(header)+4, 100)}")
            for row in r.rows[:15]:
                line = "  ".join(f"{str(v)[:col_w]:<{col_w}}" for v in row)
                print(f"    {line}")
            if len(r.rows) > 15:
                print(f"    … ({len(r.rows)-15} more rows)")
        else:
            print("    (0 rows returned)")

    print(f"\n{'═'*70}\n")


# ---------------------------------------------------------------------------
# Flask web server (port 9000)
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sandbox Engine v2 — Blueprint Verification</title>
<style>
  :root{{--bg:#0f1117;--card:#1a1d27;--accent:#4f8ef7;--green:#22c55e;--red:#ef4444;--muted:#8892a4;--border:#2d3244}}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:var(--bg);color:#e2e8f0;font-family:'Inter',system-ui,sans-serif;padding:24px}}
  h1{{font-size:1.6rem;font-weight:700;color:var(--accent);margin-bottom:4px}}
  .subtitle{{color:var(--muted);font-size:.85rem;margin-bottom:24px}}
  .badge{{display:inline-block;padding:2px 10px;border-radius:12px;font-size:.8rem;font-weight:600}}
  .pass{{background:#14532d;color:var(--green)}}
  .fail{{background:#450a0a;color:var(--red)}}
  .summary{{display:flex;gap:16px;margin-bottom:24px;flex-wrap:wrap}}
  .stat{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 20px;min-width:140px}}
  .stat h3{{font-size:.75rem;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px}}
  .stat .val{{font-size:1.8rem;font-weight:700}}
  .green{{color:var(--green)}}
  .red{{color:var(--red)}}
  .card{{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:20px;margin-bottom:16px}}
  .card-header{{display:flex;align-items:center;gap:10px;margin-bottom:14px}}
  .card-header .key{{background:#1e2d5a;color:var(--accent);border-radius:6px;padding:3px 10px;font-size:.8rem;font-weight:700}}
  .card-header h2{{font-size:1rem;font-weight:600}}
  .timing{{margin-left:auto;color:var(--muted);font-size:.8rem}}
  .detail{{font-size:.82rem;color:var(--muted);margin-bottom:10px;line-height:1.7}}
  table{{width:100%;border-collapse:collapse;font-size:.82rem}}
  th{{background:#232840;color:var(--accent);padding:7px 12px;text-align:left;font-weight:600;border-bottom:1px solid var(--border)}}
  td{{padding:6px 12px;border-bottom:1px solid var(--border);color:#cbd5e1}}
  tr:last-child td{{border-bottom:none}}
  tr:hover td{{background:#1e2233}}
  .empty{{color:var(--muted);font-style:italic;font-size:.84rem;padding:8px 0}}
  .error-box{{background:#2d0f0f;border:1px solid var(--red);border-radius:8px;padding:12px;color:#fca5a5;font-size:.84rem}}
  .port-note{{background:#1a2a1a;border:1px solid #22c55e33;border-radius:8px;padding:10px 16px;margin-bottom:20px;font-size:.82rem;color:#86efac}}
</style>
</head>
<body>
<h1>🔬 Sandbox Engine v2 &mdash; Blueprint Verification</h1>
<p class="subtitle">Serving on port {port} &nbsp;·&nbsp; DB: {db} &nbsp;·&nbsp; {ts}</p>
<div class="port-note">
  ℹ️ This server runs on <strong>:{port}</strong>. Compare with the original engine on its port to see both pipelines side-by-side.
</div>
<div class="summary">
  <div class="stat"><h3>Passed</h3><div class="val green">{passed}</div></div>
  <div class="stat"><h3>Failed</h3><div class="val red">{failed}</div></div>
  <div class="stat"><h3>Total</h3><div class="val">{total}</div></div>
  <div class="stat"><h3>Total Time</h3><div class="val" style="font-size:1.3rem">{elapsed}ms</div></div>
</div>
{cards}
</body>
</html>"""

_CARD_TMPL = """<div class="card">
<div class="card-header">
  <span class="key">{key}</span>
  <h2>{name}</h2>
  <span class="badge {cls}">{verdict}</span>
  <span class="timing">{ms}ms</span>
</div>
{body}
</div>"""


def _render_card(r: VerifyResult) -> str:
    cls = "pass" if r.passed else "fail"
    verdict = "PASS" if r.passed else "FAIL"
    ms = f"{r.seconds*1000:.1f}"

    if r.error:
        body = f'<div class="error-box">ERROR: {r.error}</div>'
    else:
        # Detail section
        detail_lines = "".join(f"<b>{k}:</b> {v}<br>" for k, v in r.detail.items())
        detail = f'<div class="detail">{detail_lines}</div>' if detail_lines else ""

        # Table
        if r.rows:
            th = "".join(f"<th>{c}</th>" for c in r.columns)
            tbody = ""
            for row in r.rows[:20]:
                cells = "".join(f"<td>{str(v)[:80]}</td>" for v in row)
                tbody += f"<tr>{cells}</tr>"
            if len(r.rows) > 20:
                tbody += f"<tr><td colspan='{len(r.columns)}' style='color:var(--muted)'>… {len(r.rows)-20} more rows</td></tr>"
            table = f"<table><thead><tr>{th}</tr></thead><tbody>{tbody}</tbody></table>"
        else:
            table = '<div class="empty">No rows returned.</div>'

        body = detail + table

    return _CARD_TMPL.format(key=r.key, name=r.name, cls=cls, verdict=verdict, ms=ms, body=body)


def start_server(results: list[VerifyResult], port: int = SERVER_PORT) -> None:
    try:
        from flask import Flask, jsonify
    except ImportError:
        log.error("Flask not installed. Install with: pip install flask")
        return

    import datetime

    app = Flask(__name__)

    @app.route("/")
    def index():
        from flask import Response
        passed = sum(1 for r in results if r.passed)
        failed = len(results) - passed
        total_ms = sum(r.seconds * 1000 for r in results)
        cards = "\n".join(_render_card(r) for r in results)
        html = _HTML_TEMPLATE.format(
            port=port,
            db=str(_DB_PATH),
            ts=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            passed=passed,
            failed=failed,
            total=len(results),
            elapsed=f"{total_ms:.0f}",
            cards=cards,
        )
        return Response(html, content_type="text/html")

    @app.route("/api/results")
    def api_results():
        return jsonify([r.as_dict() for r in results])

    @app.route("/api/rerun")
    def api_rerun():
        fresh = run_all()
        results.clear()
        results.extend(fresh)
        _REPORT_PATH.write_text(json.dumps([r.as_dict() for r in results], indent=2, default=str))
        return jsonify({"status": "ok", "passed": sum(1 for r in results if r.passed)})

    log.info("Starting Flask server on http://127.0.0.1:%d/", port)
    log.info("API:  GET /api/results   GET /api/rerun")
    log.info("Compare with original engine on its separate port.")
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m sandbox_engine.verify_sandbox",
        description="Blueprint verification: 5 Cypher queries with hard assertions",
    )
    parser.add_argument("--serve", action="store_true",
                        help=f"start Flask server on :{SERVER_PORT} after running queries")
    parser.add_argument("--port",  type=int, default=SERVER_PORT,
                        help=f"override server port (default: {SERVER_PORT})")
    args = parser.parse_args()

    results = run_all()
    _print_results(results)

    _REPORT_PATH.write_text(
        json.dumps([r.as_dict() for r in results], indent=2, default=str)
    )
    log.info("report → %s", _REPORT_PATH)

    if args.serve:
        start_server(results, port=args.port)


if __name__ == "__main__":
    _main()
