/* The graph canvas.
 *
 * A plain 2D force-directed knowledge graph. Deliberately unremarkable:
 * two-dimensional positions, one solid circle per node, one hairline per
 * relationship, and text labels placed by a small search that tries not to
 * put two of them on top of each other.
 *
 * What the layout does:
 * - Each company is a cluster. Its members are seeded on a small disc around
 *   a slot on a grid, and a soft radial force keeps them there, so a company
 *   reads as one blob rather than being smeared across the canvas.
 * - The grid slots are pushed apart by a collision force sized to the cluster,
 *   which is what stops two companies from fusing into a single tangle.
 * - Within a cluster, a link force holds related nodes at a short, readable
 *   distance and a collision force keeps circles from overlapping. The result
 *   packs tightly without the "hairball" that pure repulsion tends toward.
 *
 * The simulation runs until it settles and then stops. Nothing loops forever:
 * once alpha decays, the render loop shuts itself down.
 *
 * Answer state is a render-time filter, not a data mutation. `allNodes` and
 * `allLinks` keep every entity the server sent; `#filterAndApply` narrows the
 * rendered set to the answer subgraph so unrelated nodes and edges are simply
 * not drawn. Clearing the answer brings the full graph back.
 */

import { svgEl, typeColor, prettyType, escapeHtml, $ } from "./util.js";

export const FLASH_MS = 1500;
export const FLOW_MS = 2000;

const EDGE_KEY = (l) => `${l.source?.id || l.source}|${l.target?.id || l.target}|${l.relation || ""}`;
const BEND = 0.06;

// How many degree-1 leaf labels get drawn when nothing is hovered. A filing
// reports enough metrics that labelling every one of them is the fastest way to
// destroy an otherwise clean layout, so the tail competes for a fixed budget.
// Hover or select a node and its label bypasses the cap entirely.
const SMART_LABEL_CAP = 18;
const ANSWER_LABEL_CAP = 14;

/**
 * How many force ticks may run before the first paint.
 *
 * The layout is settled off-screen rather than animated in, so this only needs
 * to be large enough to reach rest. The cap is a guard against a pathological
 * graph turning page load into a long block; a few hundred ticks of a force
 * layout is milliseconds, not seconds.
 */
const SETTLE_TICKS = 400;

/** Clear space kept between two node circles when one is dragged past another. */
const DRAG_CLEARANCE = 6;

/* Distinct accent per company. Ordered so neighbouring slots land far apart on
 * the wheel, and deliberately not "one warm ramp for everything" — company
 * identity has to survive at a glance. */
const COMPANY_PALETTE = [
  "#FF5A1F", // orange-red (brand)
  "#3B9DFF", // azure
  "#16C79A", // mint
  "#B07CFF", // violet
  "#F2B705", // gold
  "#FF6FA5", // pink
  "#35C4D6", // cyan
  "#7C8CFF", // periwinkle
  "#8DBE2E", // olive
  "#F0563D", // coral
  "#00A3A3", // deep teal
  "#D96BA0", // rose
  "#C77DFF", // orchid
  "#6FBF73", // green
];

/* Entities take a related shade of their company's accent rather than the
 * accent itself, so the company colour reads as a family. Nodes with no
 * company stay neutral. */
const ENTITY_TINT = "#9AA3B2";
const NEUTRAL = "#9AA3B2";

function hexToRgb(hex) {
  const h = String(hex).replace("#", "");
  const full = h.length === 3 ? h.split("").map((c) => c + c).join("") : h;
  const n = parseInt(full.slice(0, 6), 16);
  return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
}

function rgbToCss({ r, g, b }) {
  return `rgb(${Math.round(r)},${Math.round(g)},${Math.round(b)})`;
}

function mix(hex, toward, amount) {
  const a = hexToRgb(hex);
  const b = hexToRgb(toward);
  return rgbToCss({
    r: a.r + (b.r - a.r) * amount,
    g: a.g + (b.g - a.g) * amount,
    b: a.b + (b.b - a.b) * amount,
  });
}

function getNodeBaseRadius(type) {
  const t = String(type || "").toLowerCase();
  if (t === "company") return 18;
  if (t === "filing" || t === "document") return 10;
  if (t === "segment") return 8.5;
  if (t === "financialmetric") return 6.5;
  if (t === "fiscalyear" || t === "fiscalquarter") return 7.5;
  if (t === "event" || t === "disclosureevent") return 7;
  return 7;
}

/* Short, readable relationship distances. Kept in one place so the same
 * relation reads the same length everywhere in the graph. */
function linkDistance(relation) {
  switch (relation) {
    case "SUBMITTED":
    case "FILED":
      return 78;
    case "REPORTS_METRIC":
    case "CONTAINS_CHUNK":
    case "DISCLOSES_EVENT":
      return 58;
    case "DISAGGREGATED_BY":
      return 48;
    default:
      return 64;
  }
}

/* Label geometry, shared by the layout and the label placer so the spacing a
 * node is given and the space its caption is assumed to need cannot drift
 * apart. */
const LABEL_CHAR_W = 6.1;
const LABEL_PAD = 10;

function labelText(name) {
  return name.length > 30 ? `${name.slice(0, 29)}…` : name;
}

function estimateLabelWidth(nodes) {
  let widest = 0;
  for (const n of nodes) {
    widest = Math.max(widest, labelText(n.name).length * LABEL_CHAR_W + LABEL_PAD);
  }
  return widest;
}

/* Deterministic hash → [0,1). Used to seed the initial scatter so a reload
 * lays the graph out the same way instead of jumping to a new arrangement. */
function hashUnit(key) {
  let h = 2166136261;
  const s = String(key);
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return ((h >>> 8) & 0xffff) / 0xffff;
}

export class GraphView {
  /**
   * @param {SVGSVGElement} canvas
   * @param {{
   *   onSelect?: (node: any) => void,
   *   tooltip?: HTMLElement,
   *   tooltipHost?: HTMLElement,
   *   inspector?: HTMLElement,
   *   sidePanel?: HTMLElement
   * }} handlers
   */
  constructor(canvas, handlers = {}) {
    this.canvas = canvas;
    this.handlers = handlers;

    // Master graph data — never mutated by filtering or by answer state.
    this.allNodes = [];
    this.allLinks = [];
    this.byId = new Map();
    this.companyClusters = new Map(); // ticker -> { color, radius, cx, cy, nodes }

    // Rendered subset of the above.
    this.nodes = [];
    this.links = [];

    // Interaction & highlighting state.
    this.seeds = new Set();
    this.cited = new Set();
    this.fresh = new Set();
    this.selected = null;
    this.hovered = null;
    this.searchPathNodes = null;
    this.searchPathEdges = null;
    this.searchPathTargetId = null;
    this.answerPathNodes = null;
    this.answerPathEdges = null;
    this.answerCompanyIds = new Set();
    this.hopCache = new Map();
    this.maxHops = 3;

    // Filters & display options.
    this.companyFilter = "ALL";
    this.hiddenTypes = new Set();
    this.hiddenRelations = new Set();
    this.nodeScale = 1.0;
    this.linkScale = 1.0;
    this.labelsMode = "smart"; // "smart" | "all" | "none"
    this.showArrows = true;

    // Viewport & simulation.
    this.view = { x: 0, y: 0, k: 1 };
    this.frame = null;
    this.nodeDragging = false;
    this.suppressClick = false;
    this.dragTravel = 0;
    this.dragOrigin = null;
    this.flashTimer = 0;
    this.flowTimer = 0;
    this.flowLinks = null;
    this.fitted = false;
    this.disposed = false;

    this.#buildSkeleton();
    this.#buildBehaviours();
    this.#buildUIOverlays();

    this.observer = new ResizeObserver(() => this.#onResize());
    this.observer.observe(canvas);
  }

  /* ── DOM Skeleton ──────────────────────────────────────────────────────── */

  #buildSkeleton() {
    this.root = svgEl("g", { class: "g-root" });
    this.layerCluster = d3.select(svgEl("g", { class: "g-clusters" }));
    this.layerEdgeGlow = d3.select(svgEl("g", { class: "g-edge-glows" }));
    this.layerEdges = d3.select(svgEl("g", { class: "g-edges" }));
    this.layerEdgeFlow = d3.select(svgEl("g", { class: "g-edge-flows" }));
    this.layerEdgeLabels = d3.select(svgEl("g", { class: "g-edge-labels" }));
    this.layerNodes = d3.select(svgEl("g", { class: "g-nodes" }));

    this.root.append(
      this.layerCluster.node(),
      this.layerEdgeGlow.node(),
      this.layerEdges.node(),
      this.layerEdgeFlow.node(),
      this.layerEdgeLabels.node(),
      this.layerNodes.node(),
    );
    this.canvas.append(this.root);

    const defs = svgEl("defs");
    const markers = [
      { id: "arrow", color: "var(--border-strong, #3a3f4d)", opacity: 0.55 },
      { id: "arrow-hot", color: "currentColor", opacity: 0.95 },
    ];

    for (const m of markers) {
      const marker = svgEl("marker", {
        id: m.id,
        viewBox: "0 0 10 10",
        refX: 8,
        refY: 5,
        markerWidth: 4.5,
        markerHeight: 4.5,
        orient: "auto-start-reverse",
        markerUnits: "userSpaceOnUse",
      });
      marker.append(svgEl("path", {
        d: "M 0 1.5 L 9 5 L 0 8.5 z",
        fill: m.color,
        opacity: m.opacity,
      }));
      defs.append(marker);
    }

    this.canvas.insertBefore(defs, this.root);
  }

  /* ── Interactive Behaviours ─────────────────────────────────────────────── */

  #buildBehaviours() {
    this.zoom = d3.zoom()
      .scaleExtent([0.08, 4])
      .filter((event) => !this.nodeDragging && !event.button)
      .on("start", () => this.canvas.classList.add("is-panning"))
      .on("zoom", (event) => {
        this.view = { x: event.transform.x, y: event.transform.y, k: event.transform.k };
        this.#applyView();
        this.#computeLabelPlacements();
      })
      .on("end", () => this.canvas.classList.remove("is-panning"));

    d3.select(this.canvas)
      .call(this.zoom)
      .on("dblclick.zoom", null)
      .on("click", (event) => {
        if (event.target === this.canvas || event.target.tagName === "svg" || event.target.classList.contains("g-root")) {
          this.clearSelection();
        }
      });

