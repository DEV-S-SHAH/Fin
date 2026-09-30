# GraphRAG Studio — `ui_next` structure

Reference for the redesigned UI in `sandbox_engine/ui_next/`. Everything below is
the shape of the code as it stands, so it can be pasted into a fresh checkout or
used to rebuild the same UI elsewhere.

- **Port:** `9100` (override with `PORT_QUERY_UI_V2` or `--port`)
- **Run:** `python -m sandbox_engine.ui_next`
- **Legacy UI:** `python -m sandbox_engine.query_ui` (port `9000`, untouched)

---

## 1. File tree

```
sandbox_engine/
├── query_ui.py                  ← SHARED BACKEND. Not part of ui_next, but ui_next
│                                   subclasses it. Holds the graph queries, the RAG
│                                   pipeline, the grader, and the SSE stream.
├── static/                      ← VENDOR SCRIPTS served at /vendor/*.js
│   ├── d3.v7.min.js
│   └── gsap.min.js
└── ui_next/
    ├── __init__.py
    ├── __main__.py              ← `from .server import main`
    ├── server.py                ← HTTP handler, asset allowlist, 2 new endpoints
    └── static/
        ├── index.html           ← the whole DOM (311 lines)
        ├── styles.css           ← tokens, layout, components, breakpoints
        ├── app.js               ← controller: wiring, ask state machine, splitters
        ├── api.js               ← fetch wrappers + SSE reader
        ├── store.js             ← tiny observable state
        ├── graph.js             ← D3 force graph, citation glow, edge dots
        ├── answer.js            ← answer / sources / trace / provenance rendering
        ├── process.js           ← the process strip (GSAP)
        ├── reports.js           ← canned-reports drawer
        └── util.js              ← DOM, formatting, prefs, toasts
```

**Both `sandbox_engine/static/` and `ui_next/static/` exist and are different.**
`/vendor/*` is served from `sandbox_engine/static/`; `/static/*` is served from
`ui_next/static/`. Putting `gsap.min.js` in the wrong one is a silent 404.

---

## 2. Why it subclasses instead of duplicating

`server.py` defines `_NextHandler(_legacy._Handler)`. There is exactly one
implementation of `/api/ask`, `/api/graph`, `/api/entities` and the SSE stream —
the legacy one. Only these are added:

| Addition | Route | Why |
| --- | --- | --- |
| Asset serving | `GET /`, `GET /static/<name>`, `GET /favicon.svg` | The new shell + modules |
| Issuer overview | `GET /api/companies` | Sample questions built from real issuers, not 8 hard-coded Apple ones |
| Route preview | `GET /api/route?q=…` | `KNOWN` / `COLD_START` / `AMBIGUOUS` — one graph query, no model call |

Everything else falls through: `return super()._get()`.

### The asset allowlist — the #1 way to get a blank page

```python
_ASSETS: dict[str, str] = {
    "index.html": "text/html; charset=utf-8",
    "styles.css":  "text/css; charset=utf-8",
    "app.js":      "text/javascript; charset=utf-8",
    "api.js":      "text/javascript; charset=utf-8",
    "store.js":    "text/javascript; charset=utf-8",
    "graph.js":    "text/javascript; charset=utf-8",
    "answer.js":   "text/javascript; charset=utf-8",
    "process.js":  "text/javascript; charset=utf-8",   # ← add new modules here
    "reports.js":  "text/javascript; charset=utf-8",
    "util.js":     "text/javascript; charset=utf-8",
}
```

A name not in this dict returns `404 no asset: <name>`. Because these are ES
modules, **one unregistered module kills the entire graph** — `app.js` fails to
resolve its import and the page renders nothing at all. If the page is blank
after adding a file, check this dict first.

`_ASSETS` is read from memory at import time, so a newly added module needs a
**server restart**, not just a browser reload.

### Caching

```python
etag = f'W/"{name}-{int(stat.st_mtime)}-{len(body)}"'
```

`index.html` is sent `Cache-Control: no-store` (it names every other asset, so a
stale copy pins an old stylesheet). Every other asset revalidates on
mtime+size, so an edited module shows on reload and an unchanged one costs a 304.

---

## 3. DOM structure

