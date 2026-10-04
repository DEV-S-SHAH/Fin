"""Legacy GraphRAG UI server — extracted from sandbox_engine.query_ui.

This module provides the HTTP server for the legacy three-pane GraphRAG viewer
(port 9000). It subclasses the handler from the original query_ui module
so the graph queries, the RAG pipeline, the grader and the SSE stream are
the *same code paths*.

Run:
    python -m ui.legacy_graphrag
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import parse_qs, urlparse

import ladybug as lb

# Import backend from sandbox_engine
from sandbox_engine.query_ui import (
    KnowledgeGraph,
    BackgroundIngestQueue,
    CANNED_REPORTS,
    OLLAMA_MODEL,
    RAG_TIMEOUT,
    MAX_BODY,
    _int_param,
    _listeners,
    parse_ports,
    resolve_db_path,
    get_backends,
    run_report,
    _setting,
    _DB_CANDIDATES,
)

# Vendor assets served under /vendor/
_VENDOR_DIR = Path(__file__).resolve().parent / "static"
VENDOR: dict[str, bytes] = {}
for _vendor_name in ("d3.v7.min.js", "gsap.min.js"):
    _vendor_path = _VENDOR_DIR / _vendor_name
    if _vendor_path.is_file():
        VENDOR[_vendor_name] = _vendor_path.read_bytes()

log = logging.getLogger("legacy_graphrag_ui")

UI_PORT_ENV = "PORT_QUERY_UI"
DEFAULT_UI_PORT = 9000


# The HTML template from the original query_ui.py
_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>graphrag viewer</title>
<style>
  :root {
    --bg: #0f1115;
    --panel: #171a21;
    --panel-2: #1e222b;
    --line: #2a2f3a;
    --text: #e6e8ee;
    --muted: #9aa3b2;
    --accent: #6ea8fe;
    --accent-2: #7ee0b8;
    --warn: #ffb454;
    --bad: #ff7b72;
    --radius: 8px;
  }
  * { box-sizing: border-box; margin:0; padding:0; }
  html, body { height: 100%; margin: 0; overflow:hidden; }
  body {
    background: var(--bg);
    color: var(--text);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: flex;
    flex-direction: column;
  }
  header {
    display: flex; align-items: center; gap: 16px;
    padding: 10px 16px; background: var(--panel);
    border-bottom: 1px solid var(--line); flex: 0 0 auto;
  }
  header h1 { font-size: 15px; margin: 0; font-weight: 600; letter-spacing: .2px; }
  header .stats { color: var(--muted); font-size: 12px; }
  header .spacer { flex: 1; }
  header select {
    background: var(--panel-2); color: var(--text); border: 1px solid var(--line);
    border-radius: 6px; padding: 4px 8px; font: inherit; outline: none;
  }

  main { flex: 1 1 auto; display: flex; min-height: 0; overflow: hidden; }

  /* ---- left: entity browser ---- */
  aside {
    width: 290px; flex: 0 0 290px; background: var(--panel);
    border-right: 1px solid var(--line);
    display: flex; flex-direction: column; min-height: 0;
  }
  aside .pane { padding: 10px 12px; border-bottom: 1px solid var(--line); }
  label.field { display: block; font-size: 11px; text-transform: uppercase;
    letter-spacing: .6px; color: var(--muted); margin-bottom: 6px; }
  input[type=search], input[type=text], textarea, select {
    width: 100%; background: var(--panel-2); color: var(--text);
    border: 1px solid var(--line); border-radius: 6px; padding: 7px 9px;
    font: inherit; outline: none;
  }
  input[type=search]:focus, input[type=text]:focus, textarea:focus, select:focus {
    border-color: var(--accent);
  }
  .entity-list { flex: 1 1 auto; overflow-y: auto; padding: 8px 10px; }
  .entity-item {
    display: flex; align-items: center; gap: 8px; padding: 6px 8px;
    border-radius: 6px; cursor: pointer; user-select: none;
  }
  .entity-item:hover { background: var(--panel-2); }
  .entity-item.selected { background: var(--accent); color: var(--bg); }
  .entity-item .type { font-size: 10px; text-transform: uppercase; letter-spacing: .5px; }
  .entity-item .name { flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  /* ---- centre: graph ---- */
  #graph { flex: 1 1 auto; position: relative; background: var(--bg); }
  #graph canvas { width: 100%; height: 100%; display: block; }
  .toast {
    position: absolute; left: 50%; transform: translateX(-50%);
    bottom: 24px; background: var(--panel-2); color: var(--text);
    padding: 8px 16px; border-radius: 6px; border: 1px solid var(--line);
    font-size: 13px; box-shadow: 0 4px 16px rgba(0,0,0,.4);
    opacity: 0; transition: opacity .2s; pointer-events: none; z-index: 10;
  }
  .toast.show { opacity: 1; }

  /* ---- right: question/answer ---- */
  section {
    width: 380px; flex: 0 0 380px; background: var(--panel);
    border-left: 1px solid var(--line);
    display: flex; flex-direction: column; min-height: 0;
  }
  section header { padding: 12px 14px; border-bottom: 1px solid var(--line); }
  section h2 { font-size: 14px; font-weight: 600; }
  .qa { flex: 1 1 auto; overflow-y: auto; padding: 12px 14px; display: flex; flex-direction: column; gap: 12px; }
  .qa textarea { width: 100%; min-height: 72px; background: var(--panel-2); color: var(--text);
    border: 1px solid var(--line); border-radius: 6px; padding: 8px 10px; font: inherit;
    outline: none; resize: vertical; }
  .qa textarea:focus { border-color: var(--accent); }
  .qa button { align-self: flex-end; background: var(--accent); color: var(--bg);
    border: none; border-radius: 6px; padding: 8px 16px; font: inherit; font-weight: 600; cursor: pointer; }
  .qa button:disabled { opacity: .5; cursor: not-allowed; }
  .qa .answer { font-size: 13.5px; line-height: 1.6; white-space: pre-wrap; }
  .qa .answer .cite { color: var(--accent-2); text-decoration: underline; cursor: pointer; }
  .qa .answer .cite:hover { color: var(--accent); }
  .qa .meta { font-size: 11px; color: var(--muted); display: flex; gap: 12px; flex-wrap: wrap; }
  .qa .error { color: var(--bad); }
  .qa .sources { font-size: 12px; color: var(--muted); margin-top: 8px; }
  .qa .sources a { color: var(--accent); }
  .qa .source-item { margin: 4px 0; }
  .qa .evidence { font-size: 12px; background: var(--panel-2); border-radius: 6px; padding: 8px; margin: 4px 0; }
  .qa .evidence .tag { font-size: 10px; background: var(--accent); color: var(--bg); padding: 1px 5px; border-radius: 3px; margin-right: 6px; }

  /* --- controls inside graph --- */
  .graph-controls {
    position: absolute; top: 12px; right: 12px; z-index: 5;
    display: flex; flex-direction: column; gap: 6px;
  }
  .graph-controls button {
    background: var(--panel-2); color: var(--text); border: 1px solid var(--line);
    border-radius: 6px; padding: 6px 10px; font: 12px inherit; cursor: pointer;
  }
  .graph-controls button:hover { border-color: var(--accent); }
  .graph-controls label { font-size: 11px; color: var(--muted); display: flex; align-items: center; gap: 6px; }
  .graph-controls input[type=range] { width: 120px; }
</style>
</head>
<body>
<header>
  <h1>GraphRAG Viewer</h1>
  <span class="spacer"></span>
  <span class="stats" id="stats">— entities, — relationships</span>
  <label><input type="checkbox" id="labels" checked> Labels</label>
  <label><input type="checkbox" id="legend" checked> Legend</label>
</header>
<main>
  <aside>
    <div class="pane">
      <label class="field" for="entity-search">Entities</label>
      <input type="search" id="entity-search" placeholder="Filter entities…" autocomplete="off">
    </div>
    <div class="pane">
      <label class="field">Type filter</label>
      <div id="type-filters"></div>
    </div>
    <div class="entity-list" id="entity-list" role="list" aria-label="Entities in graph"></div>
  </aside>
  <div id="graph">
    <canvas id="canvas"></canvas>
    <div class="toast" id="toast" role="status" aria-live="polite"></div>
    <div class="graph-controls">
      <button id="fit">Fit</button>
      <button id="relayout">Relayout</button>
      <label>Depth <input type="range" id="depth" min="1" max="3" value="2"></label>
      <label>Charge <input type="range" id="charge" min="-800" max="-50" value="-200"></label>
    </div>
  </div>
  <section>
    <header><h2>Ask the Graph</h2></header>
    <div class="qa">
      <textarea id="question" placeholder="What was Apple's total net sales in fiscal 2025?"></textarea>
      <button id="ask" type="button">Ask</button>
      <div class="answer" id="answer" aria-live="polite"></div>
      <div class="meta" id="meta"></div>
      <div class="sources" id="sources"></div>
    </div>
  </section>
</main>
<script src="/vendor/d3.v7.min.js"></script>
<script src="/vendor/gsap.min.js"></script>
<script>
// --- entity list & type filters ---
const entityList = document.getElementById("entity-list");
const typeFilters = document.getElementById("type-filters");
const question = document.getElementById("question");
const askBtn = document.getElementById("ask");
const answer = document.getElementById("answer");
const meta = document.getElementById("meta");
const sources = document.getElementById("sources");
const statsEl = document.getElementById("stats");
const canvas = document.getElementById("canvas");
const ctx = canvas.getContext("2d");
const toast = document.getElementById("toast");
const fitBtn = document.getElementById("fit");
const relayoutBtn = document.getElementById("relayout");
const depthSel = document.getElementById("depth");
const chargeSel = document.getElementById("charge");
const labelsCb = document.getElementById("labels");
const legendCb = document.getElementById("legend");

let entities = [];
let selectedEntity = null;
let graphData = { nodes: [], links: [] };
let simulation = null;
let showLabels = true;
let showLegend = true;

// ---- toast ----
function showToast(msg) {
  toast.textContent = msg;
  toast.classList.add("show");
  setTimeout(() => toast.classList.remove("show"), 2500);
}

// ---- fetch stats ----
async function fetchStats() {
  try {
    const res = await fetch("/api/stats");
    const data = await res.json();
    statsEl.textContent = `${data.nodes} entities, ${data.edges} relationships`;
  } catch (e) {
    statsEl.textContent = "stats unavailable";
  }
}

// ---- fetch entities ----
async function fetchEntities(q = "") {
  try {
    const res = await fetch(`/api/entities?q=${encodeURIComponent(q)}&limit=500`);
    const data = await res.json();
    entities = data.entities;
    renderEntityList();
    renderTypeFilters();
  } catch (e) {
    entityList.innerHTML = "<div style='padding:10px;color:var(--bad)'>Failed to load entities</div>";
  }
}

// ---- render entity list ----
function renderEntityList() {
  entityList.innerHTML = "";
  for (const e of entities) {
    const div = document.createElement("div");
    div.className = "entity-item";
    div.dataset.id = e.id;
    if (e.id === selectedEntity) div.classList.add("selected");
    div.innerHTML = `<span class="type">${e.type}</span><span class="name">${e.name}</span>`;
    div.addEventListener("click", () => selectEntity(e.id));
    entityList.appendChild(div);
  }
}

// ---- render type filters ----
const typeColors = {
  Company: "#6ea8fe", Filing: "#7ee0b8", FinancialMetric: "#ffb454",
  Segment: "#a78bfa", DisclosureEvent: "#f472b6", DocumentChunk: "#34d399"
};
function renderTypeFilters() {
  const counts = new Map();
  for (const e of entities) {
    counts.set(e.type, (counts.get(e.type) || 0) + 1);
  }
  typeFilters.innerHTML = "";
  for (const [type, count] of counts) {
    const label = document.createElement("label");
    label.innerHTML = `<input type="checkbox" checked data-type="${type}"> <span style="color:${typeColors[type] || 'var(--accent)'}">${type}</span> <span style="color:var(--muted)">${count}</span>`;
    label.querySelector("input").addEventListener("change", (ev) => {
      filterByType(type, ev.target.checked);
    });
    typeFilters.appendChild(label);
  }
}
let hiddenTypes = new Set();
function filterByType(type, show) {
  if (show) hiddenTypes.delete(type);
  else hiddenTypes.add(type);
  renderEntityList();
  fetchGraph();
}

// ---- select entity ----
function selectEntity(id) {
  selectedEntity = id;
  document.querySelectorAll(".entity-item").forEach(el => {
    el.classList.toggle("selected", el.dataset.id === id);
  });
  fetchGraph([id]);
}

// ---- graph simulation ----
function initGraph() {
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  canvas.width = w * devicePixelRatio;
  canvas.height = h * devicePixelRatio;
  ctx.scale(devicePixelRatio, devicePixelRatio);
}

function tick() {
  ctx.clearRect(0, 0, canvas.clientWidth, canvas.clientHeight);
  if (!showLabels) {
    // draw links only
    ctx.strokeStyle = "rgba(154,163,178,0.15)";
    ctx.lineWidth = 1;
    for (const link of graphData.links) {
      const s = graphData.nodes.find(n => n.id === link.source);
      const t = graphData.nodes.find(n => n.id === link.target);
      if (s && t) {
        ctx.beginPath();
        ctx.moveTo(s.x, s.y);
        ctx.lineTo(t.x, t.y);
        ctx.stroke();
      }
    }
    // draw nodes
    for (const node of graphData.nodes) {
      ctx.fillStyle = typeColors[node.type] || "#6ea8fe";
      ctx.beginPath();
      ctx.arc(node.x, node.y, 8, 0, Math.PI * 2);
      ctx.fill();
    }
    return;
  }
  // draw links
  ctx.strokeStyle = "rgba(154,163,178,0.15)";
  ctx.lineWidth = 1;
  for (const link of graphData.links) {
    const s = graphData.nodes.find(n => n.id === link.source);
    const t = graphData.nodes.find(n => n.id === link.target);
    if (s && t) {
      ctx.beginPath();
      ctx.moveTo(s.x, s.y);
      ctx.lineTo(t.x, t.y);
      ctx.stroke();
    }
  }
  // draw nodes
  for (const node of graphData.nodes) {
    const isSelected = node.id === selectedEntity;
    ctx.fillStyle = isSelected ? "#ffd600" : (typeColors[node.type] || "#6ea8fe");
    ctx.beginPath();
    ctx.arc(node.x, node.y, isSelected ? 10 : 8, 0, Math.PI * 2);
    ctx.fill();
    if (isSelected) {
      ctx.strokeStyle = "#ffd600";
      ctx.lineWidth = 2;
      ctx.stroke();
    }
    // label
    ctx.fillStyle = "var(--text)";
    ctx.font = "11px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(node.name, node.x, node.y - 14);
  }
  // legend
  if (showLegend) {
    const types = [...new Set(graphData.nodes.map(n => n.type))];
    let y = 20;
    ctx.font = "11px sans-serif";
    ctx.textAlign = "left";
    for (const type of types) {
      ctx.fillStyle = typeColors[type] || "#6ea8fe";
      ctx.fillRect(14, y, 10, 10);
      ctx.fillStyle = "var(--text)";
      ctx.fillText(type, 28, y + 8);
      y += 18;
    }
  }
}

async function fetchGraph(seeds = []) {
  const hops = parseInt(depthSel.value, 10);
  const limit = 500;
  const qs = seeds.length ? `?seed=${encodeURIComponent(seeds.join(","))}&hops=${hops}&limit=${limit}` : `?hops=${hops}&limit=${limit}`;
  try {
    const res = await fetch(`/api/graph${qs}`);
    const data = await res.json();
    graphData = data;
    if (!simulation) {
      initSimulation();
    } else {
      simulation.nodes(graphData.nodes);
      simulation.force("link").links(graphData.links);
      simulation.alpha(0.3).restart();
    }
  } catch (e) {
    showToast("Failed to load graph");
  }
}

function initSimulation() {
  simulation = d3.forceSimulation(graphData.nodes)
    .force("link", d3.forceLink(graphData.links).id(d => d.id).distance(80).strength(0.5))
    .force("charge", d3.forceManyBody().strength(-200))
    .force("center", d3.forceCenter(canvas.clientWidth / 2, canvas.clientHeight / 2))
    .on("tick", tick);
}

function resizeCanvas() {
  initGraph();
  if (simulation) {
    simulation.force("center", d3.forceCenter(canvas.clientWidth / 2, canvas.clientHeight / 2));
  }
}

// ---- ask question ----
async function askQuestion() {
  const q = question.value.trim();
  if (!q) return;
  askBtn.disabled = true;
  askBtn.textContent = "Thinking…";
  answer.innerHTML = "";
  meta.textContent = "";
  sources.innerHTML = "";
  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: q })
    });
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let fullAnswer = "";
    let citations = [];
    let route = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n\n");
      buffer = lines.pop() || "";
      for (const line of lines) {
        if (!line.startsWith("data:")) continue;
        try {
          const data = JSON.parse(line.slice(5).trim());
          if (data.event === "token") {
            fullAnswer += data.token;
            answer.textContent = fullAnswer;
          } else if (data.event === "done") {
            if (data.citations) citations = data.citations;
            if (data.route) route = data.route;
            renderAnswer(fullAnswer, citations, route);
          } else if (data.event === "error") {
            answer.innerHTML = `<span class="error">Error: ${data.message}</span>`;
          }
        } catch (e) {}
      }
    }
  } catch (e) {
    answer.innerHTML = `<span class="error">Error: ${e.message}</span>`;
  } finally {
    askBtn.disabled = false;
    askBtn.textContent = "Ask";
  }
}

function renderAnswer(text, citations, route) {
  answer.textContent = text;
  meta.innerHTML = `Route: <strong>${route || "unknown"}</strong> | Citations: <strong>${citations.length}</strong>`;
  sources.innerHTML = "";
  if (citations.length) {
    const h3 = document.createElement("h4");
    h3.textContent = "Sources";
    sources.appendChild(h3);
    for (const c of citations) {
      const div = document.createElement("div");
      div.className = "source-item";
      const a = document.createElement("a");
      a.href = "#";
      a.textContent = `${c.type} — ${c.name}`;
      a.addEventListener("click", (ev) => {
        ev.preventDefault();
        showToast(`Citation: ${c.type} — ${c.name}`);
      });
      div.appendChild(a);
      sources.appendChild(div);
    }
  }
}

// ---- event listeners ----
askBtn.addEventListener("click", askQuestion);
question.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    askQuestion();
  }
});
fitBtn.addEventListener("click", () => {
  if (simulation) simulation.alpha(0.5).restart();
});
relayoutBtn.addEventListener("click", () => {
  if (simulation) simulation.alpha(0.8).restart();
});
depthSel.addEventListener("change", () => {
  if (selectedEntity) fetchGraph([selectedEntity]);
  else fetchGraph();
});
chargeSel.addEventListener("input", () => {
  if (simulation) simulation.force("charge").strength(parseInt(chargeSel.value, 10));
});
labelsCb.addEventListener("change", () => {
  showLabels = labelsCb.checked;
});
legendCb.addEventListener("change", () => {
  showLegend = legendCb.checked;
});

// entity search
document.getElementById("entity-search").addEventListener("input", (e) => {
  fetchEntities(e.target.value);
});

// resize
window.addEventListener("resize", () => {
  resizeCanvas();
});

// init
fetchStats();
fetchEntities();
fetchGraph();
window.addEventListener("load", () => {
  setTimeout(resizeCanvas, 100);
});
</script>
</body>
</html>"""


