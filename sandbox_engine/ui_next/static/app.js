/* Application wiring: the only module that knows the page.
 *
 * Everything below is glue between five collaborators that each own one thing —
 * `api.js` talks to the server, `graph.js` draws the canvas, `answer.js`
 * renders an answer, `reports.js` owns the drawer, `store.js` holds the state —
 * and this file decides what happens when the reader does something.
 */

import {
  askStream, fetchCompanies, fetchEntities, fetchGraph,
  fetchRoute, fetchStats, saveRagState,
} from "./api.js";
import { AnswerView } from "./answer.js";
import { GraphView } from "./graph.js";
import { Process } from "./process.js";
import { ReportsPanel } from "./reports.js";
import { set, setCollection, state, subscribe } from "./store.js";
import {
  $, announce, clear, debounce, el, fmtNumber, loadPref, prettyType,
  savePref, toast, typeColor,
} from "./util.js";

/* ── the stages a run is expected to cross ────────────────────────────────── */

/* The server sends its own plan before any work starts, and the strip uses
 * that. These are the fallbacks for the window between asking and the plan
 * landing, and for a probe that never came back: a known entity is already in
 * the graph, so it skips the cold start's fetch, extract and stitch. */
const PROCESS_PLANS = {
  KNOWN: ["routing", "traversal", "synthesis"],
  COLD_START: ["routing", "fetching", "extraction", "stitching", "traversal", "synthesis"],
};

const processPlan = (route) => PROCESS_PLANS[route] || PROCESS_PLANS.KNOWN;

/* How long the route probe may take before it is given up on. It resolves in
 * about ten milliseconds -- one local graph query, no model call -- so this is
 * generous by two orders of magnitude. It exists because the probe runs while
 * the page is already flagged as busy: an unbounded probe would leave the flag
 * set with no request behind it, and the next question would be refused as
 * "already running" when nothing was. */
const ROUTE_PROBE_MS = 1000;

class App {
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
    this.processOk = true;
    this.processNote = "";
    this.process = new Process($("pipeline"));
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

    /* The search box is collapsed on load, so there is nothing to fill. The
     * canvas is the thing a reader looks at first and it is fetched on its own
     * rather than behind a list nobody has asked for yet. */
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

  /* Hiding a type is a legend action: the legend is already a list of the types
   * in the graph with their counts, so clicking one toggles it. There used to be
   * a second row of facet pills in the explorer doing the same job, and the two
   * could disagree with each other. */
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

  /* ── entity search ────────────────────────────────────────────────────────
   *
   * The search is a box floating over the graph, so this is the only place an
   * entity is ever named, and choosing one answers the question the box was
   * asking: that entity's neighbourhood, centred in the canvas. The matches are
   * a transient dropdown, not a panel, so nothing stays over the graph once the
   * reader has what they came for. */

  async loadEntities() {
    const term = this.entityTerm || "";
    try {
      /* An empty term would fetch the whole entity table to fill a dropdown
       * nobody is reading, so the box asks for matches rather than for
       * everything. */
      const data = await fetchEntities(term, 60);
      set({ entities: data.entities || [] });
      this.#renderEntities();
    } catch (error) {
      toast(`entity search failed: ${error.message}`, "bad");
    }
  }

  /** Open the matches under the box. Called on focus and on every keystroke, so
   *  the dropdown is never behind the thing the reader is looking at. */
  openSearch() {
    $("entity-results").hidden = false;
    $("entity-search").setAttribute("aria-expanded", "true");
    this.#renderEntities();
  }

  closeSearch({ keepTerm = true } = {}) {
    $("entity-results").hidden = true;
    $("entity-search").setAttribute("aria-expanded", "false");
    if (!keepTerm) this.#clearFilter();
  }

  #renderEntities() {
    const host = clear($("entity-list"));
    const term = (this.entityTerm || "").toLowerCase();
    /* The server already filtered on `term`; this is a second, local pass so
     * the highlighting and the count reflect what is actually on screen, and so
     * a stale response cannot leave a row that does not match the box. */
    const rows = state.entities.filter((entity) => {
      if (!term) return true;
      return [entity.name, entity.entity_type, entity.description]
        .some((value) => String(value ?? "").toLowerCase().includes(term));
    });