    // Dragging moves one node and nothing else.
    //
    // The layout is settled before it is drawn and is not running afterwards, so
    // a drag has to place the node itself. The simulation is deliberately not
    // reheated here: letting the forces resolve the move is what made a drag
    // shuffle its neighbours around, and what previously let a node be pushed
    // on top of another. Overlap is refused outright instead.
    this.drag = d3.drag()
      .on("start", (event, d) => {
        this.nodeDragging = true;
        this.suppressClick = false;
        this.dragTravel = 0;
        this.dragOrigin = { x: event.x, y: event.y };
        d.fx = event.x;
        d.fy = event.y;
        d.x = event.x;
        d.y = event.y;
        this.canvas.classList.add("is-panning");
        this.#hideTooltip();
      })
      .on("drag", (event, d) => {
        // A move that would put this circle on top of another is not taken, so
        // the node simply stops at the last clear spot. The drag stays alive and
        // keeps working the moment the pointer moves clear again.
        if (this.#positionIsClear(d, event.x, event.y)) {
          d.fx = event.x;
          d.fy = event.y;
          // The layout is not running, so the position is applied here rather
          // than being picked up by a tick that will never come.
          d.x = event.x;
          d.y = event.y;
        }
        this.dragTravel = Math.max(
          this.dragTravel,
          Math.hypot(event.x - this.dragOrigin.x, event.y - this.dragOrigin.y)
        );
        this.#updatePositions();
        this.#computeLabelPlacements();
      })
      .on("end", (event, d) => {
        if (!event.active && this.simulation) {
          this.simulation.alphaTarget(0);
        }
        d.fx = null;
        d.fy = null;
        this.nodeDragging = false;
        this.canvas.classList.remove("is-panning");
        this.suppressClick = this.dragTravel > 5;
        if (this.suppressClick) {
          setTimeout(() => { this.suppressClick = false; }, 60);
        }
        this.#computeLabelPlacements();
      });
  }

  /**
   * Whether a node may sit at this point without touching another one.
   *
   * Only other nodes count. Edges are free to cross, and a node is still held
   * inside its own company bench by the usual clamp, so this is purely the
   * "two circles on top of each other" check.
   */
  #positionIsClear(node, x, y) {
    if (!Number.isFinite(x) || !Number.isFinite(y)) return false;
    const r = node.r || 8;

    for (const other of this.nodes) {
      if (other === node) continue;
      const need = r + (other.r || 8) + DRAG_CLEARANCE;
      const dx = x - (other.x || 0);
      const dy = y - (other.y || 0);
      if (dx * dx + dy * dy < need * need) return false;
    }
    return true;
  }

  /* ── Overlays: Inspector and Side Control Panel ─────────────────────────── */

  #buildUIOverlays() {
    const host = this.handlers.tooltipHost || this.canvas.parentElement;
    if (!host) return;

    if (!this.handlers.inspector) {
      let inspector = host.querySelector("#graph-inspector");
      if (!inspector) {
        inspector = document.createElement("div");
        inspector.id = "graph-inspector";
        inspector.className = "graph-inspector";
        inspector.hidden = true;
        host.append(inspector);
      }
      this.inspectorEl = inspector;
    } else {
      this.inspectorEl = this.handlers.inspector;
    }

    if (!this.handlers.sidePanel) {
      let panel = host.querySelector("#graph-side-panel");
      if (!panel) {
        panel = document.createElement("aside");
        panel.id = "graph-side-panel";
        panel.className = "graph-side-panel";
        host.append(panel);
      }
      this.sidePanelEl = panel;
    } else {
      this.sidePanelEl = this.handlers.sidePanel;
    }

    let fab = host.querySelector("#graph-filter-fab");
    if (!fab) {
      fab = document.createElement("button");
      fab.id = "graph-filter-fab";
      fab.className = "graph-filter-fab";
      fab.type = "button";
      fab.title = "Open graph filters & controls";
      fab.hidden = true;
      fab.innerHTML = `
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="M4 6h16M7 12h10M10 18h4"/></svg>
        <span>Filters</span>
      `;
      host.append(fab);
    }
    this.filterFabEl = fab;
    this.filterFabEl.addEventListener("click", () => this.openSidePanel());

    window.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        if (this.selected || this.searchPathNodes) {
          this.clearSelection();
        } else if (this.sidePanelEl && !this.sidePanelEl.classList.contains("is-collapsed")) {
          this.closeSidePanel();
        }
      }
    });
  }

  openSidePanel() {
    if (!this.sidePanelEl) return;
    this.sidePanelEl.classList.remove("is-collapsed");
    if (this.filterFabEl) this.filterFabEl.hidden = true;
    $("g-filter-btn")?.setAttribute("aria-pressed", "true");
    const legend = $("legend");
    if (legend) legend.style.display = "none";
  }

  closeSidePanel() {
    if (!this.sidePanelEl) return;
    this.sidePanelEl.classList.add("is-collapsed");
    if (this.filterFabEl) this.filterFabEl.hidden = false;
    $("g-filter-btn")?.setAttribute("aria-pressed", "false");
    const legend = $("legend");
    if (legend && $("g-legend-toggle")?.getAttribute("aria-pressed") === "true") {
      legend.style.display = "";
    }
  }

  toggleSidePanel() {
    if (this.sidePanelEl?.classList.contains("is-collapsed")) {
      this.openSidePanel();
    } else {
      this.closeSidePanel();
    }
  }

  /* ── Data Ingestion & Cluster Partitioning ─────────────────────────────── */

  setData(payload, { cited = [], fresh = false } = {}) {
    const rawNodes = payload?.nodes || [];
    const rawEdges = payload?.edges || [];
    const prev = new Map(this.allNodes.map((n) => [n.id, { x: n.x, y: n.y }]));

    this.allNodes = rawNodes
      .filter((n) => n && n.id !== undefined && n.id !== null)
      .map((n) => {
        const type = n.entity_type || n.type || "Unspecified";
        const name = n.name || String(n.id);
        const before = prev.get(String(n.id));
        const baseRadius = getNodeBaseRadius(type);

        return {
          ...n,
          id: String(n.id),
          type,
          name,
          baseRadius,
          r: baseRadius,
          x: Number.isFinite(before?.x) ? before.x : null,
          y: Number.isFinite(before?.y) ? before.y : null,
          vx: 0,
          vy: 0,
          degree: 0,
          clusterId: null,
          companyColor: null,
          nodeColor: null,
          isCompany: type === "Company",
        };
      });

    this.byId = new Map(this.allNodes.map((n) => [n.id, n]));

    this.allLinks = rawEdges
      .filter((e) => this.byId.has(String(e.source)) && this.byId.has(String(e.target)))
      .map((e) => ({
        ...e,
        source: this.byId.get(String(e.source)),
        target: this.byId.get(String(e.target)),
        relation: e.relation || "RELATED_TO",
        description: e.description || "",
      }));

    for (const link of this.allLinks) {
      link.source.degree += 1;
      link.target.degree += 1;
    }

    for (const n of this.allNodes) {
      const extra = Math.min(4, Math.sqrt(n.degree) * 0.9);
      n.r = (n.baseRadius + extra) * this.nodeScale;
    }

    // Parallel edges get a small offset so they don't draw on top of each other.
    const pairGroups = new Map();
    for (const link of this.allLinks) {
      const key = link.source.id < link.target.id
        ? `${link.source.id}|${link.target.id}`
        : `${link.target.id}|${link.source.id}`;
      if (!pairGroups.has(key)) pairGroups.set(key, []);
      pairGroups.get(key).push(link);
    }
    for (const group of pairGroups.values()) {
      group.forEach((l, i) => {
        l.curve = (i - (group.length - 1) / 2) * 1.5;
      });
    }

    this.#partitionCompanyClusters();

    this.seeds = new Set((payload?.seeds || []).map(String).filter((id) => this.byId.has(id)));
    const nextCited = new Set(cited.map(String).filter((id) => this.byId.has(id)));
    this.#flash(nextCited);
    this.cited = nextCited;

    this.#computeHopDistances();
    this.#filterAndApply();

    if (fresh || !this.fitted) {
      setTimeout(() => this.fit(), 450);
    }

    this.#renderSidePanel();
    return { nodes: this.nodes.length, links: this.links.length };
  }

  /**
   * Merge new entities/relationships into the master graph without removing existing data.
   */
  #mergeSubgraph(payload) {
    if (!payload?.nodes?.length) return;
    let added = false;
    for (const n of payload.nodes) {
      if (!n || n.id === undefined || n.id === null) continue;
      const idStr = String(n.id);
      if (!this.byId.has(idStr)) {
        const type = n.entity_type || n.type || "Unspecified";
        const baseRadius = getNodeBaseRadius(type);
        const nodeObj = {
          ...n,
          id: idStr,
          type,
          name: n.name || idStr,
          baseRadius,
          r: baseRadius * this.nodeScale,
          x: null,
          y: null,
          vx: 0,
          vy: 0,
          degree: 0,
          clusterId: null,
          isCompany: type === "Company",
        };
        this.allNodes.push(nodeObj);
        this.byId.set(idStr, nodeObj);
        added = true;
      }
    }

    const existing = new Set(this.allLinks.map(EDGE_KEY));
    for (const e of (payload.edges || [])) {
      const sId = String(e.source?.id || e.source);
      const tId = String(e.target?.id || e.target);
      if (!this.byId.has(sId) || !this.byId.has(tId)) continue;
      const key = `${sId}|${tId}|${e.relation || ""}`;
      if (existing.has(key)) continue;
      const sNode = this.byId.get(sId);
      const tNode = this.byId.get(tId);
      this.allLinks.push({
        ...e,
        source: sNode,
        target: tNode,
        relation: e.relation || "RELATED_TO",
        description: e.description || "",
        curve: 0,
      });
      existing.add(key);
      sNode.degree += 1;
      tNode.degree += 1;
      added = true;
    }

    if (added) {
      for (const n of this.allNodes) {
        const extra = Math.min(4, Math.sqrt(n.degree) * 0.9);
        n.r = (n.baseRadius + extra) * this.nodeScale;
      }
      this.#partitionCompanyClusters();
      this.#filterAndApply();
    }
  }

  /**
   * Narrow the rendered graph to the answer subgraph.
   *
   * The path is traced along real backend relationships only —
   * Company → Filing → Metric/Segment/Event → Document — so the highlighted
   * route is something the graph actually asserts. Every company reached on
   * that route is collected, which is what lets a comparison answer keep all
   * of its issuers on screen, each in its own colour.
   *
   * `allNodes` / `allLinks` are left untouched: this is a view, and clearing
   * the answer restores the whole graph.
   */
  highlightAnswer(result) {
    if (!result) return;

    if (this.allNodes.length === 0 && result.graph) {
      this.setData(result.graph);
    } else if (result.graph) {
      this.#mergeSubgraph(result.graph);
    }

    // 1. Cited entities: what the answer's own tags point at.
    const citedIds = new Set();
    const tagMap = result.tag_map || {};
    for (const tag of result.used_tags || []) {
      const id = tagMap[tag];
      if (id && this.byId.has(String(id))) citedIds.add(String(id));
    }
    if (Array.isArray(result.provenance)) {
      for (const p of result.provenance) {
        for (const tag of p.cites || []) {
          const id = tagMap[tag] || tag;
          if (id && this.byId.has(String(id))) citedIds.add(String(id));
        }
      }
    }
    for (const sid of result.graph?.seeds || []) {
      if (this.byId.has(String(sid))) citedIds.add(String(sid));
    }
    // Nothing was tagged: fall back to whatever subgraph the server returned.
    if (citedIds.size === 0 && result.graph?.nodes?.length) {
      for (const n of result.graph.nodes) {
        if (this.byId.has(String(n.id))) citedIds.add(String(n.id));
      }
    }

    const targetTicker = result.ticker || null;
    let targetCompanyNode = targetTicker ? this.byId.get(String(targetTicker)) : null;
    if (!targetCompanyNode && targetTicker) {
      targetCompanyNode = this.allNodes.find(
        (n) => n.isCompany && n.id.toUpperCase() === String(targetTicker).toUpperCase()
      );
    }

    // 2. Walk the path outward from every cited node.
    const pathNodes = new Set();
    const pathEdges = new Set();
    const companies = new Set();

    if (targetCompanyNode) {
      pathNodes.add(targetCompanyNode.id);
      companies.add(targetCompanyNode.id);
    }

    const incidentTo = (id) => this.allLinks.filter((l) => l.source.id === id || l.target.id === id);
    const addCompanyEdge = (filingId) => {
      for (const fl of incidentTo(filingId)) {
        const other = fl.source.id === filingId ? fl.target : fl.source;
        if (fl.relation === "SUBMITTED" || fl.relation === "FILED" || other.isCompany) {
          pathEdges.add(EDGE_KEY(fl));
          pathNodes.add(other.id);
          if (other.isCompany) companies.add(other.id);
        }
      }
    };

    for (const id of citedIds) {
      const node = this.byId.get(id);
      if (!node) continue;
      pathNodes.add(node.id);
      if (node.isCompany) {
        companies.add(node.id);
        continue;
      }

      if (node.type === "Segment") {
        // Segment ← metric, then metric → filing → company.
        for (const l of incidentTo(node.id)) {
          if (l.relation !== "DISAGGREGATED_BY") continue;
          const metric = l.source.id === node.id ? l.target : l.source;
          pathEdges.add(EDGE_KEY(l));
          pathNodes.add(metric.id);
          for (const ml of incidentTo(metric.id)) {
            const filing = ml.source.id === metric.id ? ml.target : ml.source;
            const leadsToFiling =
              ml.relation === "REPORTS_METRIC" || filing.type === "Filing" || filing.type === "Document";
            if (!leadsToFiling) continue;
            pathEdges.add(EDGE_KEY(ml));
            pathNodes.add(filing.id);
            addCompanyEdge(filing.id);
          }
        }
      }

      if (node.type === "FinancialMetric" || node.type === "DocumentChunk" || node.type === "DisclosureEvent") {
        for (const l of incidentTo(node.id)) {
          const other = l.source.id === node.id ? l.target : l.source;
          const leadsToFiling =
            l.relation === "REPORTS_METRIC" ||
            l.relation === "CONTAINS_CHUNK" ||
            l.relation === "DISCLOSES_EVENT" ||
            other.type === "Filing" ||
            other.type === "Document";
          if (!leadsToFiling) continue;
          pathEdges.add(EDGE_KEY(l));
          pathNodes.add(other.id);
          if (other.isCompany) companies.add(other.id);
          else addCompanyEdge(other.id);
        }
      }

      if (node.type === "Filing" || node.type === "Document") {
        addCompanyEdge(node.id);
      }
    }

    // 3. Keep any relationship the server sent that connects two path nodes.
    for (const re of result.graph?.edges || []) {
      const sId = String(re.source?.id || re.source);
      const tId = String(re.target?.id || re.target);
      if (!pathNodes.has(sId) || !pathNodes.has(tId)) continue;
      for (const l of this.allLinks) {
        if ((l.source.id === sId && l.target.id === tId) || (l.source.id === tId && l.target.id === sId)) {
          pathEdges.add(EDGE_KEY(l));
        }
      }
    }

    // 4. A targeted company should still be joined to the filings it rests on.
    if (targetCompanyNode) {
      for (const nId of pathNodes) {
        if (nId === targetCompanyNode.id) continue;
        const n = this.byId.get(nId);
        if (!n || (n.type !== "Filing" && n.type !== "Document")) continue;
        for (const l of this.allLinks) {
          if (l.source.id === targetCompanyNode.id && l.target.id === nId) pathEdges.add(EDGE_KEY(l));
          else if (l.target.id === targetCompanyNode.id && l.source.id === nId) pathEdges.add(EDGE_KEY(l));
        }
      }
    }

    // Every company that owns a node on the path stays on the path, no matter
    // how few direct connections it contributed. A company that was cited stays
    // on the answer whatever its degree, because dropping it is what makes an
    // answer silently lose one of the companies it is about.
    for (const nId of [...pathNodes]) {
      const n = this.byId.get(nId);
      if (n?.clusterId) companies.add(n.clusterId);
    }
    for (const companyId of companies) {
      if (this.byId.has(companyId)) pathNodes.add(companyId);
    }

    // A relationship between two companies that are both in the answer is part
    // of the answer, so keep it whenever the corpus has it. This runs after the
    // company set is final, otherwise a cross-company edge could be dropped by
    // a filter that did not yet know the other end was involved.
    for (const l of this.allLinks) {
      const s = l.source, t = l.target;
      if (!s?.isCompany || !t?.isCompany) continue;
      if (!companies.has(s.id) || !companies.has(t.id)) continue;
      pathEdges.add(EDGE_KEY(l));
      pathNodes.add(s.id);
      pathNodes.add(t.id);
    }

    // An involved company with no surviving edge still gets its bench, plus a
    // link to the nearest cited filing so it is not a floating label. Ordering
    // matters: a company that owns a filing on the path keeps its real
    // submission edge, and this only fills a genuine gap.
    for (const companyId of companies) {
      if ([...pathEdges].some((k) => k.includes(companyId))) continue;
      let best = null;
      for (const nId of pathNodes) {
        if (nId === companyId) continue;
        const n = this.byId.get(nId);
        if (!n || n.type !== "Filing") continue;
        if (!best || n.degree > best.degree) best = n;
      }
      if (best) {
        for (const l of this.allLinks) {
          if ((l.source.id === companyId && l.target.id === best.id) ||
              (l.target.id === companyId && l.source.id === best.id)) {
            pathEdges.add(EDGE_KEY(l));
            break;
          }
        }
      }
    }

    this.answerPathNodes = pathNodes;
    this.answerPathEdges = pathEdges;
    this.answerCompanyIds = companies;
    this.cited = citedIds;
    this.searchPathNodes = null;
    this.searchPathEdges = null;
    this.searchPathTargetId = null;

    // Re-run the filter against the smaller set. Deliberately no `reseed`: the
    // answer is a filtered view of the same layout, so every company keeps the
    // bench position it already had in the main graph. Re-seeding here was what
    // produced a fresh radial arrangement around the queried company instead of
    // the cluster layout on screen.
    this.#filterAndApply();
    this.#startFlow(this.links.filter((l) => this.answerPathEdges.has(EDGE_KEY(l))));

    setTimeout(() => this.#frameAnswerClusters(), 260);
  }

  /**
   * Frame every company bench the answer touches, as one group.
   *
   * Framing the answer as a set of benches rather than a set of nodes is what
   * keeps a multi-company answer from being magnified into a single knot: the
   * per-cluster padding is part of the bounds, and the zoom is capped so a small
   * answer is never blown up across the whole screen.
   */
  #frameAnswerClusters() {
    if (this.disposed || !this.answerPathNodes || this.answerPathNodes.size === 0) return;
    const { width, height } = this.#size();
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    let count = 0;

    const grow = (x, y, r) => {
      minX = Math.min(minX, x - r); maxX = Math.max(maxX, x + r);
      minY = Math.min(minY, y - r); maxY = Math.max(maxY, y + r);
    };

    // The benches first: an involved company stays on screen even when it
    // contributed only one node, and the ring is what the eye reads as the
    // company's footprint.
    for (const id of this.answerCompanyIds) {
      const cluster = this.companyClusters.get(id);
      if (!cluster || !Number.isFinite(cluster.cx)) continue;
      grow(cluster.cx, cluster.cy, (cluster.radius || 90) + 46);
      count++;
    }

    // Then any answer node that sits outside its bench (a shared entity).
    for (const id of this.answerPathNodes) {
      const n = this.byId.get(id);
      if (!n || !Number.isFinite(n.x)) continue;
      grow(n.x, n.y, (n.r || 8) + 44);
      count++;
    }

    if (count === 0) return;

    const spanX = Math.max(240, maxX - minX);
    const spanY = Math.max(240, maxY - minY);
    const k = Math.max(0.4, Math.min(1.1, 0.94 * Math.min(width / spanX, height / spanY)));
    const tx = width / 2 - ((minX + maxX) / 2) * k;
    const ty = height / 2 - ((minY + maxY) / 2) * k;

    d3.select(this.canvas)
      .transition()
      .duration(700)
      .ease(d3.easeCubicOut)
      .call(this.zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(k));
  }

  /** Drop the answer filter and return to the full graph. */
  clearAnswerHighlight() {
    this.answerPathNodes = null;
    this.answerPathEdges = null;
    this.answerCompanyIds = new Set();
    this.cited = new Set();
    this.selected = null;
    this.searchPathNodes = null;
    this.searchPathEdges = null;
    this.searchPathTargetId = null;
    if (this.inspectorEl) this.inspectorEl.hidden = true;
    this.#stopFlow();
    // No reseed here either: clearing the answer returns the nodes to the
    // positions the exploration layout already settled on, so the graph comes
    // back exactly as the user left it instead of scattering again.
    this.#filterAndApply();
    this.fit();
  }

  /**
   * Assign every entity to exactly one company cluster and give that company
   * a single accent colour. Colour is keyed off the sorted ticker order, so a
   * company keeps the same colour across reloads instead of shifting when the
   * server returns rows in a different order.
   */
  #partitionCompanyClusters() {
    const companies = this.allNodes.filter((n) => n.isCompany);
    this.companyClusters.clear();

    for (const n of this.allNodes) {
      n.clusterId = null;
      n.companyId = null;
      n.companyName = null;
      n.companyColor = null;
      n.nodeColor = null;
    }

    const ordered = [...companies].sort((a, b) => a.id.localeCompare(b.id));
    ordered.forEach((c, i) => {
      c.clusterId = c.id;
      c.companyId = c.id;
      c.ticker = c.id;
      c.companyColor = COMPANY_PALETTE[i % COMPANY_PALETTE.length];
      this.companyClusters.set(c.id, {
        ticker: c.id,
        name: c.name,
        color: c.companyColor,
        nodes: new Set([c.id]),
        radius: 0,
        cx: null,
        cy: null,
      });
    });

    // Filings hang directly off the company that submitted them.
    for (const link of this.allLinks) {
      if (link.relation !== "SUBMITTED" && link.relation !== "FILED") continue;
      const company = link.source.isCompany ? link.source : (link.target.isCompany ? link.target : null);
      if (!company?.clusterId) continue;
      const filing = company === link.source ? link.target : link.source;
      filing.clusterId = company.clusterId;
      this.companyClusters.get(company.clusterId)?.nodes.add(filing.id);
    }

    // Metrics, segments and events hang off a filing, so they inherit it.
    for (const link of this.allLinks) {
      if (!["REPORTS_METRIC", "DISCLOSES_EVENT", "CONTAINS_CHUNK"].includes(link.relation)) continue;
      const filing = link.source.type === "Filing" || link.source.type === "Document"
        ? link.source
        : (link.target.type === "Filing" || link.target.type === "Document" ? link.target : null);
      if (!filing?.clusterId) continue;
      const leaf = filing === link.source ? link.target : link.source;
      if (leaf.clusterId) continue;
      leaf.clusterId = filing.clusterId;
      leaf.sourceFiling = filing.name;
      this.companyClusters.get(filing.clusterId)?.nodes.add(leaf.id);
    }

    // Segments are disaggregated from a metric, which already has a company.
    for (const link of this.allLinks) {
      if (link.relation !== "DISAGGREGATED_BY") continue;
      const metric = link.source.clusterId ? link.source : (link.target.clusterId ? link.target : null);
      if (!metric?.clusterId) continue;
      const segment = metric === link.source ? link.target : link.source;
      if (segment.clusterId) continue;
      segment.clusterId = metric.clusterId;
      this.companyClusters.get(metric.clusterId)?.nodes.add(segment.id);
    }

    // Colours last, now that ownership is settled.
    for (const n of this.allNodes) {
      const cluster = n.clusterId ? this.companyClusters.get(n.clusterId) : null;
      if (!cluster) continue;
      n.companyId = cluster.ticker;
      n.companyName = cluster.name;
      n.companyColor = cluster.color;
      n.nodeColor = n.isCompany ? cluster.color : mix(cluster.color, ENTITY_TINT, 0.52);
    }
  }

  /* ── Rendered Subset ───────────────────────────────────────────────────── */

  #filterAndApply({ reseed = false } = {}) {
    const answering = !!(this.answerPathNodes && this.answerPathNodes.size > 0);

    // In answer state the subgraph is the spec: unrelated nodes, hidden types
    // and the company filter all stand aside so the answer is never partially
    // filtered out by a control left over from exploration.
    this.nodes = this.allNodes.filter((n) => {
      if (answering) return this.answerPathNodes.has(n.id);
      if (this.hiddenTypes.has(String(n.type).toLowerCase())) return false;
      if (this.companyFilter !== "ALL") {
        if (n.clusterId && n.clusterId !== this.companyFilter) return false;
        if (n.isCompany && n.id !== this.companyFilter) return false;
      }
      return true;
    });

    const active = new Set(this.nodes.map((n) => n.id));
    this.links = this.allLinks.filter((l) => {
      if (!active.has(l.source.id) || !active.has(l.target.id)) return false;
      if (answering) return this.answerPathEdges.has(EDGE_KEY(l));
      return !this.hiddenRelations.has(l.relation);
    });

    // Positions carry across filters: a node that was already placed keeps its
    // spot, which is what makes an answer read as the same layout with less in
    // it rather than as a newly drawn diagram. `reseed` stays available for
    // callers that genuinely want a fresh scatter.
    if (reseed) {
      for (const n of this.nodes) {
        n.x = null;
        n.y = null;
      }
    }

    this.#seedClusters();
    this.#buildSimulation();
    this.#draw();
    this.#run();
  }

  /* ── 2D Cluster Seeding ────────────────────────────────────────────────── */

  /**
   * Give every company a slot on a grid, then scatter its members on a small
   * disc around that slot.
   *
   * The disc is a sunflower spiral rather than a ring so a cluster fills
   * evenly instead of forming an obvious hollow circle, and the offset is
   * derived from a hash of the node id so the arrangement is identical on
   * every load and after every re-filter.
   */
  #seedClusters() {
    const { width, height } = this.#size();
    const cx = width / 2;
    const cy = height / 2;
    const answering = !!(this.answerPathNodes && this.answerPathNodes.size > 0);

    const groups = new Map();
    for (const n of this.nodes) {
      const key = n.clusterId || "__shared__";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(n);
    }

    const ordered = [...groups.keys()]
      .filter((k) => k !== "__shared__" && this.companyClusters.has(k))
      .sort((a, b) => a.localeCompare(b));

    // A roughly square arrangement: four companies read as two rows of two,
    // six as three rows of two, and so on.
    const cols = Math.max(1, Math.ceil(Math.sqrt(ordered.length)));
    const rows = Math.max(1, Math.ceil(ordered.length / cols));
    const stepX = cols === 1 ? 0 : (width / (cols + 0.4)) * 0.88;
    const stepY = rows === 1 ? 0 : (height / (rows + 0.4)) * 0.88;

    // A cluster may not be wider than the gap between slots, or two companies
    // would overlap before the collision force ever ran. The 46 covers the 30px
    // bench ring plus a gap, so two neighbouring rings never touch.
    const radiusCap = Math.max(46, 0.5 * Math.min(stepX || width, stepY || height) - 46);

    ordered.forEach((key, i) => {
      const cluster = this.companyClusters.get(key);
      const members = groups.get(key) || [];
      const col = i % cols;
      const row = Math.floor(i / cols);

      const gx = cx + (cols === 1 ? 0 : (col / Math.max(1, cols - 1) - 0.5)) * stepX;
      const gy = cy + (rows === 1 ? 0 : (row / Math.max(1, rows - 1) - 0.5)) * stepY;

      // Cluster radius grows with membership, so a big company gets room and a
      // small one stays a tight knot — up to the cap above.
      //
      // An answer shows a handful of nodes per company, so it gets the same
      // formula evaluated against what is actually on screen. Sizing it from the
      // company's full membership left a wide empty bench with a few nodes
      // stranded out at the collision radius.
      const fanout = answering ? members.length : Math.max(members.length, cluster.nodes.size);
      cluster.radiusCap = radiusCap;
      cluster.radius = Math.min(radiusCap, 54 + 24 * Math.sqrt(fanout));
      cluster.cx = gx;
      cluster.cy = gy;
      const R = cluster.radius;

      members.forEach((n, j) => {
        n.slotX = gx;
        n.slotY = gy;
        if (Number.isFinite(n.x)) return;

        if (n.isCompany) {
          n.x = gx;
          n.y = gy;
          return;
        }

        // Sunflower spiral: even area coverage, no visible ring structure.
        //
        // Radius scales with the square root of the member count, which is what
        // keeps two nodes from sitting at the same distance and reading as a
        // symmetric burst. The `j + 0.6` numerator biases the first node
        // inward rather than dropping it on the outer edge.
        const others = Math.max(1, members.length - 1);
        const t = (j + 0.6) / (others + 0.4);
        const golden = Math.PI * (3 - Math.sqrt(5));
        const angle = j * golden + hashUnit(n.id) * 0.9;
        const radius = R * 0.62 * Math.sqrt(t);
        const jitter = (hashUnit(`${n.id}:j`) - 0.5) * 7;

        n.x = gx + Math.cos(angle) * radius + jitter;
        n.y = gy + Math.sin(angle) * radius + jitter * 0.7;
      });
    });

    // Shared entities have no company to belong to. They get the middle of the
    // canvas with no anchor of their own, so collision keeps them out of the
    // way without inventing a company for them.
    for (const n of groups.get("__shared__") || []) {
      n.slotX = cx;
      n.slotY = cy;
      if (Number.isFinite(n.x)) continue;
      const angle = hashUnit(n.id) * Math.PI * 2;
      const radius = 30 + hashUnit(`${n.id}:s`) * 90;
      n.x = cx + Math.cos(angle) * radius;
      n.y = cy + Math.sin(angle) * radius;
    }
  }

  /* ── 2D Force Simulation ───────────────────────────────────────────────── */

  #buildSimulation() {
    this.simulation?.stop();

    const { width, height } = this.#size();
    for (const n of this.nodes) {
      if (!Number.isFinite(n.x)) { n.x = width / 2; n.y = height / 2; }
      n.vx = 0;
      n.vy = 0;
    }

    const present = new Set(this.nodes.map((n) => n.clusterId).filter(Boolean));
    let maxClusterRadius = 90;
    for (const key of present) {
      maxClusterRadius = Math.max(maxClusterRadius, this.companyClusters.get(key)?.radius || 0);
    }

    // A label is far wider than the circle it names -- a filing label runs to
    // 160px against a 14px node. Exploration packs tight because most labels are
    // hidden and only the node has to be clickable. The answer subgraph is the
    // one place every label is shown, so it needs room for the text, not just
    // the circles: collision padding grows to match. Without this the answer
    // graph renders correctly but reads as a pile-up of overlapping captions.
    //
    // The padding is the only thing that changes. Link distances stay at the
    // exploration values on purpose -- stretching them was what spread an answer
    // across the canvas and pulled each company away from its own bench.
    const answering = !!(this.answerPathNodes && this.answerPathNodes.size > 0);
    // Collision has to clear the caption, not just the circle. Half of a label
    // is the right unit: two labels whose centres are a full label-width apart
    // cannot touch however they are anchored.
    const labelHalf = answering ? estimateLabelWidth(this.nodes) * 0.5 : 0;
    const pad = answering ? Math.max(26, Math.min(56, labelHalf * 0.6)) : 15;

    // Size each bench so its members actually fit inside it.
    //
    // This is what stops a cluster turning into a ring. If the bench is smaller
    // than the members need, collision pushes them outward until they all pile
    // against the boundary at an identical radius -- a starburst. Giving the
    // bench enough room for the nodes and their captions lets collision spread
    // them through the interior instead, which is what makes a cluster read as a
    // cluster. Packing is a disc, so the radius grows with the square root of the
    // member count.
    for (const key of new Set(this.nodes.map((n) => n.clusterId).filter(Boolean))) {
      const cluster = this.companyClusters.get(key);
      if (!cluster) continue;
      const members = this.nodes.filter((n) => n.clusterId === key && !n.isCompany);
      if (!members.length) {
        cluster.benchRadius = 54;
        continue;
      }
      const avgR = members.reduce((a, n) => a + (n.r || 8), 0) / members.length;
      const needed = (avgR + pad) * Math.sqrt(members.length) * 1.15;
      cluster.benchRadius = Math.max(54, Math.min(cluster.radiusCap || Infinity, needed));
    }

    this.simulation = d3.forceSimulation(this.nodes)
      // Related nodes hold a short, readable distance.
      .force("link", d3.forceLink(this.links)
        .id((d) => d.id)
        .distance((l) => linkDistance(l.relation))
        .strength(0.55))

      // Moderate repulsion, capped so a dense cluster stays local. An answer has
      // far fewer nodes in flight, so it needs less push to stay separated.
      // `distanceMax` is what stops a member being flung out of its own bench.
      //
      // In answer mode this is deliberately close to zero. The bench is only a
      // few nodes across, so any real repulsion presses every member outward
      // until they all rest at an identical distance from the company -- the
      // starburst. Collision below does the separating inside the bench.
      .force("charge", d3.forceManyBody()
        .strength(answering ? -30 : -180)
        .distanceMax(Math.min(maxClusterRadius * 1.9, 260)))

      // Circles never overlap.
      .force("collide", d3.forceCollide()
        .radius((d) => (d.r || 8) + pad)
        .strength(0.9)
        .iterations(2))

      // Every node is held near its company's bench. This is what makes a
      // company read as one blob instead of being smeared across the canvas.
      // The strength ramps up near the bench edge, so members stay comfortably
      // spread through the interior rather than settling onto one circle.
      .force("clusterX", d3.forceX((d) => d.slotX ?? 0).strength((d) => this.#benchHold(d)))
      .force("clusterY", d3.forceY((d) => d.slotY ?? 0).strength((d) => this.#benchHold(d)))

      // Start cool and cool down fast. The seed above has already placed every
      // node on a sensible spot inside its bench, so the simulation only has to
      // make small corrections. Letting it start hot and decay slowly made the
      // whole graph visibly slither for several seconds after every load and
      // filter change, which read as a glitch rather than as a layout.
      .alpha(0.3)
      .alphaDecay(0.06)
      .velocityDecay(0.42);

    // Run the forces to convergence *before* the first paint.
    //
    // The graph should appear already laid out rather than visibly rearranging
    // itself for a second or two after every load. Because nothing is rendered
    // during these ticks, the cost is a few milliseconds of main-thread work
    // instead of a long animation -- and it also means labels are placed once
    // against final positions, so they do not start out overlapping while the
    // nodes are still sliding underneath them.
    for (let i = 0; i < SETTLE_TICKS; i++) {
      if (this.simulation.alpha() <= this.simulation.alphaMin()) break;
      this.simulation.tick();
      // The bench clamp normally runs while rendering, one pass per frame. Doing
      // it here too is what makes this settle identical to the animated version
      // it replaces -- without it, members pile up on the bench edge where
      // collision cannot separate them, and the finished layout has overlaps in
      // it that never get resolved.
      this.#constrainToBenches();
    }

    // Nothing is left to animate. Dragging still works because a drag sets the
    // node's position directly rather than relying on the simulation running.
    this.simulation.alpha(0);
    this.simulation.stop();
  }

  /* ── Render Loop ───────────────────────────────────────────────────────── */

  #run() {
    if (this.frame) cancelAnimationFrame(this.frame);
    const loop = () => {
      if (this.disposed) return;
      if (this.simulation) {
        this.simulation.tick();
        this.#updatePositions();

        // Labels are the expensive part; a few times a second is plenty while
        // things are still moving.
        if ((this.tickCount = (this.tickCount || 0) + 1) % 8 === 0) {
          this.#computeLabelPlacements();
        }
      }

      const settled = !this.simulation || this.simulation.alpha() <= this.simulation.alphaMin();
      if (settled) {
        this.#computeLabelPlacements();
        this.frame = null;
      } else {
        this.frame = requestAnimationFrame(loop);
      }
    };
    this.frame = requestAnimationFrame(loop);
  }

  /**
   * How firmly a node is held to the centre of its own bench.
   *
   * The pull is gentle while a node is comfortably inside its bench and ramps up
   * sharply once it nears the edge. A flat pull collapses everything onto the
   * centre point, while a flat *hard* boundary makes every node settle at an
   * identical radius -- which is precisely the ring/starburst this layout is
   * meant to avoid. Grading it leaves the interior free for collision to fill,
   * and still guarantees nothing escapes its company.
   */
  #benchHold(d) {
    if (d.isCompany) return 0.95;

    const base = 0.22;
    const cluster = this.companyClusters.get(d.clusterId);
    const limit = cluster?.benchRadius || 0;
    if (!limit || !Number.isFinite(d.x) || !Number.isFinite(d.y)) return base;

    const distance = Math.hypot(d.x - d.slotX, d.y - d.slotY);
    const soft = limit * 0.7;
    if (distance <= soft) return base;
    const over = (distance - soft) / (limit - soft);
    return Math.min(2.4, base * (1 + 14 * over));
  }

  /**
   * Keep every node on the bench it belongs to.
   *
   * The forces hold nodes in the right neighbourhood; this makes it a hard
   * guarantee. Without it, members drift between companies as alpha decays and
   * the ring drawn around a company ends up containing somebody else's nodes.
   *
   * A company is pinned outright: it is the centre of its own bench, so it must
   * not drift inside the ring drawn around it.
   */
  #constrainToBenches() {
    for (const n of this.nodes) {
      if (!Number.isFinite(n.x) || !Number.isFinite(n.y)) continue;
      const slotX = n.slotX, slotY = n.slotY;
      if (!Number.isFinite(slotX) || !Number.isFinite(slotY)) continue;

      if (n.isCompany) {
        n.x = slotX;
        n.y = slotY;
        n.vx = 0;
        n.vy = 0;
        continue;
      }

      const limit = this.companyClusters.get(n.clusterId)?.benchRadius || 0;
      if (!limit) continue;

      const dx = n.x - slotX;
      const dy = n.y - slotY;
      const d = Math.hypot(dx, dy);
      if (d <= limit || d === 0) continue;

      const k = limit / d;
      n.x = slotX + dx * k;
      n.y = slotY + dy * k;
      // Drop the outward component of velocity, so a node that reached the edge
      // stops grinding along it and is free to be pulled back inward.
      const nx = dx / d, ny = dy / d;
      const outward = n.vx * nx + n.vy * ny;
      if (outward > 0) {
        n.vx -= outward * nx;
        n.vy -= outward * ny;
      }
    }
  }

  #updatePositions() {
    this.#constrainToBenches();

    this.layerEdges.selectAll("path.g-edge").attr("d", (l) => this.#edgePath(l));
    this.layerEdgeFlow.selectAll("path.g-edge-flow").attr("d", (l) => this.#edgePath(l));
    this.layerNodes.selectAll("g.g-node").attr("transform", (d) => `translate(${d.x || 0},${d.y || 0})`);

    // Each bench is centred on its company, so keep that centre current for
    // answer framing. There is no ring drawn around the company any more, but
    // the centre is still what the answer view aims the viewport at.
    for (const node of this.nodes) {
      if (!node.isCompany) continue;
      const cluster = this.companyClusters.get(node.clusterId);
      if (!cluster) continue;
      cluster.cx = node.x;
      cluster.cy = node.y;
    }
  }

  /* ── Hop Computation for Answering & RAG Citations ─────────────────────── */

  #computeHopDistances() {
    if (!this.seeds.size && !this.cited.size) {
      this.hopCache.clear();
      return;
    }

    const queue = [];
    const distances = new Map();

    for (const id of [...this.seeds, ...this.cited]) {
      if (!distances.has(id)) {
        distances.set(id, 0);
        queue.push({ id, dist: 0 });
      }
    }

    const adj = new Map();
    for (const node of this.allNodes) adj.set(node.id, []);
    for (const link of this.allLinks) {
      adj.get(link.source.id)?.push(link.target.id);
      adj.get(link.target.id)?.push(link.source.id);
    }

    while (queue.length) {
      const { id, dist } = queue.shift();
      if (dist >= this.maxHops) continue;
      for (const neighbor of adj.get(id) || []) {
        if (!distances.has(neighbor) || distances.get(neighbor) > dist + 1) {
          distances.set(neighbor, dist + 1);
          queue.push({ id: neighbor, dist: dist + 1 });
        }
      }
    }

    this.hopCache = distances;
  }

  /* ── Draw & Data Join ──────────────────────────────────────────────────── */

  #draw() {
    this.#applyView();
    this.#drawFlow();

    let highlightedNodes = null;
    let highlightedEdges = null;

    if (this.answerPathNodes && this.answerPathNodes.size > 0) {
      highlightedNodes = this.answerPathNodes;
      highlightedEdges = this.answerPathEdges;
    } else if (this.searchPathNodes) {
      highlightedNodes = this.searchPathNodes;
      highlightedEdges = this.searchPathEdges;
    } else {
      const activeId = this.hovered || this.selected;
      if (activeId) {
        highlightedNodes = new Set([activeId]);
        highlightedEdges = new Set();
        for (const l of this.links) {
          if (l.source.id === activeId || l.target.id === activeId) {
            highlightedEdges.add(EDGE_KEY(l));
            highlightedNodes.add(l.source.id);
            highlightedNodes.add(l.target.id);
          }
        }
      }
    }

    const hasFocus = !!highlightedNodes;
    const answering = !!this.answerPathNodes?.size;

    // Answer-path halo. Only these few edges get one — a blur filter across
    // every edge is the single most expensive thing this canvas could do.
    const glowLinks = this.answerPathEdges
      ? this.links.filter((l) => this.answerPathEdges.has(EDGE_KEY(l)))
      : [];

    this.layerEdgeGlow.selectAll("path.g-edge-glow")
      .data(glowLinks, EDGE_KEY)
      .join(
        (enter) => enter.append("path").attr("class", "g-edge-glow"),
        (update) => update,
        (exit) => exit.remove()
      )
      .attr("d", (l) => this.#edgePath(l))
      .attr("stroke", (l) => l.source.companyColor || "currentColor");

    // Edges.
    this.layerEdges.selectAll("path.g-edge")
      .data(this.links, EDGE_KEY)
      .join(
        (enter) => {
          const path = enter.append("path").attr("class", "g-edge");
          path.append("title");
          return path;
        },
        (update) => update,
        (exit) => exit.remove()
      )
      .attr("d", (l) => this.#edgePath(l))
      .attr("class", (l) => {
        const key = EDGE_KEY(l);
        return [
          "g-edge",
          this.answerPathEdges?.has(key) ? "is-answer-path" : "",
          this.cited.has(l.source.id) && this.cited.has(l.target.id) ? "is-cited" : "",
          hasFocus && highlightedEdges.has(key) ? "is-hot" : "",
          hasFocus && !highlightedEdges.has(key) ? "is-dim" : "",
          l.source.clusterId && l.source.clusterId === l.target.clusterId ? "is-company-edge" : "",
        ].filter(Boolean).join(" ");
      })
      .attr("marker-end", (l) => {
        if (!this.showArrows) return null;
        if (hasFocus && !highlightedEdges.has(EDGE_KEY(l))) return null;
        return answering ? "url(#arrow-hot)" : "url(#arrow)";
      })
      .style("stroke", (l) => {
        if (this.answerPathEdges?.has(EDGE_KEY(l))) return l.source.companyColor || "currentColor";
        if (this.cited.has(l.source.id) && this.cited.has(l.target.id)) return l.source.companyColor || "currentColor";
        const sameCompany = l.source.clusterId && l.source.clusterId === l.target.clusterId;
        return sameCompany ? (l.source.companyColor || "currentColor") : "currentColor";
      })
      .style("stroke-width", (l) => {
        const px = this.answerPathEdges?.has(EDGE_KEY(l)) ? 1.5
          : (l.relation === "SUBMITTED" || l.relation === "FILED") ? 0.8
            : 0.6;
        return `${px * this.linkScale}px`;
      })
      .style("opacity", (l) => {
        if (this.answerPathEdges?.has(EDGE_KEY(l))) return 0.9;
        if (hasFocus) return highlightedEdges.has(EDGE_KEY(l)) ? 0.85 : 0.22;
        if (this.cited.has(l.source.id) && this.cited.has(l.target.id)) return 0.6;
        return 0.38;
      });

    this.layerEdges.selectAll("path.g-edge > title").text((l) =>
      `${l.source.name} —[${l.relation}]→ ${l.target.name}${l.description ? `\n${l.description}` : ""}`
    );

    // Edge labels only once the view is close enough for them to be readable,
    // or on the focused path.
    const showEdgeLabels = this.links.filter((l) => {
      if (!l.source || !l.target) return false;
      const dist = Math.hypot((l.target.x || 0) - (l.source.x || 0), (l.target.y || 0) - (l.source.y || 0));
      if (dist < (l.source.r || 8) + (l.target.r || 8) + 42) return false;
      if (hasFocus) return highlightedEdges.has(EDGE_KEY(l));
      return this.view.k > 1.3;
    });

    this.layerEdgeLabels.selectAll("text.g-edge-label")
      .data(showEdgeLabels, EDGE_KEY)
      .join(
        (enter) => enter.append("text").attr("class", "g-edge-label").attr("text-anchor", "middle"),
        (update) => update,
        (exit) => exit.remove()
      )
      .attr("x", (l) => this.#edgePoint(l, 0.5).x)
      .attr("y", (l) => this.#edgePoint(l, 0.5).y - 4)
      .text((l) => l.relation.replace(/_/g, " ").toLowerCase());

    // Nodes: one solid circle each.
    const groups = this.layerNodes.selectAll("g.g-node")
      .data(this.nodes, (d) => d.id)
      .join(
        (enter) => {
          const g = enter.append("g").attr("class", "g-node");
          g.append("circle").attr("class", "core");
          g.append("title");
          g.append("text").attr("class", "g-node-label").attr("text-anchor", "middle");
          g.call(this.drag);

          g.on("pointerenter", (event, d) => {
            this.hovered = d.id;
            this.#showTooltip(event, d);
            this.#draw();
          })
            .on("pointerleave", () => {
              this.hovered = null;
              this.#hideTooltip();
              this.#draw();
            })
            .on("click", (event, d) => {
              event.stopPropagation();
              if (!this.suppressClick) this.selectNode(d.id === this.selected ? null : d.id);
            });
          return g;
        },
        (update) => update,
        (exit) => exit.remove()
      )
      .attr("class", (d) => {
        const isAnswerNode = this.answerPathNodes?.has(d.id);
        const isDim = hasFocus && !highlightedNodes.has(d.id);
        return [
          "g-node",
          `type-${String(d.type).toLowerCase()}`,
          isAnswerNode ? "is-answer-path" : "",
          d.id === this.selected ? "is-selected" : "",
          d.id === this.searchPathTargetId && this.searchPathNodes ? "is-selected" : "",
          d.id === this.hovered ? "is-hovered" : "",
          this.cited.has(d.id) ? "is-cited" : "",
          this.fresh.has(d.id) ? "is-fresh" : "",
          isDim ? "is-dim" : "",
          d.isCompany ? "is-root" : "",
        ].filter(Boolean).join(" ");
      })
      .attr("transform", (d) => `translate(${d.x || 0},${d.y || 0})`);

    groups.select("circle.core")
      .attr("r", (d) => Math.max(3, d.r || 7))
      .attr("fill", (d) => d.nodeColor || d.companyColor || typeColor(d.type))
      .attr("stroke", (d) => (d.id === this.selected || d.id === this.hovered)
        ? "var(--text)"
        : "var(--graph-bg, #050505)");

    groups.select("text.g-node-label")
      .text((d) => (d.name.length > 30 ? `${d.name.slice(0, 29)}…` : d.name));

    groups.select("title")
      .text((d) => `${d.name} (${prettyType(d.type)})${d.description ? ` — ${d.description}` : ""}`);

    this.#computeLabelPlacements();
  }

  /* ── Label Placement ──────────────────────────────────────────────────── */

  /**
   * Place labels outside their node, searching a ring of candidate offsets and
   * scoring each against three things: other nodes, labels already placed, and
   * the edges leaving this node. Company and answer labels are placed first so
   * they get first refusal on the clearest spot.
   *
   * Node lookup goes through a coarse grid. Walking every node for every
   * candidate is quadratic, which is fine at 40 nodes and not fine at 800.
   */
  #computeLabelPlacements() {
    if (!this.nodes.length) return;

    const k = this.view.k;
    const mode = this.labelsMode;
    const activeId = this.hovered || this.selected;
    const answering = !!this.answerPathNodes?.size;

    // Spatial hash of node positions.
    const CELL = 96;
    const grid = new Map();
    for (const n of this.nodes) {
      const gx = Math.floor((n.x || 0) / CELL);
      const gy = Math.floor((n.y || 0) / CELL);
      const key = `${gx},${gy}`;
      if (!grid.has(key)) grid.set(key, []);
      grid.get(key).push(n);
    }
    const nearby = (x, y) => {
      const gx = Math.floor(x / CELL);
      const gy = Math.floor(y / CELL);
      const out = [];
      for (let i = -1; i <= 1; i++) {
        for (let j = -1; j <= 1; j++) {
          const bucket = grid.get(`${gx + i},${gy + j}`);
          if (bucket) out.push(...bucket);
        }
      }
      return out;
    };

    // Mean direction of this node's edges: labels prefer the quiet side.
    // Accumulated in one pass over the links rather than by rescanning every
    // link for every node, which would be quadratic and runs on every tick.
    const acc = new Map();
    for (const n of this.nodes) acc.set(n.id, { dx: 0, dy: 0, count: 0 });

    for (const l of this.links) {
      const s = l.source;
      const t = l.target;
      if (!Number.isFinite(s.x) || !Number.isFinite(t.x)) continue;
      const ddx = s.x - t.x;
      const ddy = s.y - t.y;
      const d = Math.hypot(ddx, ddy) || 1;
      const a = acc.get(s.id);
      if (a) { a.dx += ddx / d; a.dy += ddy / d; a.count++; }
      const b = acc.get(t.id);
      if (b) { b.dx -= ddx / d; b.dy -= ddy / d; b.count++; }
    }

    const away = new Map();
    for (const [id, a] of acc) {
      const len = Math.hypot(a.dx, a.dy);
      away.set(id, a.count && len > 0.01 ? { x: a.dx / len, y: a.dy / len } : { x: 0, y: -1 });
    }

    // Rank decides who gets a label. Rank 1 is the node under the cursor or in
    // the inspector, 2 is the search path, 3 is a company, 4 a filing, 5 a well
    // connected node, 6 the long tail of degree-1 leaves.
    //
    // The tail is capped rather than merely deprioritised. A filing can report
    // 250 metrics whose names differ only by a date, and labelling all of them
    // is the one thing guaranteed to turn a clean layout into noise. Hover or
    // select any node to name it regardless of the cap.
    const candidates = [];
    let tailBudget = answering ? ANSWER_LABEL_CAP : SMART_LABEL_CAP;

    for (const d of this.nodes) {
      let mustShow = false;
      let rank = 6;

      if (answering) {
        // Answer subgraph: label the spine of it. The company and anything
        // with more than one relationship is the shape of the argument; the
        // single-hop leaves compete for the tail budget like everything else,
        // because a broad answer should thin out rather than overlap.
        mustShow = d.isCompany || d.degree >= 2;
        rank = d.isCompany ? 1 : (d.degree >= 2 ? 2 : 3);
      } else if (d.id === activeId) {
        mustShow = true;
        rank = 1;
      } else if (this.searchPathNodes?.has(d.id)) {
        mustShow = true;
        rank = 2;
      } else if (d.isCompany) {
        mustShow = mode !== "none";
        rank = 3;
      } else if (d.type === "Filing") {
        rank = 4;
      } else if (d.degree >= 5 || this.cited.has(d.id)) {
        rank = 5;
      }

      if (!mustShow) {
        if (mode === "none") { d.labelVisible = false; continue; }

        // At low zoom a label nobody can read is just noise.
        if (mode === "smart") {
          if (d.type === "Filing" && k < 0.55) { d.labelVisible = false; continue; }
          if (rank >= 5 && k < 0.75) { d.labelVisible = false; continue; }
          if (rank >= 6 && k < 1.0) { d.labelVisible = false; continue; }
        }

        if (rank >= (answering ? 3 : 6)) {
          if (tailBudget <= 0) { d.labelVisible = false; continue; }
          tailBudget--;
        }
      }

      candidates.push({ node: d, rank, mustShow });
    }

    candidates.sort((a, b) => a.rank - b.rank);

    const placedBoxes = [];

    for (const { node, mustShow } of candidates) {
      const text = labelText(node.name);
      const charWidth = node.isCompany ? 7.2 : LABEL_CHAR_W;
      const labelHeight = node.isCompany ? 14 : 12;
      const labelWidth = text.length * charWidth + LABEL_PAD;
      const r = node.r || 7;
      const nx = node.x || 0;
      const ny = node.y || 0;
      const dir = away.get(node.id) || { x: 0, y: -1 };
      const baseAngle = Math.atan2(dir.y, dir.x);

      let best = null;
      let minPenalty = Infinity;

      // A label that has to be shown searches harder: it gets a wider ring to
      // look at, so it can step back out of a crowded neighbourhood rather than
      // accept an overlap. Without this the answer subgraph -- the one place
      // every label matters -- is exactly where labels collide.
      const rings = mustShow
        ? [r + 11, r + 22, r + 34, r + 48, r + 64]
        : [r + 11, r + 22, r + 34];

      for (const dist of rings) {
        for (let i = 0; i < 12; i++) {
          const offset = (i % 2 === 0 ? 1 : -1) * Math.ceil(i / 2) * (Math.PI / 6);
          const angle = baseAngle + offset;
          const cosA = Math.cos(angle);
          const sinA = Math.sin(angle);
          const dx = cosA * dist;
          const dy = sinA * dist;

          let anchor = "middle";
          let boxX1 = nx + dx - labelWidth / 2;
          let boxX2 = nx + dx + labelWidth / 2;
          if (cosA > 0.38) {
            anchor = "start";
            boxX1 = nx + dx;
            boxX2 = nx + dx + labelWidth;
          } else if (cosA < -0.38) {
            anchor = "end";
            boxX1 = nx + dx - labelWidth;
            boxX2 = nx + dx;
          }

          let textY = dy + 3.5;
          let boxY1 = ny + dy - labelHeight * 0.62;
          let boxY2 = ny + dy + labelHeight * 0.38;
          if (sinA < -0.38) {
            textY = dy - 1;
            boxY1 = ny + dy - labelHeight;
            boxY2 = ny + dy;
          } else if (sinA > 0.38) {
            textY = dy + labelHeight * 0.85;
            boxY1 = ny + dy;
            boxY2 = ny + dy + labelHeight;
          }

          const box = { x1: boxX1, y1: boxY1, x2: boxX2, y2: boxY2 };
          let penalty = 0;

          // Neighbouring node circles.
          for (const other of nearby((boxX1 + boxX2) / 2, (boxY1 + boxY2) / 2)) {
            if (other.id === node.id) continue;
            const ox = other.x || 0;
            const oy = other.y || 0;
            const cx = Math.max(box.x1, Math.min(ox, box.x2));
            const cy = Math.max(box.y1, Math.min(oy, box.y2));
            const cdx = ox - cx;
            const cdy = oy - cy;
            const minClear = (other.r || 8) + 8;
            if (cdx * cdx + cdy * cdy < minClear * minClear) penalty += 7000;
          }

          // Labels already committed to a spot. Labels that must be shown keep a
          // larger gap than the default, so two of them never end up touching.
          const gap = mustShow ? 14 : 8;
          for (const placed of placedBoxes) {
            const overlap = !(
              box.x2 + gap < placed.x1 || box.x1 - gap > placed.x2 ||
              box.y2 + gap < placed.y1 || box.y1 - gap > placed.y2
            );
            if (overlap) penalty += 9000;
          }

          // Prefer the side with no edges, and prefer to stay close.
          penalty += (dist - (r + 11)) * 2;

          if (penalty < minPenalty) {
            minPenalty = penalty;
            best = { dx, dy: textY, anchor };
          }
        }
        if (minPenalty <= 0) break;
      }

      if (best && (minPenalty < 4000 || mustShow)) {
        node.labelX = best.dx;
        node.labelY = best.dy;
        node.labelAnchor = best.anchor;
        node.labelVisible = true;
        const lx = nx + best.dx;
        const ly = ny + best.dy;
        const w = labelWidth;
        placedBoxes.push({
          x1: lx - (best.anchor === "start" ? 0 : best.anchor === "end" ? w : w / 2),
          y1: ly - labelHeight,
          x2: lx + (best.anchor === "start" ? w : best.anchor === "end" ? 0 : w / 2),
          y2: ly + 2,
        });
      } else {
        node.labelVisible = false;
      }
    }

    this.layerNodes.selectAll("text.g-node-label")
      .attr("x", (d) => d.labelX ?? 0)
      .attr("y", (d) => d.labelY ?? ((d.r || 7) + 14))
      .attr("text-anchor", (d) => d.labelAnchor ?? "middle")
      .style("display", (d) => (d.labelVisible ? null : "none"));
  }

  #applyView() {
    this.root.setAttribute("transform", `translate(${this.view.x},${this.view.y}) scale(${this.view.k})`);
  }

  /* ── Search & Path Highlighting ────────────────────────────────────────── */

  /** Focus a matched entity and the branch it belongs to, dimming the rest. */
  highlightSearchPath(nodeOrId) {
    const id = typeof nodeOrId === "object" ? nodeOrId?.id : nodeOrId;
    const node = this.byId.get(String(id));
    if (!node) return false;

    this.searchPathTargetId = node.id;
    const pathNodes = new Set([node.id]);
    const pathEdges = new Set();

    if (node.type === "FinancialMetric" || node.type === "Segment" || node.type === "DisclosureEvent") {
      const parentLinks = this.links.filter(
        (l) => (l.source.id === node.id || l.target.id === node.id) &&
               ["REPORTS_METRIC", "DISCLOSES_EVENT", "DISAGGREGATED_BY"].includes(l.relation)
      );
      for (const fl of parentLinks) {
        pathEdges.add(EDGE_KEY(fl));
        const filingNode = fl.source.id === node.id ? fl.target : fl.source;
        pathNodes.add(filingNode.id);

        const compLinks = this.links.filter(
          (l) => (l.source.id === filingNode.id || l.target.id === filingNode.id) &&
                 ["SUBMITTED", "FILED"].includes(l.relation)
        );
        for (const cl of compLinks) {
          pathEdges.add(EDGE_KEY(cl));
          pathNodes.add(cl.source.id === filingNode.id ? cl.target.id : cl.source.id);
        }
      }
    } else if (node.type === "Filing" || node.type === "Document") {
      const compLinks = this.links.filter(
        (l) => (l.source.id === node.id || l.target.id === node.id) &&
               ["SUBMITTED", "FILED"].includes(l.relation)
      );
      for (const cl of compLinks) {
        pathEdges.add(EDGE_KEY(cl));
        pathNodes.add(cl.source.id === node.id ? cl.target.id : cl.source.id);
      }
      const childLinks = this.links.filter(
        (l) => l.source.id === node.id && ["REPORTS_METRIC", "DISCLOSES_EVENT"].includes(l.relation)
      );
      for (const ml of childLinks.slice(0, 15)) {
        pathEdges.add(EDGE_KEY(ml));
        pathNodes.add(ml.target.id);
      }
    } else if (node.isCompany) {
      for (const l of this.links) {
        if (l.source.clusterId === node.id && l.target.clusterId === node.id) {
          pathEdges.add(EDGE_KEY(l));
          pathNodes.add(l.source.id);
          pathNodes.add(l.target.id);
        }
      }
    }

    this.searchPathNodes = pathNodes;
    this.searchPathEdges = pathEdges;
    this.selected = node.id;

    const { width, height } = this.#size();
    const targetK = Math.max(0.75, Math.min(1.4, this.view.k));
    this.#setView(width / 2 - node.x * targetK, height / 2 - node.y * targetK, targetK);

    this.#showDetailPanel(node);
    this.#draw();
    return true;
  }

  clearSelection() {
    this.selected = null;
    this.searchPathNodes = null;
    this.searchPathEdges = null;
    this.searchPathTargetId = null;
    if (!this.answerPathNodes || this.answerPathNodes.size === 0) {
      this.cited = new Set();
      this.#stopFlow();
    }
    if (this.inspectorEl) this.inspectorEl.hidden = true;
    this.#draw();
    this.handlers.onSelect?.(null);
  }

  /* ── Selection & Details Panel ─────────────────────────────────────────── */

  selectNode(nodeOrId) {
    if (!nodeOrId) {
      this.selected = null;
      if (this.inspectorEl) this.inspectorEl.hidden = true;
      this.#draw();
      this.handlers.onSelect?.(null);
      return;
    }

    const id = typeof nodeOrId === "object" ? nodeOrId?.id : nodeOrId;
    const node = this.byId.get(String(id));
    if (!node) return;

    // While an answer is on screen, selecting a node inspects it without
    // disturbing the answer subgraph.
    if (this.answerPathNodes && this.answerPathNodes.size > 0) {
      this.selected = node.id;
      this.#showDetailPanel(node);
      this.#draw();
      this.handlers.onSelect?.(node);
      return;
    }

    this.highlightSearchPath(node);
    this.handlers.onSelect?.(node);
  }

  #showDetailPanel(node) {
    if (!this.inspectorEl) return;
    this.inspectorEl.hidden = false;

    const incident = this.allLinks.filter((l) => l.source.id === node.id || l.target.id === node.id);
    const company = node.companyName || node.companyId || (node.isCompany ? node.name : "—");

    let metricDetails = "";
    if (node.type === "FinancialMetric") {
      const metricEdge = incident.find((l) => l.relation === "REPORTS_METRIC");
      metricDetails = `
        <div class="inspector-prop"><span class="inspector-prop__key">Metric:</span> <span class="inspector-prop__val">${escapeHtml(node.name)}</span></div>
        ${metricEdge?.description ? `<div class="inspector-prop"><span class="inspector-prop__key">Reported:</span> <span class="inspector-prop__val">${escapeHtml(metricEdge.description)}</span></div>` : ""}
        ${node.sourceFiling ? `<div class="inspector-prop"><span class="inspector-prop__key">Document:</span> <span class="inspector-prop__val">${escapeHtml(node.sourceFiling)}</span></div>` : ""}
      `;
    }

    let filingDetails = "";
    if (node.type === "Filing" || node.type === "Document") {
      filingDetails = `
        <div class="inspector-prop"><span class="inspector-prop__key">Document:</span> <span class="inspector-prop__val">${escapeHtml(node.name)}</span></div>
        <div class="inspector-prop"><span class="inspector-prop__key">Details:</span> <span class="inspector-prop__val">${escapeHtml(node.description || "—")}</span></div>
      `;
    }

    const connectionItems = incident.slice(0, 15).map((l) => {
      const other = l.source.id === node.id ? l.target : l.source;
      const isOut = l.source.id === node.id;
      return `
        <li class="inspector-rel-item" data-id="${other.id}">
          <span class="inspector-rel-tag">${isOut ? "→" : "←"} ${escapeHtml(l.relation)}</span>
          <span class="inspector-rel-name">${escapeHtml(other.name)}</span>
        </li>
      `;
    }).join("");

    this.inspectorEl.innerHTML = `
      <div class="inspector-head">
        <div class="inspector-title-row">
          <span class="chip chip--dot" style="color: ${node.companyColor || typeColor(node.type)}">${escapeHtml(prettyType(node.type))}</span>
          <h3 class="inspector-title">${escapeHtml(node.name)}</h3>
          <button class="btn btn--ghost btn--icon inspector-close" id="inspector-close" title="Close details (Esc)">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
          </button>
        </div>
      </div>
      <div class="inspector-body">
        <div class="inspector-props">
          <div class="inspector-prop"><span class="inspector-prop__key">Entity ID:</span> <span class="inspector-prop__val font-mono">${escapeHtml(node.id)}</span></div>
          <div class="inspector-prop"><span class="inspector-prop__key">Company Anchor:</span> <span class="inspector-prop__val font-bold" style="color:${node.companyColor || "var(--primary)"}">${escapeHtml(company)}</span></div>
          ${metricDetails}
          ${filingDetails}
          <div class="inspector-prop"><span class="inspector-prop__key">Direct Relationships:</span> <span class="inspector-prop__val">${incident.length}</span></div>
        </div>

        <div class="inspector-section">
          <div class="inspector-section__head">Connected Path (${incident.length})</div>
          <ul class="inspector-rel-list">
            ${connectionItems || '<li class="empty-hint">No direct connections</li>'}
          </ul>
        </div>
      </div>
      <div class="inspector-foot">
        <button class="btn btn--ghost btn--sm" id="inspector-focus-btn">Center Entity</button>
        ${node.companyId ? `<button class="btn btn--ghost btn--sm" id="inspector-filter-co-btn">Isolate ${escapeHtml(node.companyId)}</button>` : ""}
      </div>
    `;

    this.inspectorEl.querySelector("#inspector-close")?.addEventListener("click", () => this.clearSelection());
    this.inspectorEl.querySelector("#inspector-focus-btn")?.addEventListener("click", () => this.focus(node.id));
    this.inspectorEl.querySelector("#inspector-filter-co-btn")?.addEventListener("click", () => {
      this.setCompanyFilter(node.companyId);
    });

    this.inspectorEl.querySelectorAll(".inspector-rel-item").forEach((el) => {
      el.addEventListener("click", () => {
        const targetId = el.getAttribute("data-id");
        if (targetId) this.selectNode(targetId);
      });
    });
  }

  /* ── Right-Side Control & Filter Panel ─────────────────────────────────── */

  #renderSidePanel() {
    if (!this.sidePanelEl) return;

    const companies = this.allNodes.filter((n) => n.isCompany);
    const companyOptions = [
      `<option value="ALL" ${this.companyFilter === "ALL" ? "selected" : ""}>All Companies</option>`,
      ...companies.map((c) => `<option value="${c.id}" ${this.companyFilter === c.id ? "selected" : ""}>${c.name} (${c.id})</option>`),
    ].join("");

    const typeCounts = new Map();
    for (const n of this.allNodes) {
      typeCounts.set(n.type, (typeCounts.get(n.type) || 0) + 1);
    }
    const typeCheckboxes = [...typeCounts.entries()].map(([type, count]) => {
      const key = String(type).toLowerCase();
      const isChecked = !this.hiddenTypes.has(key);
      return `
        <label class="side-panel-check">
          <input type="checkbox" data-type="${key}" ${isChecked ? "checked" : ""}>
          <span class="side-panel-dot" style="background:${this.allNodes.find((n) => n.type === type)?.companyColor || typeColor(type)}"></span>
          <span class="side-panel-label">${escapeHtml(prettyType(type))}</span>
          <span class="side-panel-count">${count}</span>
        </label>
      `;
    }).join("");

    const relCounts = new Map();
    for (const l of this.allLinks) {
      relCounts.set(l.relation, (relCounts.get(l.relation) || 0) + 1);
    }
    const relCheckboxes = [...relCounts.entries()].map(([rel, count]) => {
      const isChecked = !this.hiddenRelations.has(rel);
      return `
        <label class="side-panel-check">
          <input type="checkbox" data-rel="${rel}" ${isChecked ? "checked" : ""}>
          <span class="side-panel-label">${escapeHtml(rel)}</span>
          <span class="side-panel-count">${count}</span>
        </label>
      `;
    }).join("");

    const companySelect = this.sidePanelEl.querySelector("#side-company-select");
    if (companySelect) {
      companySelect.innerHTML = companyOptions;
      companySelect.value = this.companyFilter;
    }

    const typeChecksContainer = this.sidePanelEl.querySelector("#side-type-checks");
    if (typeChecksContainer) {
      typeChecksContainer.innerHTML = typeCheckboxes;
      typeChecksContainer.querySelectorAll("input[data-type]").forEach((cb) => {
        cb.addEventListener("change", (e) => {
          const type = e.target.getAttribute("data-type");
          if (e.target.checked) this.hiddenTypes.delete(type);
          else this.hiddenTypes.add(type);
          this.#filterAndApply();
        });
      });
    }

    const relChecksContainer = this.sidePanelEl.querySelector("#side-rel-checks");
    if (relChecksContainer) {
      relChecksContainer.innerHTML = relCheckboxes;
      relChecksContainer.querySelectorAll("input[data-rel]").forEach((cb) => {
        cb.addEventListener("change", (e) => {
          const rel = e.target.getAttribute("data-rel");
          if (e.target.checked) this.hiddenRelations.delete(rel);
          else this.hiddenRelations.add(rel);
          this.#filterAndApply();
        });
      });
    }

    const nodeSizeInput = this.sidePanelEl.querySelector("#side-node-size");
    if (nodeSizeInput) nodeSizeInput.value = this.nodeScale;
    const linkSizeInput = this.sidePanelEl.querySelector("#side-link-size");
    if (linkSizeInput) linkSizeInput.value = this.linkScale;
    const labelsModeSelect = this.sidePanelEl.querySelector("#side-labels-mode");
    if (labelsModeSelect) labelsModeSelect.value = this.labelsMode;
    const arrowsToggle = this.sidePanelEl.querySelector("#side-arrows-toggle");
    if (arrowsToggle) arrowsToggle.checked = this.showArrows;

    if (!this._sidePanelStaticWired) {
      this._sidePanelStaticWired = true;

      this.sidePanelEl.querySelector("#side-panel-collapse-btn")?.addEventListener("click", () => {
        this.closeSidePanel();
      });

      this.sidePanelEl.querySelector("#entity-search")?.addEventListener("input", (e) => {
        const q = e.target.value.toLowerCase().trim();
        if (!q) {
          this.clearSelection();
          return;
        }
        const match = this.nodes.find((n) => n.name.toLowerCase().includes(q) || n.id.toLowerCase().includes(q));
        if (match) this.highlightSearchPath(match);
      });

      this.sidePanelEl.querySelector("#side-company-select")?.addEventListener("change", (e) => {
        this.setCompanyFilter(e.target.value);
      });
      this.sidePanelEl.querySelector("#side-node-size")?.addEventListener("input", (e) => {
        this.setNodeScale(parseFloat(e.target.value));
      });
      this.sidePanelEl.querySelector("#side-link-size")?.addEventListener("input", (e) => {
        this.setLinkScale(parseFloat(e.target.value));
      });
      this.sidePanelEl.querySelector("#side-labels-mode")?.addEventListener("change", (e) => {
        this.setLabelsMode(e.target.value);
      });
      this.sidePanelEl.querySelector("#side-arrows-toggle")?.addEventListener("change", (e) => {
        this.setShowArrows(e.target.checked);
      });

      this.sidePanelEl.querySelector("#side-fit-btn")?.addEventListener("click", () => this.fit());
      this.sidePanelEl.querySelector("#side-relayout-btn")?.addEventListener("click", () => this.relayout());
      this.sidePanelEl.querySelector("#side-reset-btn")?.addEventListener("click", () => this.reset());
    }
  }

  /* ── External API & Mutators ───────────────────────────────────────────── */

  setCompanyFilter(company) {
    this.companyFilter = company || "ALL";
    this.searchPathNodes = null;
    this.searchPathEdges = null;
    this.#filterAndApply();
    this.#renderSidePanel();
    setTimeout(() => this.fit(), 350);
  }

  setNodeScale(scale) {
    this.nodeScale = Math.max(0.4, Math.min(2.5, scale));
    for (const n of this.allNodes) {
      const extra = Math.min(4, Math.sqrt(n.degree) * 0.9);
      n.r = (n.baseRadius + extra) * this.nodeScale;
    }
    this.#draw();
  }

  setLinkScale(scale) {
    this.linkScale = Math.max(0.3, Math.min(3, scale));
    this.#draw();
  }

  setLabelsMode(mode) {
    this.labelsMode = mode;
    this.#computeLabelPlacements();
  }

  setShowArrows(show) {
    this.showArrows = !!show;
    this.#draw();
  }

  setHiddenTypes(types) {
    this.hiddenTypes = new Set(types || []);
    this.#filterAndApply();
    this.#renderSidePanel();
  }

  setMaxHops(hops) {
    this.maxHops = Number(hops) || 3;
    this.#computeHopDistances();
    this.#draw();
  }

  setLabels(on) {
    this.setLabelsMode(on ? "smart" : "none");
  }

  setCited(ids) {
    const next = new Set((ids || []).map(String).filter((id) => this.byId.has(id)));
    this.#flash(next);
    this.cited = next;
    this.#computeHopDistances();
    this.#draw();

    this.#startFlow(this.links.filter((l) => this.cited.has(l.source.id) && this.cited.has(l.target.id)));
  }

  get citationSpread() {
    if (this.answerPathNodes && this.answerPathNodes.size > 0) {
      return { nodes: this.answerPathNodes.size, edges: this.answerPathEdges?.size || 0 };
    }
    let edges = 0;
    for (const link of this.links) {
      if (this.cited.has(link.source.id) && this.cited.has(link.target.id)) edges += 1;
    }
    return { nodes: this.cited.size, edges };
  }

  focus(id) {
    const node = this.byId.get(String(id));
    if (!node) return false;

    if (this.answerPathNodes && this.answerPathNodes.size > 0) {
      this.selected = node.id;
      const { width, height } = this.#size();
      const targetK = Math.max(0.75, Math.min(1.4, this.view.k));
      this.#setView(width / 2 - (node.x || 0) * targetK, height / 2 - (node.y || 0) * targetK, targetK);
      this.#showDetailPanel(node);
      this.#draw();
      this.handlers.onSelect?.(node);
      return true;
    }

    return this.highlightSearchPath(node);
  }

  fit() {
    this.fitted = true;
    const { width, height } = this.#size();
    if (!this.nodes.length) {
      this.#setView(width / 2, height / 2, 1);
      return;
    }

    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const node of this.nodes) {
      if (!Number.isFinite(node.x) || !Number.isFinite(node.y)) continue;
      const r = (node.r || 8) + 30;
      minX = Math.min(minX, node.x - r); maxX = Math.max(maxX, node.x + r);
      minY = Math.min(minY, node.y - r); maxY = Math.max(maxY, node.y + r);
    }
    if (!Number.isFinite(minX)) {
      this.#setView(width / 2, height / 2, 1);
      return;
    }

    const spanX = Math.max(1, maxX - minX);
    const spanY = Math.max(1, maxY - minY);
    const k = Math.max(0.12, Math.min(1.3, 0.92 * Math.min(width / spanX, height / spanY)));

    this.#setView(
      width / 2 - ((minX + maxX) / 2) * k,
      height / 2 - ((minY + maxY) / 2) * k,
      k,
    );
  }

  zoomBy(factor) {
    const { width, height } = this.#size();
    const k = Math.max(0.08, Math.min(4, this.view.k * factor));
    this.#setView(
      width / 2 - (width / 2 - this.view.x) * (k / this.view.k),
      height / 2 - (height / 2 - this.view.y) * (k / this.view.k),
      k,
    );
  }

  relayout() {
    for (const n of this.nodes) {
      n.x = null;
      n.y = null;
      n.vx = 0;
      n.vy = 0;
    }
    this.#seedClusters();
    this.#buildSimulation();
    setTimeout(() => this.fit(), 500);
  }

  reset() {
    this.companyFilter = "ALL";
    this.hiddenTypes.clear();
    this.hiddenRelations.clear();
    this.nodeScale = 1.0;
    this.linkScale = 1.0;
    this.labelsMode = "smart";
    this.showArrows = true;
    this.clearSelection();
    this.relayout();
    this.#renderSidePanel();
  }

  /* ── Geometry & Edge Math ──────────────────────────────────────────────── */

  #edgeGeometry(link) {
    const s = link.source;
    const t = link.target;
    const dx = t.x - s.x;
    const dy = t.y - s.y;
    const bow = BEND * (link.curve ?? 0.8);
    const cx = (s.x + t.x) / 2 - dy * bow;
    const cy = (s.y + t.y) / 2 + dx * bow;

    const out = 2 * Math.hypot(cx - s.x, cy - s.y) || 1;
    const into = 2 * Math.hypot(t.x - cx, t.y - cy) || 1;

    return {
      cx, cy,
      u0: Math.min(0.45, ((s.r || 8) + 4) / out),
      u1: 1 - Math.min(0.45, ((t.r || 8) + 5) / into),
    };
  }

  #edgePoint(link, u) {
    const s = link.source;
    const t = link.target;
    const { cx, cy } = this.#edgeGeometry(link);
    const m = 1 - u;
    return {
      x: m * m * s.x + 2 * m * u * cx + u * u * t.x,
      y: m * m * s.y + 2 * m * u * cy + u * u * t.y,
    };
  }

  #edgePath(link) {
    const s = link.source;
    const t = link.target;
    if (!s || !t) return "";
    const { cx, cy, u0, u1 } = this.#edgeGeometry(link);
    const at = (u) => {
      const m = 1 - u;
      return {
        x: m * m * s.x + 2 * m * u * cx + u * u * t.x,
        y: m * m * s.y + 2 * m * u * cy + u * u * t.y,
      };
    };
    const a = at(u0);
    const b = at(u1);
    const f = (n) => Math.round(n * 10) / 10;
    return `M${f(a.x)},${f(a.y)} Q${f(cx)},${f(cy)} ${f(b.x)},${f(b.y)}`;
  }

  /* ── Flow & Flash Animations ───────────────────────────────────────────── */

  #flash(next) {
    const fresh = new Set([...next].filter((id) => !this.cited.has(id)));
    this.fresh = fresh;
    clearTimeout(this.flashTimer);
    if (!fresh.size) return;
    this.flashTimer = setTimeout(() => {
      if (this.disposed) return;
      this.fresh = new Set();
      this.#draw();
    }, FLASH_MS);
  }

  #startFlow(links) {
    this.#stopFlow();
    if (!links.length) return;
    this.flowLinks = links;
    this.#drawFlow();
    // Bounded on purpose: a flourish for a couple of seconds, not a loop that
    // runs for the life of the page.
    this.flowTimer = setTimeout(() => {
      if (this.disposed) return;
      this.#stopFlow();
    }, FLOW_MS);
  }

  #drawFlow() {
    const links = this.flowLinks;
    if (!links || !links.length) return;
    this.layerEdgeFlow
      .selectAll("path.g-edge-flow")
      .data(links, EDGE_KEY)
      .join(
        (enter) => enter.append("path").attr("class", "g-edge-flow"),
        (update) => update,
        (exit) => exit.remove(),
      )
      .attr("d", (l) => this.#edgePath(l))
      .attr("stroke", (l) => l.source.companyColor || "currentColor")
      .style("--flow-delay", (_, i) => `${(i % 6) * 90}ms`);
  }

  #stopFlow() {
    clearTimeout(this.flowTimer);
    this.flowTimer = 0;
    this.flowLinks = null;
    if (!this.disposed) this.layerEdgeFlow.selectAll("path.g-edge-flow").remove();
  }

  /* ── Tooltip ───────────────────────────────────────────────────────────── */

  #showTooltip(event, node) {
    const host = this.handlers.tooltipHost || this.canvas.parentElement;
    const tip = this.handlers.tooltip;
    if (!host || !tip) return;

    const relations = this.allLinks
      .filter((l) => l.source.id === node.id || l.target.id === node.id)
      .slice(0, 5)
      .map((l) => {
        const other = l.source.id === node.id ? l.target : l.source;
        return `<div>${l.source.id === node.id ? "→" : "←"} ${l.relation}: ${escapeHtml(other.name)}</div>`;
      })
      .join("");

    tip.innerHTML = `
      <div class="graph-tooltip__name">${escapeHtml(node.name)}</div>
      <div class="graph-tooltip__type" style="color: ${node.companyColor || typeColor(node.type)}">${escapeHtml(prettyType(node.type))}</div>
      ${node.companyName ? `<div class="graph-tooltip__desc">Company Anchor: ${escapeHtml(node.companyName)}</div>` : ""}
      ${node.description ? `<div class="graph-tooltip__desc">${escapeHtml(node.description)}</div>` : ""}
      ${relations ? `<div class="graph-tooltip__rel">${relations}</div>` : ""}
    `;

    const bounds = host.getBoundingClientRect();
    tip.classList.add("is-visible");
    const width = tip.offsetWidth;
    const height = tip.offsetHeight;
    let x = event.clientX - bounds.left + 16;
    let y = event.clientY - bounds.top + 16;
    if (x + width > bounds.width) x = event.clientX - bounds.left - width - 16;
    if (y + height > bounds.height) y = bounds.height - height - 10;
    tip.style.left = `${Math.max(6, x)}px`;
    tip.style.top = `${Math.max(6, y)}px`;
  }

  #hideTooltip() {
    this.handlers.tooltip?.classList.remove("is-visible");
  }

  #setView(x, y, k) {
    const clamped = Math.max(0.08, Math.min(4, k));
    this.view = { x, y, k: clamped };
    d3.select(this.canvas).call(
      this.zoom.transform,
      d3.zoomIdentity.translate(x, y).scale(clamped)
    );
  }

  #size() {
    const rect = this.canvas.getBoundingClientRect();
    return {
      width: rect.width || 800,
      height: rect.height || 600,
    };
  }

  #onResize() {
    if (this.disposed) return;
    if (this.nodes.length && !this.answerPathNodes?.size) {
      this.fit();
    } else {
      this.#draw();
    }
  }

  destroy() {
    this.disposed = true;
    this.simulation?.stop();
    if (this.frame) cancelAnimationFrame(this.frame);
    clearTimeout(this.flashTimer);
    clearTimeout(this.flowTimer);
    this.observer?.disconnect();
    this.root?.remove();
  }
}