# ── HTTP Server Request Handler ───────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    server_version = "blueprint-graphrag-ui"
    protocol_version = "HTTP/1.1"

    kg: KnowledgeGraph

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s — %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, ct: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload, default=str).encode(), "application/json; charset=utf-8")

    def _err(self, status: int, msg: str) -> None:
        self._json({"error": msg}, status)

    def do_GET(self) -> None:  # noqa: N802
        self._guarded(self._get)

    def do_POST(self) -> None:  # noqa: N802
        self._guarded(self._post)

    def _guarded(self, fn: Any) -> None:
        try:
            fn()
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as exc:
            log.exception("unhandled error serving %s %s", self.command, self.path)
            try:
                self._err(500, f"{type(exc).__name__}: {exc}")
            except Exception:
                pass

    def _get(self) -> None:
        parsed = urlparse(self.path)
        p, qs = parsed.path, parse_qs(parsed.query)
        if p in ("/", "/index.html"):
            return self._send(200, _HTML.encode(), "text/html; charset=utf-8")
        if p.startswith("/vendor/") and p[len("/vendor/"):] in VENDOR:
            name = p[len("/vendor/"):]
            return self._send(200, VENDOR[name], "application/javascript; charset=utf-8")
        if p == "/api/stats":
            return self._json(self.kg.stats())
        if p == "/api/entities":
            q = (qs.get("q") or [""])[0].strip()
            limit = _int_param(qs, "limit", 200, maximum=1000)
            return self._json({"entities": self.kg.all_entities(query=q, limit=limit)})
        if p == "/api/graph":
            raw_seed = (qs.get("seed") or [""])[0].strip()
            seeds = [s.strip() for s in raw_seed.split(",") if s.strip()] if raw_seed else []
            hops = _int_param(qs, "hops", 2, minimum=1, maximum=3)
            limit = _int_param(qs, "limit", 250, maximum=500)
            return self._json(self.kg.neighborhood(seeds, hops=hops, limit=limit))
        if p == "/api/rag":
            return self._json(get_backends().resolve())
        if p == "/api/reports":
            return self._json({
                "reports": [
                    {"id": r["id"], "title": r["title"], "description": r["description"]}
                    for r in CANNED_REPORTS
                ]
            })
        if p.startswith("/api/reports/"):
            report_id = p[len("/api/reports/"):]
            return self._json(run_report(self.kg, report_id))
        return self._err(404, f"no route: {p}")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def _post(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/ask":
            return self._api_ask()
        if parsed.path == "/api/rag":
            return self._api_rag()
        return self._err(404, f"no route: {parsed.path}")

    def _api_rag(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._err(400, "invalid Content-Length")
        if length <= 0:
            return self._err(400, "empty body")
        if length > MAX_BODY:
            return self._err(413, "body too large")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            return self._err(400, f"invalid JSON: {exc}")
        if not isinstance(payload, dict):
            return self._err(400, "expected a JSON object")

        if "backend" in payload:
            requested = str(payload.get("backend") or "auto").strip().lower()
            if requested not in ("auto", "nvidia", "ollama"):
                return self._err(400, f"unknown backend: {requested}")
            get_backends().set_backend(requested)

        if "key" in payload:
            key = str(payload.get("key") or "").strip()
            if key and not key.lower().startswith("nvapi-"):
                return self._err(400, "that does not look like an NVIDIA API key "
                                       "(expected it to start with nvapi-)")
            get_backends().set_session_key(key)

        return self._json(get_backends().resolve())

    def _send_sse(self, event: str, data: dict[str, Any]) -> None:
        payload = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        self.wfile.write(payload.encode("utf-8"))
        self.wfile.flush()

    def _handle_sse_ask(self, question: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.close_connection = True

        try:
            from sandbox_engine.query_ui import ask_rag
            from sandbox_engine.router import route_query

            route_info = route_query(question, self.kg)
            self._send_sse("route", {"route": route_info.route.value})

            result = ask_rag(
                self.kg, question,
                fiscal_year=route_info.fiscal_year,
                fiscal_quarter=route_info.fiscal_quarter,
                form_type=route_info.form_type,
            )

            if result.get("text"):
                self._send_sse("token", {"token": result["text"]})
            if result.get("used_tags"):
                self._send_sse("evidence", {"evidence": {"citations": result["used_tags"]}})
            self._send_sse("done", {
                "citations": result.get("used_tags", []),
                "route": route_info.route.value
            })
        except Exception as exc:
            log.exception("RAG error")
            self._send_sse("error", {"message": f"{type(exc).__name__}: {exc}"})

    def _api_ask(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._err(400, "invalid Content-Length")
        if length <= 0:
            return self._err(400, "empty body")
        if length > MAX_BODY:
            return self._err(413, "body too large")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as exc:
            return self._err(400, f"invalid JSON: {exc}")
        if not isinstance(payload, dict):
            return self._err(400, "expected a JSON object")
        question = str(payload.get("question") or "").strip()
        if not question:
            return self._err(400, "question is required")
        self._handle_sse_ask(question)


def _configure_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def serve(host: str = "127.0.0.1", port: int | Sequence[int] = 9000,
          open_browser: bool = True, read_only: bool = True,
          db_path: Path | None = None) -> None:
    _configure_logging()
    ports = parse_ports(port)
    db_path = db_path or resolve_db_path()
    if db_path is None:
        log.error(
            "No graph database found. Looked for:\n  %s\n"
            "Build one from the committed filings first:\n"
            "  python -m sandbox_engine --reset\n"
            "then start this server again. To serve a graph from somewhere "
            "else, pass --db <path>.",
            "\n  ".join(str(p) for p in _DB_CANDIDATES),
        )
        raise SystemExit(1)

    get_backends()

    kg = KnowledgeGraph(db_path, read_only=read_only)
    handler = type("_BoundHandler", (_Handler,), {"kg": kg})
    servers = _listeners(host, ports, handler)

    primary = f"http://{host}:{ports[0]}/"
    stats = kg.stats()
    print(f"\n{'='*70}")
    print(f"  GraphRAG Viewer & Question Answering Engine (Legacy)")
    print(f"{'='*70}")
    for number in ports:
        suffix = "" if number == ports[0] else "   (same server, same graph)"
        print(f"  Web UI       : http://{host}:{number}/{suffix}")
    print(f"  Database     : {db_path}")
    print(f"  Schema       : {stats['schema']}")
    print(f"  Graph Stats  : {stats['nodes']} entities, {stats['edges']} relationships")
    where = {"nvidia": "NVIDIA NIM", "ollama": "local Ollama"}.get(stats["rag_backend"], "unavailable")
    print(f"  RAG Model    : {stats['rag_model'] or 'none'} via {where}")
    print(f"  Why          : {stats['rag_reason']}")
    if stats["rag_backend"] == "none":
        print(f"  Answers      : DISABLED. Paste a key in the browser, or run "
              f"`ollama serve` and `ollama pull {OLLAMA_MODEL}`, then re-ask.")
    print(f"  RAG Timeout  : {RAG_TIMEOUT:.0f}s")
    print(f"  Press Ctrl-C to stop")
    print(f"{'='*70}\n")

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(primary)).start()

    for server in servers[1:]:
        threading.Thread(
            target=server.serve_forever, daemon=True,
            name=f"http-{server.server_address[1]}",
        ).start()
    try:
        servers[0].serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
        kg.close()


def _main() -> None:
    _configure_logging()
    p = argparse.ArgumentParser(
        prog="python -m ui.legacy_graphrag",
        description="Legacy GraphRAG Question Answering UI for the Blueprint LadybugDB",
    )
    p.add_argument("--port", default="9000",
                   help="one port, or several separated by commas -- all served "
                        "from the same process over one graph handle, e.g. "
                        "--port 9000,8765")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--db", type=Path, default=None,
                   help="Database file to serve. Only needed to serve a "
                        "different graph than the one found automatically; "
                        "LadybugDB locks the file, so two servers cannot share "
                        "one path. For a second port, pass --port instead.")
    p.add_argument("--read-write", action="store_true",
                   help="Open the database read-write (takes an exclusive lock, "
                        "so no other server can share the file)")
    args = p.parse_args()
    try:
        ports = parse_ports(args.port)
        serve(args.host, ports, open_browser=not args.no_browser,
              read_only=not args.read_write, db_path=args.db)
    except OSError as exc:
        log.error("%s", exc)
        raise SystemExit(1)
    except ValueError as exc:
        log.error("%s", exc)
        raise SystemExit(2)


if __name__ == "__main__":
    _main()