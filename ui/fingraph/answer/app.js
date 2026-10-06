/* FinGraph Answer View — dedicated GraphRAG answer experience.
 *
 * Reuses the same collaborators as the Studio: api.js, graph.js, answer.js,
 * reports.js, runDetails.js, store.js, util.js — but with an answer-first
 * layout and automatic question execution from the URL.
 */

import {
  askStream, fetchCompanies, fetchEntities, fetchGraph,
  fetchRoute, fetchStats, saveRagState,
} from "/static/api.js";
import { AnswerView } from "/static/answer.js";
import { GraphView } from "/static/graph.js";
import { ReportsPanel } from "/static/reports.js";
import { RunDetailsPanel } from "/static/runDetails.js";
import { ExecutionCard } from "/static/executionCard.js";

import { set, setCollection, state, subscribe } from "/static/store.js";
import {
  $, announce, clear, debounce, el, fmtNumber, loadPref, prettyType,
  savePref, toast, typeColor, stampLogos,
} from "/static/util.js";

const processOk = true;
const processNote = "";
const currentTrace = null;
const currentRoute = "KNOWN";
const currentTicker = null;

const PROCESS_PLANS = {
  KNOWN: ["routing", "traversal", "synthesis"],
  COLD_START: ["routing", "fetching", "extraction", "stitching", "traversal", "synthesis"],
};

const processPlan = (route) => PROCESS_PLANS[route] || PROCESS_PLANS.KNOWN;

const ROUTE_PROBE_MS = 1000;

class AnswerApp {
  constructor() {
    this.graph = new GraphView($("graph-canvas"), {
      onSelect: (node) => {
        set({ selected: node?.id || null });
        this.#renderEntities();
      },
      tooltip: $("graph-tooltip"),
      tooltipHost: $("graph-panel"),
      inspector: $("graph-inspector"),
      sidePanel: $("graph-side-panel"),
    });
    this.answer = new AnswerView({
      onCite: (id) => this.focusCitation(id),
    });
    this.reports = new ReportsPanel();
    this.runDetails = new RunDetailsPanel();
    this.executionCard = null;
    this.abort = null;
    this.timer = null;
    this.entityTerm = "";
    this.currentRoute = "KNOWN";
    this.currentTicker = null;
  }