```
body
├── a.skip-link                        → #qa-input
└── .app#app[data-view]               grid-rows: auto / 1fr / (mobile-nav)
    ├── header.topbar
    │   ├── .brand  #schema-chip
    │   ├── .topbar__stats             #stat-nodes #stat-edges #stat-issuers
    │   └── .topbar__actions           #palette-btn ⌘K · #hops
    │                                  #reports-btn · #theme-btn
    │
    ├── main.workspace#workspace       grid-columns: minmax(0,1fr) 1px --w-qa
    │   ├── section.panel--graph#graph-panel       container-type: inline-size
    │   │   ├── svg#graph-canvas                ← D3 renders here
    │   │   ├── .graph-search#graph-search       the entity box, floating over the canvas
    │   │   │   ├── .search #entity-search #entity-clear
    │   │   │   └── .graph-search__panel#entity-results   (hidden until focus/typing)
    │   │   │       ├── #entity-count            "7 matches"
    │   │   │       ├── ul.entity-list#entity-list
    │   │   │       └── .graph-search__foot     #show-all #graph-limit #clear-focus
    │   │   ├── .graph-toolbar                  #g-relayout #g-fit #g-zoom-in
    │   │   │                                    #g-zoom-out #g-labels #g-legend-toggle
    │   │   ├── #legend  #graph-hint
    │   │   ├── .chip.graph-cited#graph-cited   "cited by this answer"
    │   │   ├── .chip.graph-count#graph-count
    │   │   └── .graph-tooltip#graph-tooltip
    │   ├── .splitter--right#split-right       role=separator  tabindex=0
    │   └── section.panel--qa#qa
    │       ├── form.composer#composer
    │       │   ├── label → #qa-input textarea
    │       │   ├── .composer__box              #model-chip-inline #samples-btn #ask-btn
    │       │   ├── .samples#samples            (hidden)
    │       │   ├── .pipeline#pipeline          ← the process strip, built by process.js
    │       │   └── .keypanel#keypanel          #key-input #key-save #key-forget #use-local
    │       ├── .splitter--composer#split-composer   ← horizontal, resizes the composer
    │       ├── .tabs                           Answer · Sources · Trace · Provenance
    │       │                                    #wait-timer · #copy-answer
    │       └── .tabpanels#tabpanels
    │           ├── #tab-answer      ← prose + verdict card
    │           ├── #tab-sources
    │           ├── #tab-trace
    │           └── #tab-provenance
    │
    └── nav.mobile-nav#mobile-nav     only < 860px: graph / ask
├── .scrim#scrim  +  aside.drawer#drawer          ← canned reports
├── .scrim#palette-scrim  +  .palette#palette     ← ⌘K command palette
├── .toasts#toasts
└── p.sr-only#announcer               aria-live announcements

scripts (classic, install globals):
  /vendor/d3.v7.min.js               ← graph.js reads the global `d3`
  /vendor/gsap.min.js                ← process.js reads the global `gsap`
  /static/app.js                     type="module" — the entry point
```

The theme is applied by an **inline script in `<head>`**, before first paint, so
a light theme never flashes white on reload.

---

## 4. Module graph

```
                 ┌──────────┐
                 │ store.js │  observable state, no DOM
                 └────┬─────┘
                      │ set / subscribe / state / setCollection
       ┌──────────────┼───────────────┬──────────────┐
       │              │               │              │
  ┌────▼────┐   ┌─────▼─────┐   ┌─────▼────┐   ┌─────▼─────┐
  │ api.js  │   │  graph.js │   │ answer.js│   │reports.js │
  └────┬────┘   └─────┬─────┘   └──────────┘   └───────────┘
       │              │
       └──────┬───────┘
          ┌───▼────┐      ┌────────────┐
          │ util.js│◄─────┤ process.js │
          └────┬───┘      └────────────┘
               │
          ┌────▼──────────────┐
          │ app.js  (entry)   │  → new App().start()
          └───────────────────┘
```

- `util.js` — **no dependencies.** `el`, `clear`, `$`, `md`, `typeColor`,
  `fmtNumber`, `loadPref`/`savePref`, `toast`, `announce`, `debounce`.
- `store.js` — **no dependencies.** Single `state` object, `set(patch)` notifies
  only the keys that changed, `setCollection` for `Set`s.
