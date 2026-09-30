/* Application wiring: the only module that knows the page.
 *
 * Everything below is glue between five collaborators that each own one thing —
 * `api.js` talks to the server, `graph.js` draws the canvas, `answer.js`
 * renders an answer, `reports.js` owns the drawer, `store.js` holds the state —
 * and this file decides what happens when the reader does something.
 */

import {
  askJson, askStream, fetchCompanies, fetchEntities, fetchGraph,
  fetchRoute, fetchStats, saveRagState,
} from "./api.js";
import { AnswerView } from "./answer.js";
import { GraphView } from "./graph.js";
import { ReportsPanel } from "./reports.js";
import { set, setCollection, state, subscribe } from "./store.js";
import {
  $, announce, clear, debounce, el, fmtNumber, loadPref, prettyType,
  savePref, toast, typeColor,
} from "./util.js";

/* ── pipeline steps shown while a question runs ──────────────────────────── */

const PHASES = {
  COLD_START: "live fetch",
  routing: "routing",
  ambiguous: "routing",
  retrieval: "retrieval",
  fetching: "live fetch",
  extracting: "extraction",
  stitching: "stitching",
  traversing: "traversal",
  synthesis: "synthesis",
  fallback: "fallback",
  failed: "failed",
};

const PHASE_ORDER = ["routing", "live fetch", "extraction", "stitching", "traversal", "retrieval", "synthesis"];

class App {
  #selectedResult = 0;
  #searchNavBound = false;

  constructor() {
    this.graph = new GraphView($("graph-canvas"), {
      onSelect: (id) => this.focusEntity(id),
      tooltip: $("graph-tooltip"),
      tooltipHost: $("graph-panel"),
    });
    this.answer = new AnswerView({
      onCite: (id) => this.focusCitation(id),
    });
    this.reports = new ReportsPanel();
    this.abort = null;
    this.timer = null;
    this.entityTerm = "";
    this.phaseSeen = new Set();
  }

  /* ── boot ────────────────────────────────────────────────────────────── */

  async start() {
    this.#restorePreferences();
    this.#wireChrome();
    this.#wireSplitters();
    this.#wireKeyboard();
    this.#wireStore();
    this.answer.empty();
    this.#renderSamples([]);

    this.#renderGraphState();

    // Three independent reads, run together. The graph is the slow one, so it
    // is not awaited behind the two that are instant: the page fills in as each
    // answer arrives instead of appearing all at once when the slowest lands.
    const [stats, companies] = await Promise.allSettled([fetchStats(), fetchCompanies()]);
    if (stats.status === "fulfilled") this.#applyStats(stats.value);
    else this.#statsFailed(stats.reason);
    if (companies.status === "fulfilled") {
      set({ companies: companies.value.companies || [] });
      this.#renderIssuers();
      this.#renderSamples(this.sampleQuestions(companies.value.companies || []));
    }

    /* Started together, not one after the other. Asking for 500 entities is the
     * slowest read on the page, and the graph is the thing a reader looks at
     * first — chaining them behind it left the canvas empty for seconds. */
    await this.showWholeGraph();

    /* ?q=... re-runs a question on load, so a link to a question is a link to
     * the answer. The question is stored in the URL rather than the answer:
     * an answer is only as good as the graph it was graded against, and that
     * changes when the next filing lands. Re-running says what the evidence
     * supports today rather than what it supported when the link was made. */
    const shared = new URLSearchParams(location.search).get("q");
    if (shared) {
      $("qa-input").value = shared;
      this.#autogrow($("qa-input"));
      await this.ask();
    }
  }

  /* ── header, stats, model chip ───────────────────────────────────────── */

