/* FinGraph Multi-Issuer Compare — side-by-side company comparison.
 *
 * Reuses the same collaborators: api.js, graph.js, store.js, util.js
 * Fetches data for multiple companies and renders comparison tables.
 */

import {
  fetchCompanies, fetchEntities, fetchGraph,
  fetchStats, askJson,
} from "/static/api.js";
import { GraphView } from "/static/graph.js";

import { set, setCollection, state, subscribe } from "/static/store.js";
import {
  $, announce, clear, debounce, el, fmtNumber, loadPref, prettyType,
  savePref, toast, typeColor, stampLogos,
} from "/static/util.js";

// Default comparison tickers
const DEFAULT_TICKERS = ["AAPL", "MSFT", "NVDA"];

class CompareApp {
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
    this.entityTerm = "";
    this.selectedTickers = [...DEFAULT_TICKERS];
    this.companyData = {};
    this.comparisonAnswer = null;
  }

  async start() {
    stampLogos();
    this.#restorePreferences();
    this.#wireChrome();
    this.#wireStore();

    this.#renderGraphState();

    const [stats, companies] = await Promise.allSettled([fetchStats(), fetchCompanies()]);
    if (stats.status === "fulfilled") this.#applyStats(stats.value);
    else this.#statsFailed(stats.reason);
    if (companies.status === "fulfilled") {
      set({ companies: companies.value.companies || [] });
      this.#renderIssuers();
      this.#renderSelector(companies.value.companies || []);
    }

    await this.showWholeGraph();
    await this.#runComparison();
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
    } catch (error) {
      toast(`graph query failed: ${error.message}`, "bad");
    } finally {
      set({ graphLoading: false });
    }
  }

  async focusEntity(id) {
    set({ selected: id });
    this.#renderEntities();
    try {
      const payload = await fetchGraph({ seed: id, hops: state.hops, limit: Number(state.graphLimit) });
      set({ graph: payload });
      const counts = this.graph.setData(payload, { fresh: true });
      $("graph-count").textContent = `${fmtNumber(counts.nodes)} nodes · ${fmtNumber(counts.links)} links`;
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

  #renderSelector(companies) {
    const host = clear($("compare-selectors"));
    const tickers = companies.map(c => c.ticker).sort();
    const defaultTickers = DEFAULT_TICKERS.filter(t => tickers.includes(t));

    for (let i = 0; i < 3; i++) {
      const select = el("select", {
        class: "compare-select",
        "data-slot": String(i),
        "aria-label": `Company ${i + 1}`,
      }, [
        el("option", { value: "", text: `Select company ${i + 1}` }),
        ...tickers.map(t => el("option", { value: t, text: t, selected: defaultTickers[i] === t })),
      ]);
      select.addEventListener("change", () => this.#onSelectorChange());
      host.append(select);
    }
  }

  #onSelectorChange() {
    const selects = $$(".compare-select");
    this.selectedTickers = Array.from(selects).map(s => s.value).filter(Boolean);
    // Update table headers
    this.#updateTableHeaders();
  }

  #updateTableHeaders() {
    const tickers = this.selectedTickers;
    const metricHeaders = ["AAPL", "MSFT", "NVDA"]; // default
    const segmentHeaders = ["AAPL", "MSFT", "NVDA"];
    
    // Update metrics table headers
    for (let i = 0; i < 3; i++) {
      const col = $(`#metrics-col-${["aapl", "msft", "nvda"][i]}`);
      if (col) col.textContent = tickers[i] || `Company ${i + 1}`;
    }
    // Update segments table headers
    for (let i = 0; i < 3; i++) {
      const col = $(`#segments-col-${["aapl", "msft", "nvda"][i]}`);
      if (col) col.textContent = tickers[i] || `Company ${i + 1}`;
    }
  }

  async #runComparison() {
    if (this.selectedTickers.length === 0) return;

    // Use GraphRAG to get comparison answer
    const question = `Compare ${this.selectedTickers.join(" vs ")} across revenue, profit margins, and key metrics`;
    try {
      const result = await askJson({ question });
      this.comparisonAnswer = result;
      
      // Also fetch graph data for the selected companies
      await this.#updateGraphForComparison();
      
      // Render comparison from the answer
      this.#renderComparisonFromAnswer(result);
    } catch (error) {
      toast(`comparison failed: ${error.message}`, "bad");
    }
  }

  #renderComparisonFromAnswer(result) {
    // Show the answer in the metrics tab
    const body = clear($("#metrics-body"));
    const tickers = this.selectedTickers;
    
    // Create a row with the full answer
    const row = document.createElement("tr");
    const cells = [
      el("td", { class: "compare-table__metric", text: "GraphRAG Comparison" }),
      el("td", { class: "compare-table__value", text: result.text || result.answer || "Loading...", colspan: 3, style: "text-align:left; font-size: 0.9rem; line-height: 1.5;" })
    ];
    row.append(...cells);
    body.append(row);
    
    // Also show provenance/grading info
    if (result.verdict) {
      const verdictRow = document.createElement("tr");
      verdictRow.innerHTML = `<td class="compare-table__metric">Verdict</td><td class="compare-table__value" colspan="3">${result.verdict}</td>`;
      body.append(verdictRow);
    }
    
    if (result.provenance_mix) {
      const mixRow = document.createElement("tr");
      const mixText = Object.entries(result.provenance_mix).map(([k, v]) => `${k}: ${v}`).join(", ");
      mixRow.innerHTML = `<td class="compare-table__metric">Provenance Mix</td><td class="compare-table__value" colspan="3">${mixText}</td>`;
      body.append(mixRow);
    }
    
    // Show citations
    if (result.used_tags && result.used_tags.length > 0) {
      const citeRow = document.createElement("tr");
      citeRow.innerHTML = `<td class="compare-table__metric">Citations</td><td class="compare-table__value" colspan="3">${result.used_tags.join(", ")}</td>`;
      body.append(citeRow);
    }
    
    // Show graph metrics
    if (result.graph_metrics) {
      const graphRow = document.createElement("tr");
      graphRow.innerHTML = `<td class="compare-table__metric">Graph Retrieved</td><td class="compare-table__value" colspan="3">${result.graph_metrics.nodesRetrieved} nodes, ${result.graph_metrics.edgesTraversed} edges</td>`;
      body.append(graphRow);
    }
  }

  #renderMetricsTable() {
    // Now handled by #renderComparisonFromAnswer
  }
      
      for (const ticker of tickers) {
        const data = this.companyData[ticker]?.fundamentals || {};
        const value = data[metric.key];
        cells.push(el("td", { class: "compare-table__value", text: metric.fmt(value) }));
      }
      
      // Fill empty columns if fewer than 3 tickers
      while (cells.length < 4) {
        cells.push(el("td", { class: "compare-table__value", text: "—" }));
      }
      
      row.append(...cells);
      body.append(row);
    }
  }

  #renderSegmentsTable() {
    const body = clear($("#segments-body"));
    const tickers = this.selectedTickers;

    // Collect all unique segments across companies
    const allSegments = new Set();
    for (const ticker of tickers) {
      const data = this.companyData[ticker]?.fundamentals || {};
      // We don't have segment breakdown in the current API, so show placeholder
    }

    // For now, show a placeholder message
    const row = document.createElement("tr");
    row.innerHTML = '<td colspan="4" style="text-align:center;color:var(--color-text-muted);padding:32px;">Segment breakdown requires additional API data. Showing key segment metrics from filings.</td>';
    body.append(row);
  }

  #renderFilingsComparison() {
    const host = clear($("#filings-comparison"));
    const tickers = this.selectedTickers;

    for (const ticker of tickers) {
      const data = this.companyData[ticker]?.graph_info || {};
      const filings = data.filings || [];
      
      const section = el("div", { class: "filings-company-section" }, [
        el("h3", { class: "filings-company-title", text: `${ticker} Recent Filings` }),
        filings.length > 0 ? el("table", { class: "filings-mini-table" }, [
          el("thead", {}, el("tr", {}, [
            el("th", { text: "Form" }),
            el("th", { text: "Fiscal Year" }),
            el("th", { text: "Period" }),
            el("th", { text: "Period End" }),
          ])),
          el("tbody", {}, ...filings.slice(0, 5).map(f => el("tr", {}, [
            el("td", { class: "form-type", text: f.form }),
            el("td", { text: f.fiscal_year }),
            el("td", { text: f.period === 'FY' ? 'Full Year' : 'Q' + f.period }),
            el("td", { text: formatDate(f.period_end) }),
          ]))),
        ]) : el("p", { class: "filings-empty", text: "No filing data in graph" }),
      ]);
      host.append(section);
    }
  }

  async #updateGraphForComparison() {
    // Focus the graph on the selected companies
    if (this.selectedTickers.length > 0) {
      try {
        // Fetch graph with multiple seeds
        const payload = await fetchGraph({ 
          seed: this.selectedTickers[0], 
          hops: state.hops, 
          limit: Number(state.graphLimit) 
        });
        set({ graph: payload });
        const counts = this.graph.setData(payload, { fresh: true });
        $("graph-count").textContent = `${fmtNumber(counts.nodes)} nodes · ${fmtNumber(counts.links)} links`;
      } catch (error) {
        console.warn("Could not update graph for comparison:", error);
      }
    }
  }

  #restorePreferences() {
    $("hops").value = String(state.hops);
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
    $("g-labels")?.addEventListener("click", (event) => {
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
      }
    });

    $("theme-btn").addEventListener("click", () => this.toggleTheme());

    // Compare run button
    $("compare-run")?.addEventListener("click", () => this.#runComparison());

    // Tab switching
    for (const tab of document.querySelectorAll(".tab--compare[data-tab]")) {
      tab.addEventListener("click", () => this.#showCompareTab(tab.dataset.tab));
    }

    document.addEventListener("keydown", (event) => {
      if (event.key === "/" && event.target.tagName !== "INPUT" && event.target.tagName !== "TEXTAREA" && !event.metaKey && !event.ctrlKey) {
        event.preventDefault();
        input.focus();
        this.openSearch();
      }
    });
  }

  #showCompareTab(name) {
    for (const tab of document.querySelectorAll(".tab--compare[data-tab]")) {
      const on = tab.dataset.tab === name;
      tab.setAttribute("aria-selected", on ? "true" : "false");
    }
    for (const panel of document.querySelectorAll(".tabpanel--compare")) {
      panel.hidden = panel.id !== `tab-${name}`;
    }
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
}

function highlightTerm(text, term) {
  if (!term) return escapeHtml(text);
  const parts = text.split(new RegExp(`(${escapeHtml(term)})`, "gi"));
  return parts.map((p, i) => i % 2 ? `<mark>${p}</mark>` : escapeHtml(p)).join("");
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&")
    .replace(/</g, "<")
    .replace(/>/g, ">")
    .replace(/"/g, """)
    .replace(/'/g, "&#039;");
}

function formatCurrency(num) {
  if (num === null || num === undefined || isNaN(num)) return "—";
  return new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 0, maximumFractionDigits: 0 }).format(num);
}

function formatPct(num) {
  if (num === null || num === undefined || isNaN(num)) return "—";
  const sign = num >= 0 ? "+" : "";
  return `${sign}${num.toFixed(2)}%`;
}

function formatDate(timestamp) {
  if (!timestamp) return "—";
  const date = timestamp > 1e10 ? new Date(timestamp) : new Date(timestamp * 1000);
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

const $$ = (sel, ctx = document) => [...ctx.querySelectorAll(sel)];

const app = new CompareApp();
app.start();