- `api.js` — depends on nothing; all `fetch` calls in the app go through it.
- `graph.js`, `answer.js`, `reports.js` — one view object each, take their
  dependencies by import and expose a small public surface.
- `process.js` — depends only on `util.js`; reads `window.gsap` at construction.
- `app.js` — the only module with side effects at import time.

### `el()` takes three arguments

```js
el(tag, props = {}, children = [])
```

A fourth argument is **silently dropped**. `style` is a prop, not an argument:

```js
el("li", { class: "proc__step", style: "--i:2" }, [ … ])   // ✅
el("li", { class: "p" }, [ … ], { style: "--i:2" })        // ❌ style lost
```

`props.text` sets `textContent`, `props.html` sets `innerHTML`, `onclick` binds.

---

## 5. The process strip — what the pipeline is doing

This is the feature that shows the retrieval as it happens, and it is honest
about what it does not know.

### Server side (`query_ui.py`)

`_StageTimer` already declared every stage up front so absent-vs-zero is
distinguishable. It gained an `on_enter` callback:

```python
class _StageTimer:
    def __init__(self, on_enter: "Callable[[str], None] | None" = None) -> None:
        …
        self._on_enter = on_enter

    @contextlib.contextmanager
    def stage(self, name: str):
        started = time.perf_counter()
        if self._on_enter is not None:
            try:
                self._on_enter(name)      # OUTSIDE the try: a broken progress
            except Exception:              # callback must not swallow the stage
                log.debug("stage callback failed for %s", name, exc_info=True)
        try:
            yield
        finally:
            self._stages[name] += (time.perf_counter() - started) * 1000.0
```

`ask_rag` takes the callback straight through:

```python
def ask_rag(kg, question, on_stage: "Callable[[str], None] | None" = None) -> dict:
    timer = _StageTimer(on_enter=on_stage)
    with timer.stage("routing"):
        …
```

The callback fires on stage **entry**, not exit — a duration is only known on
exit, and a client that renders `retrieval: 412ms` before the model has been
called is reporting work that has not happened.

Two tables describe the stages:

```python
WIRE_STAGES = ("routing", "fetching", "extraction", "stitching", "traversal", "synthesis")

STAGE_MESSAGES = {
    "routing":    "Matching the question to an entity",
    "fetching":   "Fetching the latest filing from EDGAR",
    "extraction": "Extracting financial facts from the filing",
    "stitching":  "Stitching the new facts onto the graph",
    "traversal":  "Traversing the knowledge graph",
    "synthesis":  "Composing the answer from the evidence",
}

ROUTE_PLAN = {
    "KNOWN":      ("routing", "traversal", "synthesis"),
    "COLD_START": ("routing", "fetching", "extraction", "stitching", "traversal", "synthesis"),
}
```

`ROUTE_PLAN` matters: a known entity is already in the graph, so it genuinely
skips fetch/extract/stitch. Showing six dots for a three-stage run would be a lie.

### The wire

The KNOWN route used to `POST /api/ask` and block on a silent socket — retrieval
is instant, then the model says nothing for a minute, which reads as a hang. Both
routes now stream:

```
event: status   {"step":"start","ticker":"AAPL","stages":["routing","traversal","synthesis"], …}
event: status   {"step":"routing","message":"Matching the question to an entity","elapsed_ms":0.0}
event: status   {"step":"traversal","message":"Traversing the knowledge graph","elapsed_ms":31.0}
event: status   {"step":"synthesis","message":"Composing the answer from the evidence","elapsed_ms":2921.0}
event: token    {"token":"The "}
…
event: done     {"status":"complete","route":"KNOWN","stage_latencies_ms":{…},"graph_metrics":{…}}
```

The handler dedupes, because `stage()` accumulates and a re-entry must not
replay:

```python
announced: set[str] = set()

def announce(stage: str) -> None:
    if stage in announced:
        return
    announced.add(stage)
    self._send_sse("status", {
        "step": stage,
        "message": STAGE_MESSAGES.get(stage, stage),
        "elapsed_ms": round((time.monotonic() - stage_started) * 1000.0, 2),
    })

result = ask_rag(self.kg, question, on_stage=announce)
```