  /** The header leads with three counts. Two come from /api/stats; the issuer
   *  count is a count of distinct tickers, which only /api/companies knows. The
   *  two requests finish independently, so both call this rather than the
   *  second one depending on the order they land in. */
  #renderIssuers() {
    const count = state.companies?.length || state.stats?.companies || 0;
    $("stat-issuers").textContent = count ? fmtNumber(count) : "—";
  }

  #applyStats(stats) {
    set({ stats, rag: stats });
    this.#renderIssuers();
    $("stat-nodes").textContent = fmtNumber(stats.nodes ?? 0);
    $("stat-edges").textContent = fmtNumber(stats.edges ?? 0);
    const chip = $("schema-chip");
    if (stats.schema) {
      chip.hidden = false;
      chip.textContent = stats.schema;
      chip.title = `LadybugDB schema: ${stats.schema}`;
    }
    this.#renderLegend();
    this.applyRagState(stats);
  }

  #statsFailed(error) {
    $("stat-nodes").textContent = "—";
    $("stat-edges").textContent = "—";
    toast(`could not read the graph stats: ${error.message}`, "bad");
  }

  toggleType(key) {
    const hidden = new Set(state.hiddenTypes);
    if (hidden.has(key)) hidden.delete(key);
    else hidden.add(key);
    setCollection("hiddenTypes", hidden);
    this.graph.setHiddenTypes(hidden);
    this.#syncTypeControls();
  }

  #syncTypeControls() {
    for (const row of $("legend").querySelectorAll(".legend-row")) {
      row.setAttribute("aria-pressed", state.hiddenTypes.has(row.dataset.type) ? "false" : "true");
    }
  }

  applyRagState(state_) {
    const backend = state_?.rag_backend || "";
    const model = state_?.rag_model || "";
    const where = { nvidia: "NVIDIA NIM", ollama: "local Ollama" }[backend];
    const label = where ? `${model} · ${where}` : "no model available";

    $("model-label").textContent = model ? shortModel(model) : "no model";
    $("model-dot").className = `status-dot ${where ? "status-dot--ok" : "status-dot--bad"}`;
    $("model-chip").title = state_?.rag_reason || "";
    $("model-chip-inline").textContent = where ? `${shortModel(model)} · ${where}` : "answering disabled";
    $("model-chip-inline").className = `chip chip--dot ${where ? "" : "chip--bad"}`;

    set({ rag: state_ });

    /* The key panel appears only when there is a decision to make: nothing to
     * phrase answers with, or a key that was refused. A reader with a working
     * key never sees it. */
    const blocked = backend === "none" || state_?.rag_stored_key_rejected;
    $("ask-btn").disabled = blocked;
    $("qa-input").placeholder = blocked
      ? "Answering is off until a model is available — see the note below."
      : "e.g. What was Apple's total net sales in fiscal 2025, and how much came from the Americas segment?";
    $("keypanel").hidden = !blocked;
    if (!blocked) return;

    const bits = [];
    if (state_.rag_reason) bits.push(state_.rag_reason);
    if (state_.rag_stored_key_rejected) bits.push("Paste a different key below, or start the local model.");
    else bits.push("Get a key at https://build.nvidia.com and paste it below.");
    if (!state_.rag_ollama_reachable) bits.push("Or run `ollama serve` then `ollama pull llama3.2` and press “use local model”.");
    else bits.push((state_.rag_ollama_models || []).length
      ? `The local server is up with ${state_.rag_ollama_models.join(", ")}.`
      : "The local server is up but has no models pulled yet.");

    $("key-note").textContent = bits.join(" ");
    $("use-local").hidden = !state_.rag_ollama_reachable;
    $("key-forget").hidden = state_.rag_key_source !== "browser";
  }

  async #postRag(body) {
    try {
      this.applyRagState(await saveRagState(body));
    } catch (error) {
      $("key-note").textContent = error.message;
    }
  }

  /* ── entities ────────────────────────────────────────────────────────── */

  async loadEntities() {
    try {
      const data = await fetchEntities(this.entityTerm, 500);
      set({ entities: data.entities || [] });
      this.#renderSearchResults();
    } catch (error) {
      toast(`entity search failed: ${error.message}`, "bad");
    }
  }

  #renderSearchResults() {
    const host = $("search-results");
    const input = $("topbar-search");
    const term = (this.entityTerm || "").toLowerCase();

    if (!term) {
      host.hidden = true;
      return;
    }

    const rows = state.entities.filter((entity) =>
      [entity.name, entity.entity_type, entity.description]
        .some((value) => String(value ?? "").toLowerCase().includes(term)),
    );

    host.hidden = false;
    clear(host);

    if (!rows.length) {
      host.append(el("div", { class: "search-empty", text: "No matching entities." }));
      return;
    }

    this.#selectedResult = 0;
    for (const entity of rows.slice(0, 50)) {
      const type = entity.entity_type || "";
      const item = el("button", {
        class: "search-result",
        type: "button",
        role: "option",
        title: entity.description || entity.name,
        dataset: { id: entity.id },
        onclick: () => { this.#pickResult(entity.id); },
      }, [
        el("span", { class: "search-result__dot", style: `background:${typeColor(type)}` }),
        el("span", { class: "search-result__name", html: highlightTerm(entity.name, term) }),
        el("span", { class: "search-result__type", text: prettyType(type) }),
      ]);
      host.append(item);
    }

    /* The dropdown scrolls with the keyboard: keep the active row in sight. */
    if (!this.#searchNavBound) {
      host.addEventListener("mousemove", (event) => {
        const row = event.target.closest(".search-result");
        if (!row) return;
        const rows = [...host.querySelectorAll(".search-result")];
        this.#activeResult(rows.indexOf(row), false);
      });
      this.#searchNavBound = true;
    }
  }

  #activeResult(index, scroll) {
    const host = $("search-results");
    const rows = [...host.querySelectorAll(".search-result")];
    if (!rows.length) return;
    this.#selectedResult = Math.max(0, Math.min(rows.length - 1, index));
    rows.forEach((row, i) => row.setAttribute("aria-selected", i === this.#selectedResult ? "true" : "false"));
    if (scroll) rows[this.#selectedResult]?.scrollIntoView({ block: "nearest" });
  }

  #pickResult(id) {
    this.#hideSearch();
    this.focusEntity(id);
  }

  #hideSearch() {
    $("search-results").hidden = true;
  }

  /* ── graph ───────────────────────────────────────────────────────────── */

  async showWholeGraph() {
    set({ graphLoading: true });
    try {
      const payload = await fetchGraph({ hops: state.hops, limit: Number(state.graphLimit) });
      set({ graph: payload, selected: null });
      const counts = this.graph.setData(payload, { fresh: true });
      $("graph-count").textContent = `${fmtNumber(counts.nodes)} nodes · ${fmtNumber(counts.links)} links`;
    } catch (error) {
      toast(`graph query failed: ${error.message}`, "bad");
    } finally {
      set({ graphLoading: false });
    }
  }

  async focusEntity(id) {
    set({ selected: id });
    if (window.matchMedia("(max-width: 859px)").matches) this.setView("graph");
    try {
      const payload = await fetchGraph({ seed: id, hops: state.hops, limit: Number(state.graphLimit) });
      set({ graph: payload });
      const counts = this.graph.setData(payload, { fresh: true });
      $("graph-count").textContent = `${fmtNumber(counts.nodes)} nodes · ${fmtNumber(counts.links)} links`;
    } catch (error) {
      toast(`graph query failed: ${error.message}`, "bad");
    }
  }

  /** Follow a citation. The node is usually already on screen, so this centres
   *  on it rather than refetching — and says so if it is not. */
  focusCitation(id) {
    if (this.graph.focus(id)) {
      set({ selected: id });
      // Below the two-column breakpoint the graph is its own view, so focusing a
      // citation has to move the reader there or it happens off-screen.
      if (window.matchMedia("(max-width: 859px)").matches) this.setView("graph");
      return;
    }
    toast("that entity is not in the current view — loading its neighbourhood", "");
    this.focusEntity(id);
  }

  #renderGraphState() {
    const count = state.graph?.nodes?.length ?? 0;
    $("graph-count").textContent = count
      ? `${fmtNumber(count)} nodes · ${fmtNumber(state.graph?.edges?.length ?? 0)} links`
      : "0 nodes";
  }

  #wireStore() {
    subscribe((_, keys) => {
      if (keys.includes("graph")) this.#renderGraphState();
    });
  }

  #renderLegend() {
    const host = clear($("legend"));
    const types = state.stats?.entity_types || [];
    for (const [type] of types) {
      const key = String(type).toLowerCase();
      host.append(el("button", {
        class: "legend-row",
        type: "button",
        style: `color:${typeColor(type)}`,
        dataset: { type: key },
        "aria-pressed": state.hiddenTypes.has(key) ? "false" : "true",
        title: `Show or hide ${prettyType(type)}`,
        onclick: () => this.toggleType(key),
      }, [
        el("span", { class: "legend-row__dot" }),
        el("span", { text: prettyType(type) }),
        el("span", { class: "legend-row__n", text: fmtNumber(state.stats.table_counts?.[type] ?? "") }),
      ]));
    }
  }

  /* ── asking ──────────────────────────────────────────────────────────── */

  /**
   * Two transports, chosen from the route rather than guessed at.
   *
   * `COLD_START` fetches a filing from EDGAR and synthesises it through a
   * generator, so the server really does emit tokens as it goes and SSE is worth
   * reading. `KNOWN` is one blocking call to a model that returns the whole
   * answer at once — the old page's SSE handler "streams" it by splitting the
   * finished string into words, which looks like progress and costs a second
   * full request to recover the grading the stream never carried. So the known
   * route goes down the plain JSON path: one request, complete payload.
   */
  async ask() {
    const question = $("qa-input").value.trim();
    if (!question) {
      toast("type a question first", "bad");
      $("qa-input").focus();
      return;
    }
    if (state.busy) {
      toast("a question is already running", "bad");
      return;
    }

    set({ busy: true, question, streaming: "" });
    this.phaseSeen = new Set();
    savePref("lastQuestion", question);
    this.showTab("answer");
    this.#setAskLabel(true);
    $("ask-btn").disabled = true;
    this.#startTimer();

    let route = "KNOWN";
    let ticker = null;
    try {
      const probe = await fetchRoute(question);
      route = probe.route || "KNOWN";
      ticker = probe.ticker;
    } catch {
      // A failed probe is not a failed question: fall through to the route that
      // always works rather than refusing to answer.
    }

    this.answer.pending(question);
    this.#renderPipeline(PHASES[route] || "routing");

    try {
      if (route === "COLD_START") {
        await this.#askStreaming(question);
      } else {
        this.#renderPipeline("retrieval");
        this.#finish(await askJson(question));
      }
    } catch (error) {
      if (error.name === "AbortError") {
        this.answer.error("cancelled");
      } else {
        this.answer.error(`Query failed: ${error.message}`);
        toast(`query failed: ${error.message}`, "bad");
        announce("Query failed");
      }
    } finally {
      this.#stopTimer();
      set({ busy: false });
      // After the busy flag drops, so the strip is torn down on every ending:
      // answered, refused, errored or cancelled. Leaving a row of finished dots
      // above a finished answer reads as work still in progress.
      this.#renderPipeline("done");
      this.#setAskLabel(false);
      $("ask-btn").disabled = state.rag?.rag_backend === "none";
    }

    if (route === "COLD_START" && ticker) {
      toast(`nothing stored for ${ticker} — answered from a filing fetched just now, and a full ingest is queued in the background`, "");
    }
  }

  async #askStreaming(question) {
    this.abort = new AbortController();
    await askStream(question, (event) => this.#onStreamEvent(event), { signal: this.abort.signal });
    this.abort = null;
  }

  #onStreamEvent(event) {
    const { type, data } = event;

    if (type === "status") {
      const phase = PHASES[data.step] || data.step;
      this.#renderPipeline(phase);
      if (data.message) this.#setTimerLabel(data.message);
      return;
    }
    if (type === "token") {
      set({ streaming: state.streaming + (data.token ?? "") });
      this.answer.appendStream(state.streaming);
      return;
    }
    if (type === "error") {
      this.answer.error(data.error || "the server reported an error");
      return;
    }
    if (type !== "done") return;

    if (data.status === "error") {
      this.answer.error(data.error || "the server reported an error");
      return;
    }
    /* The COLD_START stream carries the synthesis, the provenance ledger and the
     * graph, but no grading — that path has nothing to grade, because the
     * synthesis never went through the grader. */
    this.#finish({
      ...data,
      question,
      text: data.answer || state.streaming,
      answer: data.answer || state.streaming,
      route: data.route || "COLD_START",
    });
  }

  #finish(result) {
    set({ answer: result, streaming: "" });
    this.answer.render(result);

    if (result.rag) this.applyRagState(result.rag);
    if (result.rag_backend) set({ rag: { ...(state.rag || {}), rag_backend: result.rag_backend } });

    if (result.graph) {
      const cited = (result.used_tags || []).map((tag) => result.tag_map?.[tag]).filter(Boolean);
      setCollection("cited", new Set(cited));
      const counts = this.graph.setData(result.graph, { cited });
      $("graph-count").textContent = `${fmtNumber(counts.nodes)} nodes · ${fmtNumber(counts.links)} links`;
    }

    announce(["answer", result.verdict?.toLowerCase() || result.route?.toLowerCase() || "returned",
      ...Object.entries(result.provenance_mix || {}).map(([key, value]) => `${key} ${value}`),
      ...(result.violations || [])].filter(Boolean).join(". "));
  }

  #startTimer() {
    const chip = $("wait-timer");
    chip.hidden = false;
    this.timerStarted = Date.now();
    this.phaseMessage = "retrieving";
    clearInterval(this.timer);
    this.timer = setInterval(() => this.#tickTimer(), 1000);
    this.#tickTimer();
  }

  #tickTimer() {
    // A local model on a laptop can take minutes, and a counter that stops
    // advancing reads as a hang rather than as a slow machine.
    const seconds = Math.round((Date.now() - (this.timerStarted || Date.now())) / 1000);
    $("wait-timer").textContent = `${this.phaseMessage}… ${seconds}s`;
  }

  /** A status line from the server replaces the generic label, so the reader
   *  sees which stage is slow instead of guessing. The elapsed count survives:
   *  a stage change is not a restart. */
  #setTimerLabel(message) {
    this.phaseMessage = String(message || "working").replace(/…$/, "");
    this.#tickTimer();
  }

  #stopTimer() {
    clearInterval(this.timer);
    $("wait-timer").hidden = true;
  }

  #setAskLabel(busy) {
    $("ask-label").textContent = busy ? "Asking…" : "Ask";
  }

  #renderPipeline(active) {
    const host = $("pipeline");
    if (!state.busy && active === "done") {
      host.hidden = true;
      return;
    }
    host.hidden = false;
    const steps = active === "done" ? [] : PHASE_ORDER;
    clear(host);
    for (const step of steps) {
      const isActive = step === active;
      const isDone = this.phaseSeen.has(step);
      host.append(el("span", {
        class: `step ${isActive ? "is-active" : ""} ${isDone && !isActive ? "is-done" : ""}`,
      }, [el("span", { class: "step__dot" }), el("span", { text: step })]));
    }
    if (active && active !== "done") this.phaseSeen.add(active);
  }

  /* ── sample questions, built from the issuers actually in the graph ──── */

  sampleQuestions(companies) {
    if (!companies.length) return [];
    const out = [];
    for (const company of companies.slice(0, 3)) {
      const name = shortCompany(company.name);
      const latest = company.latest || {};
      const year = latest.fiscal_year ? `fiscal ${latest.fiscal_year}` : "the latest period";
      out.push(`What was ${name}'s total net sales in ${year}?`);
      out.push(`What did ${name} report for gross profit, operating income and R&D in ${year}?`);
      out.push(`How does ${name}'s revenue break down across its product and geographic segments?`);
      out.push(`What are ${name}'s total assets and total liabilities in ${year}?`);
    }
    return out.slice(0, 8);
  }

  /** The question box grows with its content up to a ceiling, then scrolls:
   *  the common case is one line, and a box that jumps in height as you type
   *  pushes the rest of the page around while you are reading it. */
  #autogrow(input) {
    input.style.height = "auto";
    input.style.height = `${Math.min(168, input.scrollHeight)}px`;
  }

  #renderSamples(questions) {
    const host = clear($("samples"));
    for (const question of questions) {
      host.append(el("button", {
        class: "sample",
        type: "button",
        text: question,
        onclick: () => {
          $("qa-input").value = question;
          this.ask();
        },
      }));
    }
  }

  /* ── chrome wiring ───────────────────────────────────────────────────── */

  #restorePreferences() {
    const root = document.documentElement;

    const qaWidth = loadPref("qaWidth", null);
    if (qaWidth) root.style.setProperty("--w-qa", `${qaWidth}px`);

    $("hops").value = String(state.hops);
    $("graph-limit").value = String(state.graphLimit);
    $("g-labels").setAttribute("aria-pressed", state.showLabels ? "true" : "false");
    $("g-legend-toggle").setAttribute("aria-pressed", state.legendOpen ? "true" : "false");
    $("legend").hidden = !state.legendOpen;
    $("qa-input").value = state.lastQuestion || "";
    this.#autogrow($("qa-input"));
    this.setView(state.view);
    this.graph.setLabels(state.showLabels);
  }

  #wireChrome() {
    /* topbar entity search — one input, results drop below it */
    const onFilter = debounce((term) => {
      this.entityTerm = term;
      if (term) this.loadEntities();
      else this.#hideSearch();
    }, 180);
    const input = $("topbar-search");
    input.addEventListener("input", () => onFilter(input.value.trim().toLowerCase()));
    input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        const host = $("search-results");
        if (host.hidden) { onFilter(input.value.trim().toLowerCase()); return; }
        this.#activeResult((this.#selectedResult ?? 0) + 1, true);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        this.#activeResult((this.#selectedResult ?? 0) - 1, true);
      } else if (event.key === "Enter") {
        const host = $("search-results");
        const rows = [...host.querySelectorAll(".search-result")];
        if (host.hidden || !rows.length) return;
        event.preventDefault();
        this.#pickResult(rows[this.#selectedResult ?? 0].dataset.id);
      } else if (event.key === "Escape") {
        this.#hideSearch();
        input.blur();
      }
    });
    input.addEventListener("blur", () => {
      /* Clicking a result fires blur before click; hide now rather than let the
       * click target disappear. */
      setTimeout(() => this.#hideSearch(), 120);
    });

    $("graph-limit").addEventListener("change", (event) => {
      const value = Number(event.target.value);
      set({ graphLimit: value });
      savePref("graphLimit", value);
      this.showWholeGraph();
    });
    $("hops").addEventListener("change", (event) => {
      const value = Number(event.target.value);
      set({ hops: value });
      savePref("hops", value);
      if (state.selected) this.focusEntity(state.selected);
      else this.showWholeGraph();
    });

    /* graph controls */
    $("g-relayout").addEventListener("click", () => this.graph.relayout());
    $("g-fit").addEventListener("click", () => this.graph.fit());
    $("g-zoom-in").addEventListener("click", () => this.graph.zoomBy(1.25));
    $("g-zoom-out").addEventListener("click", () => this.graph.zoomBy(1 / 1.25));
    $("g-labels").addEventListener("click", (event) => {
      const on = event.currentTarget.getAttribute("aria-pressed") !== "true";
      event.currentTarget.setAttribute("aria-pressed", on ? "true" : "false");
      set({ showLabels: on });
      savePref("labels", on);
      this.graph.setLabels(on);
    });
    $("g-legend-toggle").addEventListener("click", (event) => {
      const on = event.currentTarget.getAttribute("aria-pressed") !== "true";
      event.currentTarget.setAttribute("aria-pressed", on ? "true" : "false");
      $("legend").hidden = !on;
      set({ legendOpen: on });
      savePref("legend", on);
    });
    $("graph-canvas").addEventListener("click", (event) => {
      if (event.target === $("graph-canvas")) {
        setCollection("cited", new Set());
        this.graph.setCited([]);
      }
    });

    /* asking */
    $("composer").addEventListener("submit", (event) => {
      event.preventDefault();
      this.ask();
    });
    $("qa-input").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        this.ask();
      }
    });
    $("qa-input").addEventListener("input", () => this.#autogrow($("qa-input")));
    $("samples-btn").addEventListener("click", (event) => {
      const on = event.currentTarget.getAttribute("aria-pressed") !== "true";
      event.currentTarget.setAttribute("aria-pressed", on ? "true" : "false");
      $("samples").hidden = !on;
    });
    $("copy-answer").addEventListener("click", async () => {
      const text = state.answer?.text || state.streaming;
      if (!text) {
        toast("nothing to copy yet", "bad");
        return;
      }
      try {
        await navigator.clipboard.writeText(text);
        toast("answer copied to the clipboard", "ok");
      } catch {
        toast("the browser blocked clipboard access", "bad");
      }
    });

    /* model key panel */
    $("key-save").addEventListener("click", () => {
      const key = $("key-input").value.trim();
      if (!key) {
        $("key-note").textContent = "Paste a key first.";
        return;
      }
      this.#postRag({ key, backend: "nvidia" });
    });
    $("key-input").addEventListener("keydown", (event) => {
      if (event.key === "Enter") $("key-save").click();
    });
    $("key-forget").addEventListener("click", () => this.#postRag({ key: "", backend: "auto" }));
    $("use-local").addEventListener("click", () => this.#postRag({ backend: "ollama" }));

    /* theme */
    $("theme-btn").addEventListener("click", () => this.toggleTheme());

    /* tabs */
    for (const tab of document.querySelectorAll(".tab[data-tab]")) {
      tab.addEventListener("click", () => this.showTab(tab.dataset.tab));
      tab.addEventListener("keydown", (event) => {
        const tabs = [...document.querySelectorAll(".tab[data-tab]")];
        const at = tabs.indexOf(tab);
        let next = null;
        if (event.key === "ArrowRight" || event.key === "ArrowDown") next = (at + 1) % tabs.length;
        else if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = (at - 1 + tabs.length) % tabs.length;
        else if (event.key === "Home") next = 0;
        else if (event.key === "End") next = tabs.length - 1;
        if (next === null) return;
        event.preventDefault();
        tabs[next].focus();
        this.showTab(tabs[next].dataset.tab);
      });
    }

    /* mobile view switch */
    for (const button of $("mobile-nav").querySelectorAll("button")) {
      button.addEventListener("click", () => this.setView(button.dataset.view));
    }

    $("palette-btn").addEventListener("click", () => this.palette.open());
  }

  toggleTheme() {
    const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
    document.documentElement.dataset.theme = next;
    savePref("theme", next);
    $("theme-icon").innerHTML = next === "light"
      ? `<circle cx="12" cy="12" r="4.2"/><path d="M12 2.5v2.2M12 19.3v2.2M4.2 4.2l1.6 1.6M18.2 18.2l1.6 1.6M2.5 12h2.2M19.3 12h2.2M4.2 19.8l1.6-1.6M18.2 5.8l1.6-1.6"/>`
      : `<path d="M20 13.5A8 8 0 0 1 10.5 4a8.2 8.2 0 1 0 9.5 9.5Z" stroke-linejoin="round"/>`;
  }

  showTab(name) {
    set({ tab: name });
    for (const tab of document.querySelectorAll(".tab[data-tab]")) {
      const on = tab.dataset.tab === name;
      tab.setAttribute("aria-selected", on ? "true" : "false");
    }
    for (const panel of document.querySelectorAll(".tabpanel")) {
      panel.hidden = panel.id !== `tab-${name}`;
    }
    $("tabpanels").scrollTop = 0;
  }

  setView(view) {
    if (!["ask", "graph"].includes(view)) view = "graph";
    set({ view });
    savePref("view", view);
    $("app").dataset.view = view;
    for (const button of $("mobile-nav").querySelectorAll("button")) {
      button.setAttribute("aria-selected", button.dataset.view === view ? "true" : "false");
    }
  }

  /* ── splitters ───────────────────────────────────────────────────────── */

  /** Nudge the answer panel's width, in pixels. The splitter is a 1px target on
   *  a wide screen, so the command palette offers the same resize to anyone who
   *  would rather not go looking for the handle. */
  resizePanel(delta) {
    this.setPanelWidth($("qa").getBoundingClientRect().width + delta);
    this.graph.fit();
  }

  /** The one place the answer panel width is set: clamped to what the viewport
   *  can spare, written to the grid variable, remembered, and reflected in the
   *  splitter's ARIA values so a screen reader announces the new size. */
  setPanelWidth(width) {
    const room = window.innerWidth - 300 - 360;
    const low = 320;
    const high = Math.max(460, Math.min(960, room));
    const clamped = Math.max(low, Math.min(high, Math.round(width)));
    document.documentElement.style.setProperty("--w-qa", `${clamped}px`);
    savePref("qaWidth", clamped);
    const element = $("split-right");
    element.setAttribute("aria-valuemin", String(low));
    element.setAttribute("aria-valuemax", String(high));
    element.setAttribute("aria-valuenow", String(clamped));
    element.setAttribute("aria-valuetext", `${clamped} pixels`);
    return clamped;
  }

  #wireSplitters() {
    /* The same numbers the stylesheet starts from, so a double-click, a Home
     * key and a cleared preference all land in the same place. */
    const DEFAULT = 520;
    const element = $("split-right");
    const pane = () => $("qa");
    const apply = (width) => this.setPanelWidth(width);

    let startX = 0;
    let startWidth = 0;

    // Announce the width the pane actually has, which is the restored
    // preference on a reload rather than the stylesheet default.
    this.setPanelWidth(pane().getBoundingClientRect().width);

    element.addEventListener("pointerdown", (event) => {
      // The grid owns the width, so the drag has to start from what is on
      // screen rather than from the stylesheet default.
      event.preventDefault();
      element.setPointerCapture(event.pointerId);
      element.classList.add("is-dragging");
      startX = event.clientX;
      startWidth = pane().getBoundingClientRect().width;
    });

    element.addEventListener("pointermove", (event) => {
      if (!element.hasPointerCapture(event.pointerId)) return;
      apply(startWidth - (event.clientX - startX));
    });

    const end = (event) => {
      element.classList.remove("is-dragging");
      try { element.releasePointerCapture(event.pointerId); } catch { /* already released */ }
      this.graph.fit();
    };
    element.addEventListener("pointerup", end);
    element.addEventListener("pointercancel", end);

    /* Keyboard, because a 1px target is a mouse-only affordance and because
     * arrow keys are how anyone resizes a pane in an IDE. Home puts it back
     * to the default width. */
    element.addEventListener("keydown", (event) => {
      const step = event.shiftKey ? 64 : 16;
      const current = pane().getBoundingClientRect().width;
      let next = null;
      if (event.key === "ArrowLeft") next = current + step;
      else if (event.key === "ArrowRight") next = current - step;
      else if (event.key === "Home" || event.key === "Enter" || event.key === " ") next = DEFAULT;
      if (next === null) return;
      event.preventDefault();
      apply(next);
      this.graph.fit();
    });

    element.addEventListener("dblclick", () => {
      apply(DEFAULT);
      this.graph.fit();
    });
  }

  /* ── keyboard ────────────────────────────────────────────────────────── */

  #wireKeyboard() {
    document.addEventListener("keydown", (event) => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "");
      const mod = event.metaKey || event.ctrlKey;

      if (event.key === "Escape") {
        if (this.palette.isOpen) { this.palette.close(); return; }
        if (this.reports.isOpen) { this.reports.close(); return; }
        if (typing) document.activeElement.blur();
        return;
      }
      if (mod && event.key.toLowerCase() === "k") {
        event.preventDefault();
        this.palette.toggle();
        return;
      }
      if (mod && event.key === "Enter") {
        event.preventDefault();
        this.ask();
        return;
      }
      if (typing || mod) return;

      if (event.key === "/") {
        event.preventDefault();
        $("topbar-search").focus();
        $("topbar-search").select();
      } else if (event.key === "?" ) {
        event.preventDefault();
        this.palette.open("");
      } else if (event.key === "f") {
        this.graph.fit();
      } else if (event.key === "l") {
        $("g-labels").click();
      } else if (event.key === "r") {
        this.graph.relayout();
      } else if (["1", "2", "3", "4"].includes(event.key)) {
        this.showTab(["answer", "sources", "trace", "provenance"][Number(event.key) - 1]);
      }
    });
  }

  /* ── command palette ─────────────────────────────────────────────────── */

  get palette() {
    if (!this._palette) this._palette = new Palette(this);
    return this._palette;
  }
}