    $("entity-clear").hidden = !term;
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

  /* ── graph ───────────────────────────────────────────────────────────── */

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

  /** An entity was picked from the search box. The dropdown has done its job,
   *  so it goes away and the graph is left to itself. */
  #chooseEntity(id) {
    this.closeSearch();
    this.focusEntity(id);
  }

  /** Follow a citation. The node is usually already on screen, so this centres
   *  on it rather than refetching — and says so if it is not. */
  focusCitation(id) {
    if (this.graph.focus(id)) {
      set({ selected: id });
      this.#renderEntities();
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

    let route = "KNOWN";
    let ticker = null;

    /* The try starts here, one line after `busy` is set, and that placement is
     * the whole fix. The setup between those two lines used to sit outside the
     * try, so a throw or a hang in any of it -- a missing element, a route
     * probe that never came back -- left `busy` true for good. The page then
     * refused every later question with "a question is already running" while
     * nothing was on the wire: the Ask button disabled, the label stuck on
     * "Asking.", the elapsed timer still climbing, the spinner still turning.
     * The one `finally` below is now the only way out, and it runs on every
     * ending -- answered, refused, errored, cancelled, or blown up in setup. */
    set({ busy: true, question, streaming: "" });
    try {
      savePref("lastQuestion", question);
      this.showTab("answer");
      this.#setAskLabel(true);
      $("ask-btn").disabled = true;
      this.#startTimer();

      /* Bounded, because this probe is what the flag waits on. It is one local
       * graph query, measured at ~10 ms, so a second of silence is a fault
       * rather than slowness -- and an unbounded one leaves the page claiming a
       * question is running with no request behind it. */
      const probeCtl = new AbortController();
      const probeTimer = setTimeout(() => probeCtl.abort(), ROUTE_PROBE_MS);
      try {
        const probe = await fetchRoute(question, { signal: probeCtl.signal });
        route = probe.route || "KNOWN";
        ticker = probe.ticker;
      } catch {
        // A failed or slow probe is not a failed question: fall through to the
        // route that always works rather than refusing to answer.
      } finally {
        clearTimeout(probeTimer);
      }

      this.answer.pending(question);
      this.process.begin({ plan: processPlan(route), ticker });

      /* Both routes stream now. The known path used to post to /api/ask and wait
       * on a silent socket, which is the wait people described as a hang -- the
       * retrieval is quick and then the model says nothing for a minute. Its
       * stream carries the same stages as the cold start's, so one timeline in
       * the client serves both and neither route is the one that goes blank. */
      await this.#askStreaming(question);
    } catch (error) {
      if (error.name === "AbortError") {
        this.answer.error("cancelled");
      } else {
        this.answer.error(`Query failed: ${error.message}`);
        toast(`query failed: ${error.message}`, "bad");
        announce("Query failed");
      }
    } finally {
      // Cleared here as well as by the request finishing, so a superseded or
      // abandoned controller cannot abort whatever runs next.
      this.abort = null;
      this.#stopTimer();
      set({ busy: false });
      // After the busy flag drops, so the strip is torn down on every ending:
      // answered, refused, errored or cancelled. Leaving a row of live clocks
      // above a finished answer reads as work still in progress.
      this.process.end({ ok: this.processOk, note: this.processNote });
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
        // The plan arrives before any work does, so the rail is the right shape
        // from the first frame instead of growing as stages fire.
        this.process.begin({ plan: data.stages, ticker: data.ticker });
        return;
      }
      if (data.step === "ambiguous") {
        this.processOk = false;
        this.process.end({ ok: false, note: "ambiguous" });
        this.processOk = true;
        return;
      }
      this.process.enter(data.step, data.message);
      if (data.message) this.#setTimerLabel(data.message);
      return;
    }
    if (type === "token") {
      // The known route's answer arrives as words the moment the model returns
      // it, so the first token means synthesis is over.
      if (state.streaming === "") this.process.enter("synthesis", "Composing the answer from the evidence");
      set({ streaming: state.streaming + (data.token ?? "") });
      this.answer.appendStream(state.streaming);
      return;
    }
    if (type === "error") {
      this.processOk = false;
      this.processNote = "failed";
      this.answer.error(data.error || "the server reported an error");
      return;
    }
    if (type !== "done") return;

    if (data.status === "error") {
      this.processOk = false;
      this.processNote = "failed";
      this.answer.error(data.error || "the server reported an error");
      return;
    }
    if (data.degraded) {
      this.processNote = "degraded";
    }
    /* The COLD_START stream carries the synthesis, the provenance ledger and the
     * graph, but no grading — that path has nothing to grade, because the
     * synthesis never went through the grader. */
    this.#finish({
      ...data,
      // Read from the store, not from a local: this handler is called by the SSE
      // reader, not by `ask()`, so nothing named `question` is in scope here and
      // naming it threw a ReferenceError on every completed run -- the answer
      // was already rendered by then, so the page showed the text and then
      // reported "Query failed: question is not defined". `ask()` puts it in
      // the store before the first byte is written, so it is always set by the
      // time a `done` frame can arrive.
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

    if (result.graph) {
      const cited = (result.used_tags || []).map((tag) => result.tag_map?.[tag]).filter(Boolean);
      setCollection("cited", new Set(cited));
      const counts = this.graph.setData(result.graph, { cited });
      $("graph-count").textContent = `${fmtNumber(counts.nodes)} nodes · ${fmtNumber(counts.links)} links`;
      this.#showCitationBadge();
    }

    announce(["answer", result.verdict?.toLowerCase() || result.route?.toLowerCase() || "returned",
      ...Object.entries(result.provenance_mix || {}).map(([key, value]) => `${key} ${value}`),
      ...(result.violations || [])].filter(Boolean).join(". "));
  }

  /* A caption for the glow. It names how much of the graph the answer leaned
   * on, because the question a reader has while staring at a graph with five
   * nodes lit out of ninety is "is that all it used?" — and the graph is
   * answering it with light alone. The counts come from the graph view's own
   * state, so the badge cannot drift from what is actually lit. */
  #showCitationBadge() {
    const badge = $("graph-cited");
    if (!badge) return;
    const { nodes, edges } = this.graph.citationSpread;
    badge.hidden = nodes === 0;
    /* The way out of a citation focus belongs next to the thing it undoes, and
     * it is the only reason the dropdown needs a footer control at all. */
    $("clear-focus").hidden = nodes === 0;
    if (!nodes) return;
    badge.textContent = `${nodes} cited · ${edges} link${edges === 1 ? "" : "s"}`;
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
    /* Read by #wireComposerSplitter, which also clamps it to the current window
     * — a height that fitted the panel last week can be taller than the panel
     * today, and restoring it unclamped would hide the answer it was meant to
     * reveal. */
    const composerHeight = loadPref("composerHeight", null);
    if (composerHeight) root.style.setProperty("--h-composer", `${composerHeight}px`);

    $("hops").value = String(state.hops);
    $("graph-limit").value = String(state.graphLimit);
    $("g-labels").setAttribute("aria-pressed", state.showLabels ? "true" : "false");
    $("g-legend-toggle").setAttribute("aria-pressed", state.legendOpen ? "true" : "false");
    $("legend").hidden = !state.legendOpen;
    $("qa-input").value = state.lastQuestion || "";
    this.#autogrow($("qa-input"));
    /* "explore" was a view made of the entity panel beside the graph. That panel
     * is gone, so a stored preference naming it would restore a view that has no
     * panels to show and land the reader on an empty workspace. */
    this.setView(state.view === "ask" ? "ask" : "graph");
    this.graph.setLabels(state.showLabels);
  }

  #wireChrome() {
    /* One search box, over the graph it searches. It is the only place an entity
     * is named now, so there is no second input to keep in step with it. */
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
    $("entity-clear").addEventListener("click", () => {
      this.#clearFilter();
      input.focus();
    });

    /* Arrow keys walk the matches from inside the box, so the dropdown is
     * reachable without a pointer. Enter takes the highlighted one, or the first
     * when nothing is highlighted, which is what a reader who typed three letters
     * and pressed Return meant. Escape puts the box away and gives the graph
     * back. */
    input.addEventListener("keydown", (event) => {
      const rows = [...$("entity-list").querySelectorAll(".entity")];
      const at = rows.indexOf(document.activeElement);

      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        if (!rows.length) return;
        event.preventDefault();
        const step = event.key === "ArrowDown" ? 1 : -1;
        const next = Math.max(0, Math.min(rows.length - 1, (at === -1 ? -1 : at) + step));
        rows[next]?.focus();
      } else if (event.key === "Enter" && rows.length) {
        event.preventDefault();
        (at === -1 ? rows[0] : rows[at]).click();
      } else if (event.key === "Escape") {
        event.preventDefault();
        this.closeSearch();
        input.blur();
      }
    });

    /* A click anywhere else closes the dropdown. Without this the matches stay
     * over the graph after the reader has moved on to reading it. */
    document.addEventListener("pointerdown", (event) => {
      if ($("entity-results").hidden) return;
      if (!$("graph-search").contains(event.target)) this.closeSearch();
    });

    $("show-all").addEventListener("click", () => {
      this.closeSearch();
      this.showWholeGraph();
    });
    $("clear-focus").addEventListener("click", () => {
      setCollection("cited", new Set());
      this.graph.setCited([]);
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
        this.#showCitationBadge();
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

    /* Global shortcut: "/" focuses the entity search (when not typing in an input) */
    document.addEventListener("keydown", (event) => {
      if (event.key === "/" && event.target.tagName !== "INPUT" && event.target.tagName !== "TEXTAREA" && !event.metaKey && !event.ctrlKey) {
        event.preventDefault();
        this.setView("graph");
        input.focus();
        input.select();
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

  /* ── splitters ───────────────────────────────────────────────────────── */

  /** Nudge the answer panel's width, in pixels. The splitter is a 1px target on
   *  a wide screen, so the command palette offers the same resize to anyone who
   *  would rather not go looking for the handle. */
  resizePanel(delta) {
    this.setPanelWidth("right", $("qa").getBoundingClientRect().width + delta);
    this.graph.fit();
  }

  /** The width the graph must keep, or 0 when it has the window to itself.
   *
   *  Below 860px the panels are stacked tabs, so the canvas has the row. Above
   *  that it shares the row with the answer panel, and a narrower floor is enough
   *  to keep a node and its label legible. */
  #graphFloor() {
    const width = window.innerWidth;
    if (width < 860) return 0;
    if (width < 1180) return 320;
    return 360;
  }

  /** The answer panel's current width, or 0 when it is out of flow. A stacked tab
   *  is not in the row, so it takes no width from the canvas. */
  #liveWidth() {
    if ($("split-right").offsetParent === null) return 0;
    return $("qa").getBoundingClientRect().width;
  }

  /** The width the live splitter occupies in the grid row. It is a 1px column
   *  and it comes out of the same row as the graph, so leaving it out of the
   *  budget is what put the canvas a couple of pixels under its floor. */
  #splitGutter() {
    return $("split-right").offsetParent === null ? 0 : 1;
  }

  /** Resolve the answer panel's width against the shared budget, or null when it
   *  is out of flow.
   *
   *  The canvas keeps a floor and the panel takes what is left, capped so it
   *  cannot swallow the page. `fair` decides how a narrowing window is applied: a
   *  panel being dragged is not scaled, because a layout that changes the width
   *  you are dragging is a layout you cannot drag, while a window narrowing has
   *  no owner and simply gives the canvas its floor back. */
  #fitPanel(wanted, fair = false) {
    if ($("split-right").offsetParent === null) return null;

    const budget = Math.max(0, window.innerWidth - this.#graphFloor() - this.#splitGutter());
    const cap = 960;
    const prefMin = 320;
    let mine = Math.max(0, Math.min(cap, Math.round(wanted)));

    if (fair) {
      if (mine > budget) mine = budget;
    } else {
      // A preferred minimum that does not fit is not a minimum. The canvas floor
      // is the one size this layout does not go below.
      mine = Math.max(Math.min(prefMin, budget), Math.min(cap, budget, mine));
    }

    return mine;
  }

  /** The one place the panel width is set: clamped to the shared budget, written
   *  to the grid variable, remembered, and reflected in the splitter's ARIA
   *  values so a screen reader announces the new size. */
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
    /* The same number the stylesheet starts from, so a double-click, a Home key
     * and a cleared preference all land in the same place. */
    const DEFAULT = 520;
    const apply = (width) => this.setPanelWidth("right", width);

    const element = $("split-right");
    const pane = () => $("qa");
    // Announce the width the pane actually has, which is the restored
    // preference on a reload rather than the stylesheet default.
    this.setPanelWidth("right", pane().getBoundingClientRect().width);

    let startX = 0;
    let startWidth = 0;
    element.addEventListener("pointerdown", (event) => {
      const rect = pane().getBoundingClientRect();
      // The grid owns the width, so the drag has to start from what is on
      // screen rather than from the stylesheet default.
      if (rect.width < 10) return;
      event.preventDefault();
      element.setPointerCapture(event.pointerId);
      element.classList.add("is-dragging");
      startX = event.clientX;
      startWidth = rect.width;
    });

    element.addEventListener("pointermove", (event) => {
      if (!element.hasPointerCapture(event.pointerId)) return;
      // The panel is to the right of the handle, so dragging left widens it.
      apply(startWidth + (startX - event.clientX));
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

    this.#wireComposerSplitter();

    /* Re-derive the width when the window narrows. The graph's own
     * ResizeObserver re-fits the zoom, but nothing was re-running the clamp: a
     * layout arranged on a 2560px monitor kept its 520 when the window was
     * dragged down to 1400 and the canvas was left with whatever was left over.
     * Only the shrinking direction is applied -- widening the window back
     * restores the width the user chose instead of preserving the squeeze they
     * had to make to fit.
     *
     * The panel is measured before and after so a resize that changed no width
     * does not trigger a re-fit, which would fight the user's own zoom. */
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


  /* The horizontal divider between the question box and the answer.
   *
   * Kept apart from the two panel splitters because everything about it differs:
   * the axis is vertical, and it does not resize a panel the window can spare —
   * it trades height *within* one. That trade is the whole point of it. A long
   * answer is the common case on this page, and a reader who has to scroll the
   * graph and the answer both to check a figure has been handed a layout
   * problem the divider exists to solve.
   */
  #wireComposerSplitter() {
    const element = $("split-composer");
    const composer = $("composer");

    /* The floor is the label plus the composer box — a height below that and
     * the question being edited is no longer fully visible, which is worse than
     * a cramped answer. The ceiling leaves the answer at least a quarter of the
     * panel, because a divider that can push the answer off the panel is a
     * divider that can hide the answer entirely. */
    const LOW = 124;
    const limits = () => {
      const panel = $("qa").getBoundingClientRect().height || 600;
      return [LOW, Math.max(LOW, Math.round(panel * 0.74))];
    };

    /* `null` means "no height of its own" — the stylesheet's `auto`, which is
     * what the box wants until something inside it needs more room than its
     * content asked for. Resetting to null is therefore the reset, not a number.
     * The ARIA values always report the height on screen, which after a reset is
     * the box's own content height rather than any remembered number.
     *
     * `persist` is off during a drag: localStorage writes are synchronous, and
     * one per pointermove is a stall on every frame of the drag. The value is
     * written once, on release. */
    const apply = (height, { persist = true } = {}) => {
      const [low, high] = limits();
      const clamped = height === null ? null : Math.max(low, Math.min(high, Math.round(height)));
      if (clamped === null) {
        document.documentElement.style.removeProperty("--h-composer");
        if (persist) savePref("composerHeight", null);
      } else {
        document.documentElement.style.setProperty("--h-composer", `${clamped}px`);
        if (persist) savePref("composerHeight", clamped);
      }
      const shown = Math.round(composer.getBoundingClientRect().height);
      element.setAttribute("aria-valuemin", String(low));
      element.setAttribute("aria-valuemax", String(high));
      element.setAttribute("aria-valuenow", String(shown));
      element.setAttribute("aria-valuetext", `${shown} pixels tall`);
      return { height: clamped, shown };
    };
    /* Kept on the instance so the command palette can make the same trade
     * through the same code, instead of growing a second copy of the clamp. */
    this.setComposerHeight = apply;

    /* Report where the divider actually is before anyone touches it. The saved
     * preference is applied first by #restorePreferences, so this measures the
     * restored box rather than the stylesheet default. */
    apply(loadPref("composerHeight", null));

    let startY = 0;
    let startHeight = 0;

    element.addEventListener("pointerdown", (event) => {
      const rect = composer.getBoundingClientRect();
      if (rect.height < 10) return;
      event.preventDefault();
      element.setPointerCapture(event.pointerId);
      element.classList.add("is-dragging");
      startY = event.clientY;
      startHeight = rect.height;
    });

    element.addEventListener("pointermove", (event) => {
      if (!element.hasPointerCapture(event.pointerId)) return;
      /* The box is the space *above* the handle, and its top edge is nailed to
       * the panel, so the box is exactly as tall as the handle's own travel.
       * Adding the displacement is what makes the handle track the pointer
       * one-to-one: pull up, the box shortens, the answer grows. Subtracting it
       * here would slide the boundary the opposite way from the cursor. */
      apply(startHeight + (event.clientY - startY), { persist: false });
    });

    const release = (event) => {
      try { element.releasePointerCapture(event.pointerId); } catch { /* already released */ }
    };
    element.addEventListener("pointerup", (event) => {
      release(event);
      if (!element.classList.contains("is-dragging")) return;
      element.classList.remove("is-dragging");
      // Written here rather than on every frame of the drag, so the position
      // survives a reload but the drag itself stays smooth.
      apply(composer.getBoundingClientRect().height);
    });
    /* A cancelled drag is a drag the browser took over — a touch scroll, a
     * system gesture. It is put back where it started rather than kept at
     * wherever the pointer happened to be when it was interrupted. */
    element.addEventListener("pointercancel", (event) => {
      release(event);
      if (!element.classList.contains("is-dragging")) return;
      element.classList.remove("is-dragging");
      apply(startHeight);
    });

    /* Keyboard, for the same reason the panel splitters have it: a 1px target is
     * a mouse-only affordance otherwise. Up and Down move the divider, and Home
     * hands the height back to the box. */
    element.addEventListener("keydown", (event) => {
      const step = event.shiftKey ? 64 : 16;
      const current = composer.getBoundingClientRect().height;
      if (event.key === "Home" || event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        apply(null);
        return;
      }
      if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
      event.preventDefault();
      apply(current + (event.key === "ArrowUp" ? -step : step));
    });

    element.addEventListener("dblclick", () => apply(null));
  }

  /** The same trade the divider makes, reachable without hunting for a 1px
   *  handle. `delta` is pixels of *answer* gained, so the sign reads as the
   *  reader's intent rather than as the box's. */
  resizeComposer(delta) {
    const current = $("composer").getBoundingClientRect().height;
    const step = Math.abs(delta) >= 64 ? 64 : 16;
    return this.setComposerHeight(current - Math.sign(delta) * step);
  }

  /** Hand the height back to the question box. */
  resetComposer() {
    return this.setComposerHeight(null);
  }

  /* ── keyboard ────────────────────────────────────────────────────────── */

  #wireKeyboard() {
    document.addEventListener("keydown", (event) => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "");
      const mod = event.metaKey || event.ctrlKey;

      if (event.key === "Escape") {
        if (this.palette.isOpen) { this.palette.close(); return; }
        if (this.reports.isOpen) { this.reports.close(); return; }
        if (!$("entity-results").hidden) { this.closeSearch(); return; }
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
        $("entity-search").focus();
        $("entity-search").select();
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
      { label: "Reset the answer panel width", group: "view", keys: "", run: () => { app.setPanelWidth("right", 520); app.graph.fit(); } },
      { label: "Search the graph for an entity", group: "graph", keys: "/", run: () => { app.setView("graph"); $("entity-search").focus(); $("entity-search").select(); } },
      { label: "Give the answer more room", group: "view", keys: "", run: () => app.resizeComposer(16) },
      { label: "Give the question box more room", group: "view", keys: "", run: () => app.resizeComposer(-16) },
      { label: "Reset the question box height", group: "view", keys: "", run: () => app.resetComposer() },
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
