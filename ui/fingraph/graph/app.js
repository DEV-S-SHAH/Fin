/* FinGraph Knowledge Graph Explorer — full-screen graph exploration.
 *
 * Reuses the same collaborators as the Studio: api.js, graph.js, store.js, util.js
 * but without the Q&A panel — pure graph exploration experience.
 */

import {
  fetchCompanies, fetchEntities, fetchGraph,
  fetchStats,
} from "/static/api.js";
import { GraphView } from "/static/graph.js";

import { set, setCollection, state, subscribe } from "/static/store.js";
import {
  $, announce, clear, debounce, el, fmtNumber, loadPref, prettyType,
  savePref, toast, typeColor, stampLogos,
} from "/static/util.js";

class GraphExplorerApp {
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
  }

  async start() {
    stampLogos();
    this.#restorePreferences();
    this.#wireChrome();
    this.#wireStore();
    this.#renderSamples();

    this.#renderGraphState();

    const [stats, companies] = await Promise.allSettled([fetchStats(), fetchCompanies()]);
    if (stats.status === "fulfilled") this.#applyStats(stats.value);
    else this.#statsFailed(stats.reason);
    if (companies.status === "fulfilled") {
      set({ companies: companies.value.companies || [] });
      this.#renderIssuers();
    }

    await this.showWholeGraph();
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

  #restorePreferences() {
    const root = document.documentElement;

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

    document.addEventListener("keydown", (event) => {
      if (event.key === "/" && event.target.tagName !== "INPUT" && event.target.tagName !== "TEXTAREA" && !event.metaKey && !event.ctrlKey) {
        event.preventDefault();
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

  #renderSamples() {
    // No samples in graph explorer
  }
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

const app = new GraphExplorerApp();
app.start();