  async #checkAuth() {
    try {
      const res = await fetch("/api/reports", { credentials: "include", cache: "no-store" });
      return res.ok;
    } catch {
      return false;
    }
  }

  async start() {
    stampLogos();
    this.#restorePreferences();
    this.#wireChrome();
    this.#wireSplitters();
    this.#wireStore();
    this.answer.empty();
    this.#renderSamples(this.sampleQuestions([]));

    this.#renderGraphState();

    const [stats, companies] = await Promise.allSettled([fetchStats(), fetchCompanies()]);
    if (stats.status === "fulfilled") this.#applyStats(stats.value);
    else this.#statsFailed(stats.reason);
    if (companies.status === "fulfilled") {
      set({ companies: companies.value.companies || [] });
      this.#renderIssuers();
      this.#renderSamples(this.sampleQuestions(companies.value.companies || []));
    }

    await this.showWholeGraph();

    const shared = new URLSearchParams(location.search).get("q");
    if (shared) {
      $("qa-input").value = shared;
      await this.ask();
    }
  }

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

    const chip = $("model-chip-inline");
    chip.textContent = where ? `${shortModel(model)} · ${where}` : "answering disabled";
    chip.className = `chip chip--dot ${where ? "" : "chip--bad"}`;
    chip.title = state_?.rag_reason || "";

    set({ rag: state_ });

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

  async loadEntities() {
    const term = this.entityTerm || "";
    try {
      const data = await fetchEntities(term, 60);
      set({ entities: data.entities || [] });
      this.#renderEntities();
    } catch (error) {
      toast(`entity search failed: ${error.message}`, "bad");
    }
  }

  openSearch() {
    this.graph?.openSidePanel?.();
    const results = $("entity-results");
    if (results) results.hidden = false;
    $("entity-search")?.setAttribute("aria-expanded", "true");
    $("side-panel-search")?.classList.add("is-expanded");
    this.#renderEntities();
  }

  closeSearch({ keepTerm = true } = {}) {
    const results = $("entity-results");
    if (results) results.hidden = true;
    $("entity-search")?.setAttribute("aria-expanded", "false");
    $("side-panel-search")?.classList.remove("is-expanded");
    if (!keepTerm) this.#clearFilter();
  }

  #renderEntities() {
    const host = clear($("entity-list"));
    const term = (this.entityTerm || "").toLowerCase();
    const rows = state.entities.filter((entity) => {
      if (!term) return true;
      return [entity.name, entity.entity_type, entity.description]
        .some((value) => String(value ?? "").toLowerCase().includes(term));
    });

    $("entity-count").textContent = term
      ? `${fmtNumber(rows.length)} ${rows.length === 1 ? "match" : "matches"}`
      : `Type to search ${fmtNumber(state.stats?.nodes ?? 0)} entities`;

    if (!rows.length) {
      host.append(el("li", {}, el("div", { class: "empty", text: term
        ? "No matching entities — try a different term"
        : `Type to search ${fmtNumber(state.stats?.nodes ?? 0)} entities (companies, filings, segments, metrics, events)` })));
      return;
    }

    const fragment = document.createDocumentFragment();
    for (const entity of rows) {
      const type = entity.entity_type || "";
      const isSelected = state.selected === entity.id;
      const labelHint = entity.label_hint ? ` · ${entity.label_hint}` : "";
      const item = el("li", {}, el("button", {
        class: "entity",
        type: "button",
        role: "option",
        tabindex: -1,
        "aria-selected": isSelected ? "true" : "false",
        "aria-current": isSelected ? "true" : "false",
        title: entity.description || entity.name,
        onclick: () => this.#chooseEntity(entity.id),
      }, [
        el("span", { class: "entity__dot", style: `background:${typeColor(type)}` }),
        el("span", { class: "entity__name", html: highlightTerm(entity.name, term) }),
        el("span", { class: "entity__type", text: prettyType(type) + labelHint }),
      ]));
      fragment.append(item);
    }
    host.append(fragment);
  }

  async showWholeGraph() {
    set({ graphLoading: true });
    try {
      const payload = await fetchGraph({ hops: state.hops, limit: Number(state.graphLimit) });
      set({ graph: payload, selected: null });
      const counts = this.graph.setData(payload, { fresh: true });
      $("graph-count").textContent = `${fmtNumber(counts.nodes)} nodes · ${fmtNumber(counts.links)} links`;
      this.#showCitationBadge();
    } catch (error) {
      toast(`graph query failed: ${error.message}`, "bad");
    } finally {
      set({ graphLoading: false });
    }
  }

  async focusEntity(id) {
    set({ selected: id });
    this.#renderEntities();
    if (window.matchMedia("(max-width: 859px)").matches) this.setView("graph");
    try {
      const payload = await fetchGraph({ seed: id, hops: state.hops, limit: Number(state.graphLimit) });
      set({ graph: payload });
      const counts = this.graph.setData(payload, { fresh: true });
      $("graph-count").textContent = `${fmtNumber(counts.nodes)} nodes · ${fmtNumber(counts.links)} links`;
      this.#showCitationBadge();
    } catch (error) {
      toast(`graph query failed: ${error.message}`, "bad");
    }
  }

  #chooseEntity(id) {
    this.closeSearch();
    if (!this.graph.focus(id)) {
      this.focusEntity(id);
    } else {
      set({ selected: id });
      this.#renderEntities();
    }
  }

  focusCitation(id) {
    if (this.graph.focus(id)) {
      set({ selected: id });
      this.#renderEntities();
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

    // Check authentication before asking
    const authed = await this.#checkAuth();
    if (!authed) {
      const returnUrl = encodeURIComponent(location.pathname + location.search);
      location.assign(`/auth?return=${returnUrl}`);
      return;
    }

    let route = "KNOWN";
    let ticker = null;

    set({ busy: true, question, streaming: "" });
    try {
      savePref("lastQuestion", question);
      this.showTab("answer");
      this.#setAskLabel(true);
      $("ask-btn").disabled = true;
      this.#startTimer();

      const probeCtl = new AbortController();
      const probeTimer = setTimeout(() => probeCtl.abort(), ROUTE_PROBE_MS);
      try {
        const probe = await fetchRoute(question, { signal: probeCtl.signal });
        route = probe.route || "KNOWN";
        ticker = probe.ticker;
      } catch {
      } finally {
        clearTimeout(probeTimer);
      }

      this.currentRoute = route;
      this.currentTicker = ticker;

      this.answer.pending(question);

      if (!this.executionCard) {
        this.executionCard = new ExecutionCard();
      }
      this.executionCard.show(question, route, ticker, () => { });

      await this.#askStreaming(question);
    } catch (error) {
      // Handle authentication error - redirect to auth page
      if (error.message && error.message.includes("401")) {
        const returnUrl = encodeURIComponent(location.pathname + location.search);
        location.assign(`/auth?return=${returnUrl}`);
        return;
      }
      if (error.name === "AbortError") {
        this.answer.error("cancelled");
        this.executionCard?.handleError();
        this.executionCard?.hide();
      } else {
        this.answer.error(`Query failed: ${error.message}`);
        toast(`query failed: ${error.message}`, "bad");
        announce("Query failed");
        this.executionCard?.handleError();
        this.executionCard?.hide();
      }
    } finally {
      this.abort = null;
      this.#stopTimer();
      set({ busy: false });
      this.processOk = true;
      this.processNote = "";
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
      if (data.step === "start") {
        return;
      }
      if (data.step === "ambiguous") {
        return;
      }

      if (data.message) this.#setTimerLabel(data.message);

      this.executionCard?.handleStatusEvent(data);
      return;
    }
    if (type === "token") {
      if (state.streaming === "") {
        this.executionCard?.handleTokenEvent();
      }
      set({ streaming: state.streaming + (data.token ?? "") });
      this.answer.appendStream(state.streaming);
      return;
    }
    if (type === "error") {
      this.answer.error(data.error || "the server reported an error");
      this.executionCard?.handleError();
      this.executionCard?.hide();
      return;
    }
    if (type !== "done") return;

    if (data.status === "error") {
      this.answer.error(data.error || "the server reported an error");
      this.executionCard?.handleError();
      this.executionCard?.hide();
      return;
    }
    if (data.degraded) {
    }

    this.#finish({
      ...data,
      question: state.question,
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

    const cited = (result.used_tags || []).map((tag) => result.tag_map?.[tag]).filter(Boolean);
    setCollection("cited", new Set(cited));
    this.graph.highlightAnswer(result);
    this.#showCitationBadge();

    const trace = this.#buildExecutionTrace(result);
    this.currentTrace = trace;

    this.executionCard?.handleDoneEvent(result);
    this.executionCard?.hide();

    announce(["answer", result.verdict?.toLowerCase() || result.route?.toLowerCase() || "returned",
      ...Object.entries(result.provenance_mix || {}).map(([key, value]) => `${key} ${value}`),
      ...(result.violations || [])].filter(Boolean).join(". "));
  }

  #buildExecutionTrace(result) {
    if (!result) return null;

    const now = Date.now();
    const startedAt = now - (result.latency_ms || 0);

    const stageMap = {
      routing: 'routing',
      fetching: 'fetching',
      extracting: 'extraction',
      stitching: 'stitching',
      traversing: 'traversal',
      traversal: 'traversal',
      synthesis: 'synthesis'
    };

    const steps = [];
    const stageLatencies = result.stage_latencies_ms || {};

    const isColdStart = result.route === 'COLD_START';
    const routeStages = isColdStart
      ? ['routing', 'fetching', 'extraction', 'stitching', 'traversal', 'synthesis']
      : ['routing', 'traversal', 'synthesis'];

    for (const stage of routeStages) {
      const latency = stageLatencies[stage] || 0;
      const stepKey = stageMap[stage] || stage;
      const status = latency > 0 ? 'completed' : 'pending';

      steps.push({
        id: stepKey,
        name: this.#getStepLabel(stepKey),
        status,
        startedAt: latency > 0 ? startedAt : null,
        completedAt: latency > 0 ? startedAt + latency : null,
        durationMs: latency > 0 ? latency : null,
        message: this.#getStepMessage(stepKey),
        details: this.#getStepDetails(stepKey, result),
        children: this.#getStepChildren(stepKey, result)
      });
    }

    if (result.answer && steps.length > 0) {
      const lastStep = steps[steps.length - 1];
      if (lastStep.status === 'pending') {
        lastStep.status = 'completed';
        lastStep.startedAt = startedAt + (result.latency_ms || 0) - 100;
        lastStep.completedAt = now;
        lastStep.durationMs = 100;
      }
    }

    return {
      runId: `run-${startedAt}-${Math.random().toString(36).slice(2, 9)}`,
      question: result.question || state.question || '',
      ticker: result.ticker,
      company: result.ticker,
      status: result.status === 'error' ? 'failed' : 'success',
      startedAt,
      completedAt: now,
      durationMs: result.latency_ms,
      steps,
      answer: result.answer,
      provenance: result.provenance,
      stageLatencies,
      graphMetrics: result.graph_metrics,
      error: result.error
    };
  }

  #getStepLabel(stepKey) {
    const labels = {
      routing: 'Entity Resolution',
      fetching: 'Filing Acquisition',
      extraction: 'Fact Extraction',
      stitching: 'Graph Stitching',
      traversal: 'Graph Retrieval',
      synthesis: 'Answer Synthesis'
    };
    return labels[stepKey] || stepKey;
  }

  #getStepMessage(stepKey) {
    const messages = {
      routing: 'Matching the question to a known entity in the graph',
      fetching: 'Fetching the latest SEC filing from EDGAR',
      extraction: 'Extracting financial facts and relationships from the filing',
      stitching: 'Stitching extracted facts onto the knowledge graph',
      traversal: 'Traversing the graph to retrieve relevant context',
      synthesis: 'Composing the final answer from retrieved evidence'
    };
    return messages[stepKey] || '';
  }

  #getStepDetails(stepKey, result) {
    const details = {};
    const stageLatencies = result.stage_latencies_ms || {};
    const latency = stageLatencies[stepKey];
    if (latency) {
      details['Latency'] = `${latency}ms`;
    }
    return details;
  }

  #getStepChildren(stepKey, result) {
    const children = [];
    const stageLatencies = result.stage_latencies_ms || {};

    switch (stepKey) {
      case 'routing':
        if (result.ticker) {
          children.push({
            id: 'routing-company',
            name: 'Company Resolved',
            status: 'completed',
            startedAt: null,
            completedAt: null,
            durationMs: 0,
            message: `Identified ${result.ticker}`,
            details: { Ticker: result.ticker }
          });
        }
        break;

      case 'traversal':
        if (result.graph_metrics) {
          children.push({
            id: 'traversal-graph',
            name: '2-Hop Graph Traversal',
            status: 'completed',
            startedAt: null,
            completedAt: null,
            durationMs: stageLatencies?.traversal || 0,
            message: `Retrieved ${result.graph_metrics.nodesRetrieved} nodes, ${result.graph_metrics.edgesTraversed} edges`,
            details: {
              'Max Hop Depth': result.graph_metrics.maxHopDepth,
              'Nodes Retrieved': result.graph_metrics.nodesRetrieved,
              'Edges Traversed': result.graph_metrics.edgesTraversed
            }
          });
        }
        break;

      case 'synthesis':
        if (result.provenance) {
          children.push({
            id: 'synthesis-context',
            name: 'Context Assembly',
            status: 'completed',
            startedAt: null,
            completedAt: null,
            durationMs: 0,
            message: `Assembled context from ${result.provenance.length} evidence sources`,
            details: {
              'Evidence Sources': result.provenance.length,
              'Graph Facts': result.graph_metrics?.nodesRetrieved || '—'
            }
          });
        }
        if (result.stage_latencies_ms?.synthesis) {
          children.push({
            id: 'synthesis-llm',
            name: 'LLM Answer Generation',
            status: 'completed',
            startedAt: null,
            completedAt: null,
            durationMs: result.stage_latencies_ms.synthesis,
            message: 'Generated answer with citations',
            details: {
              'Model': 'Nemotron-3-Ultra',
              'Latency': `${result.stage_latencies_ms.synthesis}ms`,
              'Citations': result.provenance?.length || 0
            }
          });
        }
        break;
    }

    return children.length > 0 ? children : undefined;
  }

  #showCitationBadge() {
    const badge = $("graph-cited");
    if (!badge) return;
    const { nodes, edges } = this.graph.citationSpread;
    badge.hidden = nodes === 0;
    $("clear-focus").hidden = nodes === 0;
    if (!nodes) return;
    badge.textContent = `${nodes} cited · ${edges} link${edges === 1 ? "" : "s"}`;
  }

  #startTimer() {
    const chip = $("wait-timer");
    chip.hidden = false;
    this.timerStarted = Date.now();
    this.phaseMessage = "Composing";
    clearInterval(this.timer);
    this.timer = setInterval(() => this.#tickTimer(), 1000);
    this.#tickTimer();
  }

  #tickTimer() {
    $("wait-timer").textContent = "Composing";
  }

  #setTimerLabel(message) {
    const base = message || "Composing";
    this.phaseMessage = String(base).replace(/…$/, "");
    this.#tickTimer();
  }

  #stopTimer() {
    clearInterval(this.timer);
    $("wait-timer").hidden = true;
  }

  #setAskLabel(busy) {
    $("ask-label").textContent = busy ? "Asking…" : "Ask";
  }

  sampleQuestions(companies) {
    if (!companies.length) {
      return [
        "What was Apple's total net sales in fiscal 2026?",
        "What did Apple report for gross profit, operating income and R&D in fiscal 2026?",
        "How does Apple's revenue break down across its product and geographic segments?",
        "What was Microsoft's revenue growth in the latest period?",
      ];
    }
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

  #restorePreferences() {
    const root = document.documentElement;

    const qaWidth = loadPref("qaWidth", null);
    if (qaWidth) root.style.setProperty("--w-qa", `${qaWidth}px`);

    $("hops").value = String(state.hops);
    $("qa-input").value = state.lastQuestion || "";
    this.setView(state.view === "ask" ? "ask" : "graph");
    this.graph.setLabels(state.showLabels);
  }

  #wireChrome() {
    const input = $("entity-search");
    const loadingEl = $("entity-search-loading");

    const search = debounce(async (term) => {
      this.entityTerm = term;
      loadingEl.hidden = false;
      await this.loadEntities();
      loadingEl.hidden = true;
    }, 180);

    input.addEventListener("focus", () => this.openSearch());
    input.addEventListener("input", () => {
      this.openSearch();
      search(input.value.trim().toLowerCase());
    });

    input.addEventListener("keydown", (event) => {
      const rows = [...$("entity-list").querySelectorAll(".entity")];

      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        if (!rows.length) return;
        event.preventDefault();
        const step = event.key === "ArrowDown" ? 1 : -1;
        const activeIdx = rows.indexOf(document.activeElement);
        const next = Math.max(0, Math.min(rows.length - 1, (activeIdx === -1 ? -1 : activeIdx) + step));
        rows[next]?.focus();
      } else if (event.key === "Enter" && rows.length) {
        event.preventDefault();
        const activeIdx = rows.indexOf(document.activeElement);
        (activeIdx === -1 ? rows[0] : rows[activeIdx]).click();
      } else if (event.key === "Escape") {
        event.preventDefault();
        this.closeSearch();
        input.blur();
      }
    });

    document.addEventListener("pointerdown", (event) => {
      const results = $("entity-results");
      if (!results || results.hidden) return;
      const searchBox = $("side-panel-search") || $("graph-side-panel");
      if (searchBox && !searchBox.contains(event.target)) this.closeSearch();
    });

    $("show-all").addEventListener("click", () => {
      this.closeSearch();
      this.showWholeGraph();
    });
    $("clear-focus").addEventListener("click", () => {
      setCollection("cited", new Set());
      this.graph.clearAnswerHighlight();
      this.#showCitationBadge();
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
      this.graph.setMaxHops(value);
      if (state.selected) this.focusEntity(state.selected);
      else this.showWholeGraph();
    });

    $("g-relayout")?.addEventListener("click", () => this.graph.relayout());
    $("g-fit")?.addEventListener("click", () => this.graph.fit());
    $("g-zoom-in")?.addEventListener("click", () => this.graph.zoomBy(1.25));
    $("g-zoom-out")?.addEventListener("click", () => this.graph.zoomBy(1 / 1.25));
    $("g-labels")?.addEventEventListener("click", (event) => {
      const on = event.currentTarget.getAttribute("aria-pressed") !== "true";
      event.currentTarget.setAttribute("aria-pressed", on ? "true" : "false");
      set({ showLabels: on });
      savePref("labels", on);
      this.graph.setLabels(on);
    });
    $("g-filter-btn")?.addEventListener("click", () => {
      this.graph.toggleSidePanel();
    });
    $("g-legend-toggle")?.addEventListener("click", (event) => {
      const on = event.currentTarget.getAttribute("aria-pressed") !== "true";
      event.currentTarget.setAttribute("aria-pressed", on ? "true" : "false");
      if ($("legend")) $("legend").hidden = !on;
      set({ legendOpen: on });
      savePref("legend", on);
    });
    $("graph-canvas").addEventListener("click", (event) => {
      if (event.target === $("graph-canvas")) {
        setCollection("cited", new Set());
        this.graph.setCited([]);
        this.#showCitationBadge();
      }
    });

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

    $("theme-btn").addEventListener("click", () => this.toggleTheme());

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

    for (const button of $("mobile-nav").querySelectorAll("button")) {
      button.addEventListener("click", () => this.setView(button.dataset.view));
    }

    $("palette-btn").addEventListener("click", () => this.palette.open());

    document.addEventListener("keydown", (event) => {
      if (event.key === "/" && event.target.tagName !== "INPUT" && event.target.tagName !== "TEXTAREA" && !event.metaKey && !event.ctrlKey) {
        event.preventDefault();
        this.setView("graph");
        input.focus();
        this.openSearch();
      }
    });
  }

  #clearFilter() {
    $("entity-search").value = "";
    this.entityTerm = "";
    $("entity-search-loading").hidden = true;
    this.loadEntities();
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
    set({ view });
    savePref("view", view);
    $("app").dataset.view = view;
    for (const button of $("mobile-nav").querySelectorAll("button")) {
      button.setAttribute("aria-selected", button.dataset.view === view ? "true" : "false");
    }
  }

  resizePanel(delta) {
    this.setPanelWidth("right", $("qa").getBoundingClientRect().width + delta);
    this.graph.fit();
  }

  #graphFloor() {
    const width = window.innerWidth;
    if (width < 860) return 0;
    if (width < 1180) return 320;
    return 360;
  }

  #liveWidth() {
    if ($("split-right").offsetParent === null) return 0;
    return $("qa").getBoundingClientRect().width;
  }

  #splitGutter() {
    return $("split-right").offsetParent === null ? 0 : 1;
  }

  #fitPanel(wanted, fair = false) {
    if ($("split-right").offsetParent === null) return null;

    const budget = Math.max(0, window.innerWidth - this.#graphFloor() - this.#splitGutter());
    const cap = 960;
    const prefMin = 320;
    let mine = Math.max(0, Math.min(cap, Math.round(wanted)));

    if (fair) {
      if (mine > budget) mine = budget;
    } else {
      mine = Math.max(Math.min(prefMin, budget), Math.min(cap, budget, mine));
    }

    return mine;
  }

  setPanelWidth(_which, width, fair = false) {
    const clamped = this.#fitPanel(width, fair);
    if (clamped === null) return null;
    document.documentElement.style.setProperty("--w-qa", `${clamped}px`);
    savePref("qaWidth", clamped);
    const element = $("split-right");
    element.setAttribute("aria-valuemin", String(Math.min(320, clamped)));
    element.setAttribute("aria-valuemax", String(clamped));
    element.setAttribute("aria-valuenow", String(clamped));
    element.setAttribute("aria-valuetext", `${clamped} pixels`);
    return clamped;
  }

  #wireSplitters() {
    const DEFAULT = 520;
    const apply = (width) => this.setPanelWidth("right", width);
    const element = $("split-right");
    const pane = () => $("qa");
    this.setPanelWidth("right", pane().getBoundingClientRect().width);

    let startX = 0;
    let startWidth = 0;
    element.addEventListener("pointerdown", (event) => {
      const rect = pane().getBoundingClientRect();
      if (rect.width < 10) return;
      event.preventDefault();
      element.setPointerCapture(event.pointerId);
      element.classList.add("is-dragging");
      startX = event.clientX;
      startWidth = rect.width;
    });

    element.addEventListener("pointermove", (event) => {
      if (!element.hasPointerCapture(event.pointerId)) return;
      apply(startWidth + (startX - event.clientX));
    });

    const end = (event) => {
      element.classList.remove("is-dragging");
      try { element.releasePointerCapture(event.pointerId); } catch { }
      this.graph.fit();
    };
    element.addEventListener("pointerup", end);
    element.addEventListener("pointercancel", end);

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

    let pending = 0;
    window.addEventListener("resize", () => {
      if (pending) cancelAnimationFrame(pending);
      pending = requestAnimationFrame(() => {
        pending = 0;
        const before = this.#liveWidth();
        this.setPanelWidth("right", this.#liveWidth(), true);
        if (this.#liveWidth() !== before) this.graph.fit();
      });
    });
  }

  palette = {
    open() {
      $("palette-scrim").hidden = false;
      $("palette").hidden = false;
      $("palette-input").focus();
    },
    close() {
      $("palette-scrim").hidden = true;
      $("palette").hidden = true;
      $("palette-input").value = "";
    }
  };
}

function shortModel(model) {
  return model
    .replace(/^nvidia\//, "")
    .replace(/^meta-llama\//, "")
    .replace(/-instruct$/, "")
    .replace(/:.*$/, "");
}

function shortCompany(name) {
  return name
    .replace(/ Inc\.$/, "")
    .replace(/ Corporation$/, "")
    .replace(/ Corp\.$/, "")
    .replace(/ Ltd\.$/, "")
    .replace(/ Limited$/, "");
}

function highlightTerm(text, term) {
  if (!term) return escapeHtml(text);
  const parts = text.split(new RegExp(`(${escapeHtml(term)})`, "gi"));
  return parts.map((p, i) => i % 2 ? `<mark>${p}</mark>` : escapeHtml(p)).join("");
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

const app = new AnswerApp();
app.start();