### Client side (`process.js`)

```js
const p = new Process($("pipeline"));
p.begin({ plan, ticker });   // plan = data.stages from the "start" event
p.enter(stage, message);     // a stage began
p.detail("128 entities, 310 relationships");
p.end({ ok: true, note: "" });
```

Three rules that make it worth having:

1. **The plan is built from the server's own list**, sent before any work starts.
   A stage the plan did not predict is appended, never dropped — it is real work
   somebody is waiting on.
2. **The clock is `requestAnimationFrame`, not `gsap.ticker`.** Routing it
   through the ticker looked tidier, but the no-GSAP shim's ticker never fires,
   so a page without GSAP — or one that asked for reduced motion — would have
   shown a process with no timings, which is the one thing the strip exists to
   provide.
3. **The progress rail is scaled (`scaleX`), not width-animated**, and it fills
   by *stage count*, not by elapsed time. There are no stage weights, so a rail
   that creeps forward on a timer would be inventing a completion estimate.

### GSAP is optional

```js
function motion() {
  if (globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return SHIM;
  return globalThis.gsap || SHIM;
}
```

`SHIM` applies the end state and animates nothing. `prefers-reduced-motion` takes
the same route as a missing vendor file, because **a stylesheet cannot switch
GSAP off** — it writes inline transforms and no `!important` rule reaches them.
The choice has to be made at the one point every tween passes through.

---

## 6. The panel-width budget (the side-panel fix)

`grid-template-columns: minmax(0, 1fr) 1px var(--w-qa)`

`minmax(0, 1fr)` has **no floor of its own**. Two bugs came out of that when
there were three panels. Each panel was handed `viewport - 660` independently, so
both could claim the whole of it: at 1180px a 520px explorer plus a 520px QA
panel left the canvas 138px wide. And nothing re-clamped when the window
narrowed, so a layout arranged on a 2560px monitor kept its 520 + 960 at 1400px —
wider than the window — and the canvas went to nothing.

The explorer is gone now, so the budget has one claimant instead of two and the
pair logic collapses with it. What survives is the floor:

```js
#graphFloor()      // 0 below 860px, 320 below 1180px, else 360
#liveWidth()       // 0 when the splitter is hidden (a stacked tab)
#splitGutter()     // the live 1px splitter column, counted out of the budget
#fitPanel(wanted, fair)   // clamps the QA panel against the floor
```

Two things `#fitPanel` still has to get right:

- **The canvas floor is a floor, not a preference.** A preferred minimum that
  does not fit the room left over is not a minimum; the panel gets the room and
  the canvas keeps 360px.
- **`fair` decides who gives way.** While the panel is dragged it does not — it
  takes what was asked for, because a layout that resizes the handle you are
  holding cannot be dragged. A window narrowing has no owner, so the panel is
  scaled straight down to the budget.

A hidden splitter means the panel is a stacked tab: it holds no width, and
writing to it would overwrite a preference needed back on a wide screen — so
`setPanelWidth` returns `null` and does nothing.

Verified in real Chrome by dragging the handle 5000px past the left edge of the
window, at four window widths: the canvas never dropped below its floor.

| Window | qa | graph | floor |
| --- | --- | --- | --- |
| 900 | 520 | 363 | 320 |
| 1280 | 520 | 758 | 360 |
| 1600 | 520 | 1063 | 360 |
| 1920 | 520 | 1383 | 360 |

### The composer splitter

`#split-composer` sits *inside* `#qa`, between the composer and the tabs, and
resizes vertically. Direction is not the same as the panels:

- `#split-right` — `startX - event.clientX` (right edge fixed)
- `#split-composer` — `startHeight + (event.clientY - startY)` (top edge fixed)

Defaults `auto`, `220`–`640px`; drag / ArrowUp shrinks, ArrowDown expands,
`Home` / double-click resets; persisted as `composerHeight`.

---

## 7. Responsive behaviour

| Width | Layout |
| --- | --- |
| `≥ 860px` | Two columns, `#split-right` live. |
| `< 860px` | One panel at a time via `.app[data-view="ask"\|"graph"]`; the splitter is hidden. `#mobile-nav` appears. |