/* ── command palette ─────────────────────────────────────────────────────── */

const ICON_COMMAND = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M5 6h14M5 12h14M5 18h9"/></svg>`;

class Palette {
  constructor(app) {
    this.app = app;
    this.root = $("palette");
    this.scrim = $("palette-scrim");
    this.input = $("palette-input");
    this.list = $("palette-list");
    this.items = [];
    this.at = 0;

    this.input.addEventListener("input", () => this.#render(this.input.value));
    this.input.addEventListener("keydown", (event) => this.#onKey(event));
    this.scrim.addEventListener("click", () => this.close());
  }

  get isOpen() {
    return this.root.classList.contains("is-open");
  }

  open(prefill = "") {
    this.lastFocus = document.activeElement;
    this.root.hidden = false;
    this.scrim.hidden = false;
    requestAnimationFrame(() => {
      this.root.classList.add("is-open");
      this.scrim.classList.add("is-open");
    });
    this.input.value = prefill;
    this.#render(prefill);
    this.input.focus();
  }

  close() {
    this.root.classList.remove("is-open");
    this.scrim.classList.remove("is-open");
    setTimeout(() => {
      this.root.hidden = true;
      this.scrim.hidden = true;
    }, 160);
    this.lastFocus?.focus?.();
  }

  toggle() {
    if (this.isOpen) this.close();
    else this.open();
  }

  #render(query) {
    const app = this.app;
    const commands = [
      { label: "Show the whole graph", group: "graph", run: () => app.showWholeGraph() },
      { label: "Fit the graph to the view", group: "graph", keys: "F", run: () => app.graph.fit() },
      { label: "Re-run the graph layout", group: "graph", keys: "R", run: () => app.graph.relayout() },
      { label: app.graph.showLabels ? "Hide node labels" : "Show node labels", group: "graph", keys: "L", run: () => $("g-labels").click() },
      { label: state.legendOpen ? "Hide the legend" : "Show the legend", group: "graph", run: () => $("g-legend-toggle").click() },
      { label: "Open the canned reports", group: "view", keys: "", run: () => app.reports.open() },
      { label: "Widen the answer panel", group: "view", keys: "⇧←", run: () => app.resizePanel(80) },
      { label: "Narrow the answer panel", group: "view", keys: "⇧→", run: () => app.resizePanel(-80) },
      { label: "Reset the answer panel width", group: "view", keys: "", run: () => { app.setPanelWidth(520); app.graph.fit(); } },
      { label: "Copy the last answer", group: "answer", run: () => $("copy-answer").click() },
      { label: "Show the answer", group: "answer", keys: "1", run: () => app.showTab("answer") },
      { label: "Show the cited sources", group: "answer", keys: "2", run: () => app.showTab("sources") },
      { label: "Show the retrieval trace", group: "answer", keys: "3", run: () => app.showTab("trace") },
      { label: "Show the provenance ledger", group: "answer", keys: "4", run: () => app.showTab("provenance") },
      { label: "Toggle light and dark", group: "view", run: () => app.toggleTheme() },
      { label: "Focus the question box", group: "ask", keys: "⌘↵", run: () => $("qa-input").focus() },
    ];

    /* One command per issuer and per sample question: the palette is the
     * shortest path from "I have a ticker" to "I am looking at that issuer". */
    for (const company of state.companies || []) {
      commands.push({
        label: `Focus ${company.ticker} — ${company.name}`,
        group: "issuer",
        run: () => app.focusEntity(company.ticker),
      });
    }

    const term = query.trim().toLowerCase();
    this.items = term
      ? commands.filter((command) => `${command.label} ${command.group}`.toLowerCase().includes(term))
      : commands.slice(0, 12);
    this.at = 0;
    this.#paint();
  }

  #paint() {
    const host = clear(this.list);
    if (!this.items.length) {
      host.append(el("li", { class: "palette__item", text: "No matching command" }));
      return;
    }
    this.items.forEach((item, index) => {
      host.append(el("li", {}, el("button", {
        class: "palette__item",
        type: "button",
        role: "option",
        "aria-selected": index === this.at ? "true" : "false",
        onclick: () => {
          this.close();
          item.run();
        },
      }, [
        el("span", { html: ICON_COMMAND }),
        el("span", { text: item.label }),
        item.keys ? el("kbd", { class: "kbd", text: item.keys }) : el("span", { class: "palette__group", text: item.group }),
      ])));
    });
  }

  #onKey(event) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const delta = event.key === "ArrowDown" ? 1 : -1;
      this.at = (this.at + delta + this.items.length) % this.items.length;
      this.#paint();
      this.list.children[this.at]?.querySelector("button")?.scrollIntoView({ block: "nearest" });
    } else if (event.key === "Enter") {
      event.preventDefault();
      const item = this.items[this.at];
      if (item) {
        this.close();
        item.run();
      }
    }
  }
}

/* ── helpers ─────────────────────────────────────────────────────────────── */

function shortModel(model) {
  return String(model || "").split("/").pop();
}

function shortCompany(name) {
  return String(name || "").replace(/\b(CORPORATION|INC|INCORPORATED|CORP|LTD|LLC|PLC)\b\.?/gi, "").trim() || name;
}

function highlightTerm(text, term) {
  const safe = document.createElement("span");
  safe.textContent = text ?? "";
  if (!term) return safe.innerHTML;
  const escaped = term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return safe.innerHTML.replace(new RegExp(escaped, "gi"), (hit) => `<mark class="hit">${hit}</mark>`);
}

const app = new App();
app.start().catch((error) => {
  console.error(error);
  toast(`the UI failed to start: ${error.message}`, "bad");
});

export { app };