Which panel is on screen is **state, not a guess from the viewport**. A stored
`view` of `"explore"` from an older session is coerced to `"graph"` in
`#restorePreferences`, because that view was made of the explorer panel and would
otherwise land the reader on an empty workspace.

### Inside the graph

The overlays in the canvas are keyed to a **container query**, not a viewport
media query, because the canvas is the element the reader resizes — dragging the
answer panel changes the graph's width without changing the window's.

```css
.graph-panel { container-type: inline-size; }

@container (max-width: 719px) {
  .graph-legend { top: calc(var(--sp-3) + 92px); max-width: none; }
}
```

A narrow canvas cannot hold the search box and the legend side by side, and the
search box is what the reader came for, so the legend drops below the toolbar and
the search keeps the top-left corner.

---

## 8. State and persistence

`store.js` is a ~40-line observable. `set(patch)` notifies only the keys that
actually changed, so a subscriber is not re-run for an unrelated write.

```js
export const state = {
  stats: null, companies: [], entities: [],
  graph: { nodes: [], links: [], seeds: [] },
  graphLoading: false, selected: null,

  cited: new Set(), hiddenTypes: new Set(),
  showLabels: loadPref("labels", true), legendOpen: loadPref("legend", true),
  hops: loadPref("hops", 2), graphLimit: loadPref("graphLimit", 150),

  question: "", answer: null, streaming: "", busy: false, phase: null, tab: "answer",
  rag: null,

  view: loadPref("view", "graph"),
};
```

`localStorage` keys (all prefixed `ui2.`): `theme`, `view`, `hops`, `labels`,
`legend`, `graphLimit`, `qaWidth`, `composerHeight`, `lastQuestion`.
`explorerWidth` is no longer read; a stale key in an old profile is inert.

`loadPref` / `savePref` round-trip `null` as "absent" — a cleared preference must
not come back as the string `"null"`.

---

## 9. API surface

| Call | Endpoint | Notes |
| --- | --- | --- |
| `fetchStats()` | `GET /api/stats` | node/edge counts, schema, rag model |
| `fetchCompanies()` | `GET /api/companies` | **ui_next only** |
| `fetchRagState()` | `GET /api/rag` | backend, model, whether a key is held |
| `saveRagState(body)` | `POST /api/rag` | `{key}` or `{backend}`; key never echoed back |
| `fetchEntities(q, limit)` | `GET /api/entities` | |
| `fetchGraph({seed, hops, limit})` | `GET /api/graph` | |
| `fetchRoute(question)` | `GET /api/route?q=` | **ui_next only**, 1s client timeout |
| `fetchReports()` | `GET /api/reports` | |
| `fetchReport(id)` | `GET /api/reports/<id>` | zero-LLM Cypher |
| `askJson(question)` | `POST /api/ask` | non-streaming; **no longer used by app.js** |
| `askStream(q, onEvent, {signal})` | `POST /api/ask?stream=true` | SSE reader, forwards `AbortSignal` |

`ROUTE_PROBE_MS = 1000`. The probe resolves in ~10ms (one local graph query, no
model call), so a second of silence is a fault, not slowness. A failed probe is
**not** a failed question — it falls through to the route that always works.

### The ask state machine

Both routes stream now. All setup is inside `try/finally`, so the busy flag
clears on every ending:

```js
set({ busy: true, question, streaming: "" });
try {
  /* 1s bounded route probe */
  this.answer.pending(question);
  this.process.begin({ plan: processPlan(route), ticker });
  await this.#askStreaming(question);
} catch (error) {
  if (error.name === "AbortError") this.answer.error("cancelled");
  else { this.answer.error(`Query failed: ${error.message}`); … }
} finally {
  this.abort = null;
  this.#stopTimer();
  set({ busy: false });
  this.process.end({ ok: this.processOk, note: this.processNote });
  this.processOk = true; this.processNote = "";
  this.#setAskLabel(false);
  $("ask-btn").disabled = state.rag?.rag_backend === "none";
}
```

`AbortError` is preserved through `api.js` on purpose — a cancel is not a
failure and must not be reported as one.

A model failure is `status: "error"` with an `error` field and **no verdict**.
`REFUSED` means the model declined to answer, which is a different thing, and a
503 from the model backend must never be rendered as a refusal.

---

## 10. The graph (`graph.js`)

D3 force simulation, one SVG, layered so a glow cannot sit under a crisp edge:

```
.g-edge-glow     ← wide, blurred, for cited/incident edges
.g-edges         ← crisp lines
.g-edge-flows    ← the marching dots for a new answer
.g-nodes
```

- **Only answer-cited nodes glow.** Everything else recedes to context; hovering
  a dimmed node rescues it.
- **Marching dots** run along the answer's own edges, 2.1s
  (`FLOW_MS = 2100`), travel 1.4s, stagger up to 450ms, one-shot, torn down on
  completion or `destroy()`. Points are re-read from live force-layout geometry
  each frame, so a node still settling does not detach its dots.
- `prefers-reduced-motion` disables the flow.
- Public surface: `setData(payload, {cited, fresh})`, `fit`, `zoomBy`, `relayout`,
  `focus(id)`, `setCited`, `setHiddenTypes`, `setLabels`, `destroy`.

---

## 11. Design tokens

Dark is the **default** (a dark canvas is what the graph is drawn on). Light is a
full second theme, not an inversion — every token that matters has its own light
value in `[data-theme="light"]`.

```css
--text-xs 11px  --text-sm 13px  --text-md 14px  --text-base/lg/xl clamp(…)
--sp-1 … --sp-10      4px base, named by use
--r-xs/sm/md/lg/xl/full
--dur-fast 120ms  --dur 200ms  --dur-slow 380ms
--ease     cubic-bezier(0.32, 0.72, 0, 1)
--ease-out cubic-bezier(0.16, 1, 0.3, 1)
--topbar-h 56px  --mobile-nav-h 58px
--w-qa 520px                          ← written by app.js, clamped
```

Surfaces `1/2/3`, then `--green/--amber/--red` with `-soft` variants, and
`--shadow-1/2/3`. Breakpoints: `1180px`, `860px`, plus
`max-height: 620px and min-width: 860px` and a `prefers-reduced-motion` /
`prefers-contrast: more` pair at the end.

---

## 12. Run and verify

```powershell
python -m sandbox_engine.ui_next --port 9100 --no-browser
# → http://127.0.0.1:9100/
```

Health check:

```powershell
foreach ($p in @("/","/static/app.js","/static/process.js","/vendor/gsap.min.js","/api/stats")) {
  "{0} {1}" -f $p, (Invoke-WebRequest "http://127.0.0.1:9100$p" -UseBasicParsing).StatusCode
}
```

Watch the stages arrive:

```powershell
(Invoke-WebRequest "http://127.0.0.1:9100/api/ask?stream=true" -Method POST `
  -Body '{"question":"What was Apple total net sales in fiscal 2026?","stream":true}' `
  -ContentType "application/json" -UseBasicParsing).Content |
  Select-String 'event:|"step"'
```

```powershell
node --check sandbox_engine/ui_next/static/app.js      # and each other module
python -m pytest tests/test_query_ui_ports.py tests/test_query_ui_transport.py tests/test_provenance_ui.py -q
```

The full suite is 827 tests. `tests/test_benchmark_runner.py::TestInterruptHandling::
test_a_real_sigint_to_a_real_process_saves_and_exits_130` fails on Windows — it
calls `os.killpg`, which is POSIX-only. Unrelated to the UI. Running every file
in one process dies partway through on a model call; run them per file.

---

## 13. If the page is blank

In order:

1. **`_ASSETS` in `server.py`** — a new module is not listed. Because these are ES
   modules one 404 kills the whole app. **Restart the server**; the dict is built
   at import.
2. **Devtools console** — a module-level throw names itself.
3. **Vendor path** — `/vendor/*.js` comes from `sandbox_engine/static/`, not from
   `ui_next/static/`. GSAP missing is survivable (the shim takes over); d3
   missing is not.
4. **Stale process** — an old server still holding the port serves the old
   `_ASSETS`. `Get-NetTCPConnection -State Listen | Where LocalPort -eq 9100`,
   then `Stop-Process -Id <pid>`.
5. **Server port** — `PORT_QUERY_UI_V2` or `--port` can move it; the banner
   prints the real URL.
