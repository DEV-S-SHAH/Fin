/* The graph canvas.
 *
 * Full D3.js Knowledge Graph visualization adhering to FinGraph design system:
 * - Completely separate, clearly spaced force-directed company clusters (Apple, Microsoft, Nvidia, Tesla)
 * - Strict hierarchy: Company (Anchor) → Documents (1st-level) → Metrics/Segments (deeper branches)
 * - Pure existing backend graph data with zero cross-company mixing
 * - Fluid dragging with live link updates (dragging one branch does not scatter other company clusters)
 * - Type-aware sizing (#FF3C00 Company 22px, #FF5A1F Documents 13px, #F59E0B Metrics 7.5px)
 * - Search path highlighting (focuses matching entity + provenance path, dims unrelated clusters)
 * - In-place selection with connected branch highlight and compact metadata inspector
 * - Smooth D3 zoom, pan, drag, fit-to-view, and reset
 */

import { svgEl, typeColor, prettyType, escapeHtml, $ } from "./util.js";

export const FLASH_MS = 1500;
export const FLOW_MS = 2500;

const EDGE_KEY = (l) => `${l.source?.id || l.source}|${l.target?.id || l.target}|${l.relation || ""}`;
const BEND = 0.06;

function getNodeBaseRadius(type) {
  const t = String(type || "").toLowerCase();
  if (t === "company") return 22;
  if (t === "filing" || t === "document") return 13;
  if (t === "segment") return 9.5;
  if (t === "financialmetric") return 7.5;
  if (t === "fiscalyear" || t === "fiscalquarter") return 8.5;
  if (t === "event" || t === "disclosureevent") return 8;
  return 7.5;
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

    // Master graph data
    this.allNodes = [];
    this.allLinks = [];
    this.byId = new Map();
    this.companyClusters = new Map(); // ticker -> { x, y, name, nodes: Set }

    // Active displayed data
    this.nodes = [];
    this.links = [];

    // Interaction & Highlighting states
    this.seeds = new Set();
    this.cited = new Set();
    this.fresh = new Set();
    this.selected = null;
    this.hovered = null;
    this.searchPathNodes = null; // Set of node IDs on active search path
    this.searchPathEdges = null; // Set of edge keys on active search path
    this.answerPathNodes = null; // Set of node IDs on active answer/provenance path
    this.answerPathEdges = null; // Set of edge keys on active answer/provenance path
    this.answerCompanyId = null; // Ticker of active answer company
    this.hopCache = new Map();
    this.maxHops = 3;

    // Filters & Display options
    this.companyFilter = "ALL";
    this.hiddenTypes = new Set();
    this.hiddenRelations = new Set();
    this.nodeScale = 1.0;
    this.linkScale = 1.0;
    this.labelsMode = "smart"; // "smart" | "all" | "none"
    this.showArrows = true;

    // Viewport & Simulation
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
    this.layerEdgeGlow = d3.select(svgEl("g", { class: "g-edge-glows" }));
    this.layerEdges = d3.select(svgEl("g", { class: "g-edges" }));
    this.layerEdgeFlow = d3.select(svgEl("g", { class: "g-edge-flows" }));
    this.layerEdgeLabels = d3.select(svgEl("g", { class: "g-edge-labels" }));
    this.layerNodes = d3.select(svgEl("g", { class: "g-nodes" }));

    this.root.append(
      this.layerEdgeGlow.node(),
      this.layerEdges.node(),
      this.layerEdgeFlow.node(),
      this.layerEdgeLabels.node(),
      this.layerNodes.node(),
    );
    this.canvas.append(this.root);

    const defs = svgEl("defs");

    // Crisp directional arrow markers
    const markers = [
      { id: "arrow", color: "var(--border-strong, #3a3f4d)", opacity: 0.6 },
      { id: "arrow-highlight", color: "var(--primary, #FF3C00)", opacity: 0.95 },
      { id: "arrow-cited", color: "var(--primary, #FF3C00)", opacity: 0.95 },
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

    // Bloom filter
    const bloom = svgEl("filter", {
      id: "g-bloom",
      x: "-100%", y: "-100%", width: "300%", height: "300%",
      filterUnits: "objectBoundingBox",
    });
    bloom.append(svgEl("feGaussianBlur", { stdDeviation: "3.5", result: "blur" }));
    const merge = svgEl("feMerge");
    merge.append(svgEl("feMergeNode", { in: "blur" }));
    merge.append(svgEl("feMergeNode", { in: "SourceGraphic" }));
    bloom.append(merge);
    defs.append(bloom);

    this.canvas.insertBefore(defs, this.root);
  }

  /* ── Interactive Behaviors ─────────────────────────────────────────────── */

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

    // Node Dragging: naturally pulls connected branch through force simulation
    this.drag = d3.drag()
      .on("start", (event, d) => {
        this.nodeDragging = true;
        this.suppressClick = false;
        this.dragTravel = 0;
        this.dragOrigin = { x: event.x, y: event.y };
        d.fx = d.x;
        d.fy = d.y;
        if (!event.active && this.simulation) {
          this.simulation.alphaTarget(0.28).restart();
        }
        this.canvas.classList.add("is-panning");
        this.#hideTooltip();
        this.#run();
      })
      .on("drag", (event, d) => {
        d.fx = event.x;
        d.fy = event.y;
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

    // Persistent Reopen Filter FAB button
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
    const prevPositions = new Map(this.allNodes.map((n) => [n.id, { x: n.x, y: n.y, vx: n.vx, vy: n.vy }]));

    this.allNodes = rawNodes
      .filter((n) => n && n.id !== undefined && n.id !== null)
      .map((n) => {
        const type = n.entity_type || n.type || "Unspecified";
        const name = n.name || n.id;
        const prev = prevPositions.get(n.id);
        const baseRadius = getNodeBaseRadius(type);

        return {
          ...n,
          id: String(n.id),
          type,
          name,
          baseRadius,
          r: baseRadius,
          x: prev?.x ?? null,
          y: prev?.y ?? null,
          vx: prev?.vx ?? 0,
          vy: prev?.vy ?? 0,
          degree: 0,
          clusterId: null, // Company ticker this node strictly belongs to
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

    // Radius with degree influence
    for (const n of this.allNodes) {
      const extra = Math.min(5, Math.sqrt(n.degree) * 1.2);
      n.r = (n.baseRadius + extra) * this.nodeScale;
    }

    // Parallel edge bows
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

    // Strict Company cluster partitioning (NEVER mix company data)
    this.#partitionCompanyClusters();

    // Seeds and Citations
    this.seeds = new Set((payload?.seeds || []).map(String).filter((id) => this.byId.has(id)));
    const nextCited = new Set(cited.map(String).filter((id) => this.byId.has(id)));
    this.#flash(nextCited);
    this.cited = nextCited;

    this.#computeHopDistances();
    this.#filterAndApply();

    if (fresh || !this.fitted) {
      setTimeout(() => this.fit(), 400);
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
        const name = n.name || idStr;
        const baseRadius = getNodeBaseRadius(type);
        const nodeObj = {
          ...n,
          id: idStr,
          type,
          name,
          baseRadius,
          r: baseRadius * this.nodeScale,
          x: null,
          y: null,
          vx: 0,
          vy: 0,
          degree: 0,
          clusterId: null,
        };
        this.allNodes.push(nodeObj);
        this.byId.set(idStr, nodeObj);
        added = true;
      }
    }

    const existingEdgeKeys = new Set(this.allLinks.map(EDGE_KEY));
    for (const e of (payload.edges || [])) {
      const sId = String(e.source?.id || e.source);
      const tId = String(e.target?.id || e.target);
      if (this.byId.has(sId) && this.byId.has(tId)) {
        const key = `${sId}|${tId}|${e.relation || ""}`;
        if (!existingEdgeKeys.has(key)) {
          const sNode = this.byId.get(sId);
          const tNode = this.byId.get(tId);
          const linkObj = {
            ...e,
            source: sNode,
            target: tNode,
            relation: e.relation || "RELATED_TO",
            description: e.description || "",
            curve: 0,
          };
          this.allLinks.push(linkObj);
          existingEdgeKeys.add(key);
          sNode.degree += 1;
          tNode.degree += 1;
          added = true;
        }
      }
    }

    if (added) {
      this.#partitionCompanyClusters();
      this.#filterAndApply();
    }
  }

  /**
   * Highlight ONLY the nodes and edges that belong to the retrieved answer/provenance path
   * in FinGraph red/orange, while keeping all unrelated graph data visually de-emphasized
   * in muted dark grey.
   * Path: Company → Filing → Financial Metric → Evidence/Document Chunk.
   */
  highlightAnswer(result) {
    if (!result) return;

    // If master graph has no nodes yet, initialize from result.graph
    if (this.allNodes.length === 0 && result.graph) {
      this.setData(result.graph);
    } else if (result.graph) {
      this.#mergeSubgraph(result.graph);
    }

    // 1. Identify cited entities from used_tags, tag_map, provenance, and seeds
    const usedTags = result.used_tags || [];
    const tagMap = result.tag_map || {};
    const citedIds = new Set();

    for (const tag of usedTags) {
      const id = tagMap[tag];
      if (id && this.byId.has(String(id))) citedIds.add(String(id));
    }

    if (Array.isArray(result.provenance)) {
      for (const p of result.provenance) {
        if (Array.isArray(p.cites)) {
          for (const tag of p.cites) {
            const id = tagMap[tag] || tag;
            if (id && this.byId.has(String(id))) citedIds.add(String(id));
          }
        }
      }
    }

    if (Array.isArray(result.graph?.seeds)) {
      for (const sid of result.graph.seeds) {
        if (this.byId.has(String(sid))) citedIds.add(String(sid));
      }
    }

    // Fallback: If no tags were cited, use result.graph.nodes
    if (citedIds.size === 0 && result.graph?.nodes?.length) {
      for (const n of result.graph.nodes) {
        if (this.byId.has(String(n.id))) citedIds.add(String(n.id));
      }
    }

    // Target ticker/company from query router
    const targetTicker = result.ticker || null;
    let targetCompanyNode = targetTicker ? this.byId.get(String(targetTicker)) : null;
    if (!targetCompanyNode && targetTicker) {
      targetCompanyNode = this.allNodes.find(
        (n) => n.type === "Company" && n.id.toUpperCase() === targetTicker.toUpperCase()
      );
    }

    // 2. Trace complete relevant path strictly along real backend relationships:
    // Company → Filing → Financial Metric → Evidence/Document Chunk / Segment
    const pathNodes = new Set();
    const pathEdges = new Set();

    if (targetCompanyNode) {
      pathNodes.add(targetCompanyNode.id);
    }

    for (const id of citedIds) {
      const node = this.byId.get(id);
      if (!node) continue;
      pathNodes.add(node.id);

      if (node.type === "Company") continue;

      // Find real incident links
      const incident = this.allLinks.filter((l) => l.source.id === node.id || l.target.id === node.id);

      // Path from Segment -> FinancialMetric
      if (node.type === "Segment") {
        for (const l of incident) {
          if (l.relation === "DISAGGREGATED_BY") {
            const metric = l.source.id === node.id ? l.target : l.source;
            pathEdges.add(EDGE_KEY(l));
            pathNodes.add(metric.id);
            // From metric to filing
            const metricIncident = this.allLinks.filter(
              (ml) => ml.source.id === metric.id || ml.target.id === metric.id
            );
            for (const ml of metricIncident) {
              if (
                ml.relation === "REPORTS_METRIC" ||
                ml.target.type === "Filing" ||
                ml.source.type === "Filing" ||
                ml.target.type === "Document" ||
                ml.source.type === "Document"
              ) {
                const filing = ml.source.id === metric.id ? ml.target : ml.source;
                pathEdges.add(EDGE_KEY(ml));
                pathNodes.add(filing.id);
                // Filing to company
                const filingIncident = this.allLinks.filter(
                  (fl) => fl.source.id === filing.id || fl.target.id === filing.id
                );
                for (const fl of filingIncident) {
                  if (
                    fl.relation === "SUBMITTED" ||
                    fl.relation === "FILED" ||
                    fl.target.type === "Company" ||
                    fl.source.type === "Company"
                  ) {
                    const comp = fl.source.id === filing.id ? fl.target : fl.source;
                    pathEdges.add(EDGE_KEY(fl));
                    pathNodes.add(comp.id);
                  }
                }
              }
            }
          }
        }
      }

      // Path from FinancialMetric / DocumentChunk / DisclosureEvent -> Filing
      if (
        node.type === "FinancialMetric" ||
        node.type === "DocumentChunk" ||
        node.type === "DisclosureEvent"
      ) {
        for (const l of incident) {
          const other = l.source.id === node.id ? l.target : l.source;
          const isFilingRel = (
            l.relation === "REPORTS_METRIC" ||
            l.relation === "CONTAINS_CHUNK" ||
            l.relation === "DISCLOSES_EVENT" ||
            other.type === "Filing" ||
            other.type === "Document"
          );
          if (isFilingRel) {
            pathEdges.add(EDGE_KEY(l));
            pathNodes.add(other.id);

            // From filing to company
            const filingIncident = this.allLinks.filter(
              (fl) => fl.source.id === other.id || fl.target.id === other.id
            );
            for (const fl of filingIncident) {
              const comp = fl.source.id === other.id ? fl.target : fl.source;
              if (
                fl.relation === "SUBMITTED" ||
                fl.relation === "FILED" ||
                comp.type === "Company"
              ) {
                pathEdges.add(EDGE_KEY(fl));
                pathNodes.add(comp.id);
              }
            }
          }
        }
      }

      // Path from Filing -> Company
      if (node.type === "Filing" || node.type === "Document") {
        for (const l of incident) {
          const other = l.source.id === node.id ? l.target : l.source;
          if (
            l.relation === "SUBMITTED" ||
            l.relation === "FILED" ||
            other.type === "Company"
          ) {
            pathEdges.add(EDGE_KEY(l));
            pathNodes.add(other.id);
          }
        }
      }
    }

    // Also include any relationships between nodes in the answer path from result.graph.edges
    if (result.graph?.edges) {
      for (const re of result.graph.edges) {
        const sId = String(re.source?.id || re.source);
        const tId = String(re.target?.id || re.target);
        if (pathNodes.has(sId) && pathNodes.has(tId)) {
          for (const l of this.allLinks) {
            if (
              (l.source.id === sId && l.target.id === tId) ||
              (l.source.id === tId && l.target.id === sId)
            ) {
              pathEdges.add(EDGE_KEY(l));
            }
          }
        }
      }
    }

    // If company was targeted but has no edges in path yet, link it to filings on the path
    if (targetCompanyNode) {
      for (const nId of pathNodes) {
        if (nId === targetCompanyNode.id) continue;
        const n = this.byId.get(nId);
        if (n && (n.type === "Filing" || n.type === "Document")) {
          for (const l of this.allLinks) {
            if (
              (l.source.id === targetCompanyNode.id && l.target.id === nId) ||
              (l.source.id === nId && l.target.id === targetCompanyNode.id)
            ) {
              pathEdges.add(EDGE_KEY(l));
            }
          }
        }
      }
    }

    this.answerPathNodes = pathNodes;
    this.answerPathEdges = pathEdges;
    this.cited = citedIds;
    this.answerCompanyId = targetTicker;

    // Clear search path if active
    this.searchPathNodes = null;
    this.searchPathEdges = null;

    // Recalculate Force Simulation so the relevant answer path settles with clear spacing
    this.simulation?.alpha(0.45).restart();
    this.#run();

    this.#draw();
    this.#startFlow(this.links.filter((l) => this.answerPathEdges.has(EDGE_KEY(l))));

    // Smoothly focus & frame the answer path
    setTimeout(() => {
      this.#frameAnswerPath();
    }, 200);
  }

  /**
   * Smoothly frame the active answer path within the viewport.
   */
  #frameAnswerPath() {
    if (!this.answerPathNodes || this.answerPathNodes.size === 0) return;
    const { width, height } = this.#size();
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    let count = 0;

    for (const id of this.answerPathNodes) {
      const n = this.byId.get(id);
      if (!n || n.x === null || isNaN(n.x)) continue;
      const r = (n.r || 8) + 35;
      minX = Math.min(minX, n.x - r); maxX = Math.max(maxX, n.x + r);
      minY = Math.min(minY, n.y - r); maxY = Math.max(maxY, n.y + r);
      count++;
    }

    if (count === 0) return;

    const spanX = Math.max(240, maxX - minX + 180);
    const spanY = Math.max(240, maxY - minY + 180);
    const targetK = Math.max(0.45, Math.min(1.2, 0.88 * Math.min(width / spanX, height / spanY)));
    const targetX = width / 2 - ((minX + maxX) / 2) * targetK;
    const targetY = height / 2 - ((minY + maxY) / 2) * targetK;

    d3.select(this.canvas)
      .transition()
      .duration(750)
      .ease(d3.easeCubicOut)
      .call(
        this.zoom.transform,
        d3.zoomIdentity.translate(targetX, targetY).scale(targetK)
      );
  }

  /**
   * Clear answer highlight and return to default state.
   */
  clearAnswerHighlight() {
    this.answerPathNodes = null;
    this.answerPathEdges = null;
    this.answerCompanyId = null;
    this.cited.clear();
    this.selected = null;
    this.searchPathNodes = null;
    this.searchPathEdges = null;
    this.searchPathTargetId = null;
    if (this.inspectorEl) this.inspectorEl.hidden = true;
    this.#stopFlow();
    this.#draw();
    this.fit();
  }

  /**
   * Strictly partition each entity into its own company cluster.
   * Company (Anchor) → Documents (1st-level) → Metrics/Segments (deeper)
   */
  #partitionCompanyClusters() {
    const companies = this.allNodes.filter((n) => n.type === "Company");
    this.companyClusters.clear();

    for (const c of companies) {
      c.clusterId = c.id;
      c.companyId = c.id;
      c.ticker = c.id;
      this.companyClusters.set(c.id, {
        ticker: c.id,
        name: c.name,
        companyNode: c,
        nodes: new Set([c.id]),
      });
    }

    // Step 1: Assign Filings directly connected to Company via SUBMITTED / FILED
    for (const link of this.allLinks) {
      if (link.relation === "SUBMITTED" || link.relation === "FILED") {
        if (link.source.type === "Company") {
          link.target.clusterId = link.source.id;
          link.target.companyId = link.source.id;
          link.target.companyName = link.source.name;
          this.companyClusters.get(link.source.id)?.nodes.add(link.target.id);
        } else if (link.target.type === "Company") {
          link.source.clusterId = link.target.id;
          link.source.companyId = link.target.id;
          link.source.companyName = link.target.name;
          this.companyClusters.get(link.target.id)?.nodes.add(link.source.id);
        }
      }
    }

    // Step 2: Assign Metrics/Segments/Events connected to Filings
    for (const link of this.allLinks) {
      if (link.relation === "REPORTS_METRIC" || link.relation === "DISCLOSES_EVENT" || link.relation === "CONTAINS_CHUNK") {
        const filing = (link.source.type === "Filing" || link.source.type === "Document")
          ? link.source
          : ((link.target.type === "Filing" || link.target.type === "Document") ? link.target : null);
        const leaf = link.source === filing ? link.target : link.source;

        if (filing && leaf && filing.clusterId) {
          leaf.clusterId = filing.clusterId;
          leaf.companyId = filing.companyId;
          leaf.companyName = filing.companyName;
          leaf.sourceFiling = filing.name;
          this.companyClusters.get(filing.clusterId)?.nodes.add(leaf.id);
        }
      }
    }

    // Step 3: Segment -> Metric -> Filing -> Company
    for (const link of this.allLinks) {
      if (link.relation === "DISAGGREGATED_BY") {
        const metric = link.source.clusterId ? link.source : (link.target.clusterId ? link.target : null);
        const segment = link.source === metric ? link.target : link.source;
        if (metric && segment && metric.clusterId) {
          segment.clusterId = metric.clusterId;
          segment.companyId = metric.companyId;
          segment.companyName = metric.companyName;
          this.companyClusters.get(metric.clusterId)?.nodes.add(segment.id);
        }
      }
    }
  }

  /* ── Spatial Cluster Layout & Forces ───────────────────────────────────── */

  #filterAndApply() {
    this.nodes = this.allNodes.filter((n) => {
      if (this.hiddenTypes.has(String(n.type).toLowerCase())) return false;
      if (this.companyFilter !== "ALL") {
        if (n.clusterId && n.clusterId !== this.companyFilter) return false;
        if (n.type === "Company" && n.id !== this.companyFilter) return false;
      }
      return true;
    });

    const activeNodeIds = new Set(this.nodes.map((n) => n.id));

    this.links = this.allLinks.filter((l) => {
      if (!activeNodeIds.has(l.source.id) || !activeNodeIds.has(l.target.id)) return false;
      if (this.hiddenRelations.has(l.relation)) return false;
      return true;
    });

    this.#seedSeparateClusters();
    this.#buildSimulation();
    this.#draw();
    this.#run();
  }

  /**
   * Distribute company clusters into distinct, widely separated spatial zones.
   * Apple, Microsoft, Nvidia, Tesla each form their own galaxy with no collision.
   */
  #seedSeparateClusters() {
    const { width, height } = this.#size();
    const cx = width / 2;
    const cy = height / 2;

    const companies = this.nodes.filter((n) => n.type === "Company");

    // Wide spatial spacing between cluster centers to keep clusters strictly separated
    const spacingX = Math.max(480, width * 0.42);
    const spacingY = Math.max(360, height * 0.38);

    // 2x2 quadrant centers for companies (Apple, Microsoft, Nvidia, Tesla)
    const clusterPositions = [
      { x: cx - spacingX, y: cy - spacingY }, // Top-Left: AAPL
      { x: cx + spacingX, y: cy - spacingY }, // Top-Right: MSFT
      { x: cx - spacingX, y: cy + spacingY }, // Bottom-Left: NVDA
      { x: cx + spacingX, y: cy + spacingY }, // Bottom-Right: TSLA
    ];

    companies.forEach((co, idx) => {
      const pos = clusterPositions[idx % clusterPositions.length];
      co.clusterX = pos.x;
      co.clusterY = pos.y;
      if (co.x === null || co.y === null || isNaN(co.x)) {
        co.x = pos.x;
        co.y = pos.y;
      }
    });

    // Filings branch out from company anchor with organic jitter (no artificial starburst rings)
    const filings = this.nodes.filter((n) => n.type === "Filing" || n.type === "Document");
    filings.forEach((f, idx) => {
      const parentCo = this.byId.get(f.clusterId);
      const originX = parentCo?.clusterX ?? cx;
      const originY = parentCo?.clusterY ?? cy;
      f.clusterX = originX;
      f.clusterY = originY;

      if (f.x === null || f.y === null || isNaN(f.x)) {
        const jx = ((idx * 53) % 150) - 75;
        const jy = ((idx * 67) % 150) - 75;
        f.x = originX + jx;
        f.y = originY + jy;
      }
    });

    // Metrics/segments branch around parent filing with organic jitter
    this.nodes.forEach((n, idx) => {
      if (n.type !== "Company" && n.type !== "Filing" && n.type !== "Document") {
        const parentCo = this.byId.get(n.clusterId);
        const originX = parentCo?.clusterX ?? cx;
        const originY = parentCo?.clusterY ?? cy;
        n.clusterX = originX;
        n.clusterY = originY;

        if (n.x === null || n.y === null || isNaN(n.x)) {
          const jx = ((idx * 79) % 240) - 120;
          const jy = ((idx * 93) % 240) - 120;
          n.x = originX + jx;
          n.y = originY + jy;
        }
      }
    });
  }

  /* ── D3 Force Simulation (Multi-Cluster Independent Gravities) ─────────── */

  #buildSimulation() {
    this.simulation?.stop();

    this.simulation = d3.forceSimulation(this.nodes)
      // Gravitational pull toward its own company cluster center (keeps clusters strictly separated)
      .force(
        "clusterX",
        d3.forceX((d) => d.clusterX ?? (this.#size().width / 2))
          .strength((d) => (d.type === "Company" ? 0.30 : 0.08))
      )
      .force(
        "clusterY",
        d3.forceY((d) => d.clusterY ?? (this.#size().height / 2))
          .strength((d) => (d.type === "Company" ? 0.30 : 0.08))
      )
      // Generous Hierarchical Link Distances: Company -> Filing (145px), Filing -> Metric (88px)
      .force(
        "link",
        d3.forceLink(this.links)
          .id((d) => d.id)
          .distance((l) => {
            const isAnswerEdge = this.answerPathEdges?.has(EDGE_KEY(l));
            const r = l.relation;
            let dist = 95;
            if (r === "SUBMITTED" || r === "FILED") dist = 145;
            else if (r === "REPORTS_METRIC" || r === "CONTAINS_CHUNK") dist = 88;
            else if (r === "DISAGGREGATED_BY") dist = 78;
            return isAnswerEdge ? dist * 1.25 : dist;
          })
          .strength(0.55)
      )
      // Charge Repulsion: Company anchors repel strongly; leaves stay properly spaced
      .force(
        "charge",
        d3.forceManyBody()
          .strength((d) => {
            const isAnswerNode = this.answerPathNodes?.has(d.id);
            const boost = isAnswerNode ? 1.35 : 1.0;
            if (d.type === "Company") return -950 * boost;
            if (d.type === "Filing" || d.type === "Document") return -280 * boost;
            return -90 * boost;
          })
          .distanceMax(650)
      )
      // Strong collision avoidance to guarantee zero node overlap
      .force(
        "collide",
        d3.forceCollide()
          .radius((d) => {
            const isAnswerNode = this.answerPathNodes?.has(d.id);
            const pad = isAnswerNode ? 18 : 15;
            return (d.r || 8) + pad;
          })
          .iterations(4)
      )
      .alphaDecay(0.024);

    this.simulation.alpha(0.7);
  }

  #run() {
    if (this.frame) cancelAnimationFrame(this.frame);
    let tickCount = 0;
    const loop = () => {
      if (this.disposed) return;
      if (this.simulation) {
        this.simulation.tick();
        this.#updatePositions();
        tickCount++;
        if (tickCount % 6 === 0) {
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

  #updatePositions() {
    this.layerEdgeGlow.selectAll("path.g-edge-glow").attr("d", (l) => this.#edgePath(l));
    this.layerEdges.selectAll("path.g-edge").attr("d", (l) => this.#edgePath(l));
    this.layerEdgeFlow.selectAll("path.g-edge-flow").attr("d", (l) => this.#edgePath(l));
    this.layerEdgeLabels.selectAll("text.g-edge-label")
      .attr("x", (l) => this.#edgePoint(l, 0.5).x)
      .attr("y", (l) => this.#edgePoint(l, 0.5).y - 4);

    this.layerNodes.selectAll("g.g-node")
      .attr("transform", (d) => `translate(${d.x || 0},${d.y || 0})`);
  }

  /* ── Hop Computation for Answering & RAG Citations ─────────────────────── */

  #computeHopDistances() {
    if (!this.seeds.size && !this.cited.size) {
      this.hopCache.clear();
      return;
    }

    const queue = [];
    const distances = new Map();

    for (const id of this.seeds) {
      distances.set(id, 0);
      queue.push({ id, dist: 0 });
    }
    for (const id of this.cited) {
      if (!distances.has(id) || distances.get(id) > 0) {
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

  /* ── Draw & D3 Data Join ───────────────────────────────────────────────── */

  #draw() {
    this.#applyView();
    this.#drawFlow();

    // Active Highlight Set (Answer Path > Search Path > Selection > Hover)
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

    // 1. Edge Glows
    this.layerEdgeGlow.selectAll("path.g-edge-glow")
      .data(this.links, EDGE_KEY)
      .join(
        (enter) => enter.append("path").attr("class", "g-edge-glow"),
        (update) => update,
        (exit) => exit.remove(),
      )
      .attr("d", (l) => this.#edgePath(l))
      .attr("class", (l) => {
        const edgeKey = EDGE_KEY(l);
        const isAnswerEdge = this.answerPathEdges?.has(edgeKey);
        const isCited = this.cited.has(l.source.id) && this.cited.has(l.target.id);
        const isHot = hasFocus && highlightedEdges.has(edgeKey);
        return [
          "g-edge-glow",
          isAnswerEdge ? "is-answer-path is-hot" : "",
          isCited ? "is-cited" : "",
          isHot ? "is-hot" : "",
          hasFocus && !isHot ? "is-dim is-unrelated" : "",
        ].filter(Boolean).join(" ");
      });

    // 2. Edges: thin, low-opacity, highlighting connected path, dimming unrelated
    const edges = this.layerEdges.selectAll("path.g-edge")
      .data(this.links, EDGE_KEY)
      .join(
        (enter) => {
          const path = enter.append("path").attr("class", "g-edge");
          path.append("title");
          return path;
        },
        (update) => update,
        (exit) => exit.remove(),
      )
      .attr("d", (l) => this.#edgePath(l))
      .attr("class", (l) => {
        const edgeKey = EDGE_KEY(l);
        const isAnswerEdge = this.answerPathEdges?.has(edgeKey);
        const isCited = this.cited.has(l.source.id) && this.cited.has(l.target.id);
        const isHot = hasFocus ? highlightedEdges.has(edgeKey) : false;
        const isDim = hasFocus ? !isHot : (this.cited.size > 0 && !isCited);
        return [
          "g-edge",
          isAnswerEdge ? "is-answer-path is-hot" : "",
          isCited ? "is-cited" : "",
          isHot ? "is-hot" : "",
          isDim ? "is-dim is-unrelated" : "",
        ].filter(Boolean).join(" ");
      })
      .attr("marker-end", (l) => {
        if (!this.showArrows) return null;
        const edgeKey = EDGE_KEY(l);
        const isAnswerEdge = this.answerPathEdges?.has(edgeKey);
        const isHot = hasFocus && highlightedEdges.has(edgeKey);
        if (isAnswerEdge || isHot) return "url(#arrow-highlight)";
        if (hasFocus) return null;
        return "url(#arrow)";
      })
      .style("stroke-width", (l) => {
        const edgeKey = EDGE_KEY(l);
        const isAnswerEdge = this.answerPathEdges?.has(edgeKey);
        const isHot = hasFocus && highlightedEdges.has(edgeKey);
        if (isAnswerEdge || isHot) return `${2.2 * this.linkScale}px`;
        if (hasFocus) return `${0.5 * this.linkScale}px`;
        const isSubmitted = l.relation === "SUBMITTED" || l.relation === "FILED";
        const baseWidth = isSubmitted ? 0.9 : 0.65;
        return `${baseWidth * this.linkScale}px`;
      });

    edges.select("title").text((l) =>
      `${l.source.name} —[${l.relation}]→ ${l.target.name}${l.description ? `\n${l.description}` : ""}`
    );

    // 3. Edge Labels (rendered only when edges are long enough to avoid overlapping nodes)
    const showEdgeLabels = this.links.filter((l) => {
      if (!l.source || !l.target) return false;
      const dist = Math.hypot((l.target.x || 0) - (l.source.x || 0), (l.target.y || 0) - (l.source.y || 0));
      if (dist < (l.source.r || 8) + (l.target.r || 8) + 36) return false;
      if (hasFocus && highlightedEdges.has(EDGE_KEY(l))) return true;
      if (!hasFocus && this.view.k > 1.25) return true;
      return false;
    });

    this.layerEdgeLabels.selectAll("text.g-edge-label")
      .data(showEdgeLabels, EDGE_KEY)
      .join(
        (enter) => enter.append("text").attr("class", "g-edge-label").attr("text-anchor", "middle"),
        (update) => update,
        (exit) => exit.remove(),
      )
      .attr("x", (l) => this.#edgePoint(l, 0.5).x)
      .attr("y", (l) => this.#edgePoint(l, 0.5).y - 4)
      .text((l) => l.relation.replace(/_/g, " ").toLowerCase());

    // 4. Nodes: Company (Anchor), Filings (Branch), Metrics (Leaves)
    const groups = this.layerNodes.selectAll("g.g-node")
      .data(this.nodes, (d) => d.id)
      .join(
        (enter) => {
          const g = enter.append("g").attr("class", "g-node");
          g.append("circle").attr("class", "bloom");
          g.append("circle").attr("class", "halo");
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
              if (!this.suppressClick) {
                this.selectNode(d.id === this.selected ? null : d.id);
              }
            });
          return g;
        },
        (update) => update,
        (exit) => exit.remove(),
      )
      .attr("class", (d) => {
        const isAnswerNode = this.answerPathNodes?.has(d.id);
        const isSelected = this.selected === d.id;
        const isHovered = this.hovered === d.id;
        const isPathTarget = this.searchPathNodes ? (d.id === this.searchPathTargetId) : false;
        const isConnected = hasFocus ? highlightedNodes.has(d.id) : true;
        const isCited = this.cited.has(d.id);
        const isDim = hasFocus
          ? !isConnected
          : (this.cited.size > 0 && !isCited && (this.hopCache.get(d.id) ?? Infinity) > this.maxHops);

        return [
          "g-node",
          `type-${String(d.type).toLowerCase()}`,
          isAnswerNode ? "is-answer-path is-hot" : "",
          isSelected ? "is-selected is-hot" : "",
          isPathTarget ? "is-selected is-hot" : "",
          isHovered ? "is-hovered is-hot" : "",
          isCited ? "is-cited" : "",
          this.fresh.has(d.id) ? "is-fresh" : "",
          isDim ? "is-dim is-unrelated" : "",
          d.type === "Company" ? "is-root" : "",
        ].filter(Boolean).join(" ");
      })
      .attr("transform", (d) => `translate(${d.x || 0},${d.y || 0})`);

    groups.select("circle.bloom")
      .attr("r", (d) => (d.r || 8) + 8);

    groups.select("circle.halo")
      .attr("r", (d) => (d.r || 8) + 4);

    groups.select("circle.core")
      .attr("r", (d) => d.r || 8)
      .attr("fill", (d) => {
        const isConnected = hasFocus ? highlightedNodes.has(d.id) : true;
        if (this.selected === d.id && !isConnected) {
          return "#2b303e";
        }
        if (hasFocus && !isConnected) {
          return "#1d212c";
        }
        if (this.answerPathNodes?.has(d.id)) {
          if (d.type === "Company") return "#FF3C00";
          if (d.type === "Filing" || d.type === "Document") return "#FF5A1F";
          return typeColor(d.type);
        }
        return typeColor(d.type);
      })
      .attr("stroke", (d) => {
        const isConnected = hasFocus ? highlightedNodes.has(d.id) : true;
        if (this.selected === d.id) {
          return "#ffffff";
        }
        if (hasFocus && !isConnected) {
          return "#2b303e";
        }
        if (this.answerPathNodes?.has(d.id)) {
          return "#ffffff";
        }
        return "var(--graph-bg, #050505)";
      });

    groups.select("text.g-node-label")
      .text((d) => (d.name.length > 28 ? `${d.name.slice(0, 27)}…` : d.name));

    groups.select("title")
      .text((d) => `${d.name} (${prettyType(d.type)})${d.description ? ` — ${d.description}` : ""}`);

    this.#computeLabelPlacements();
  }

  /**
   * Dynamic label placement with D3-based collision avoidance.
   * Tests 16 candidate angular directions across multi-distance tiers around each node.
   * Checks collisions against:
   * 1. Neighboring node circles (ensuring minimum 6px clearance)
   * 2. Edge line segments (ensuring labels never cross or cover links)
   * 3. Already placed label bounding boxes (ensuring minimum 8px gap)
   * Followed by a D3 iterative relaxation pass to guarantee 0% residual overlap.
   */
  #computeLabelPlacements() {
    if (!this.nodes.length) return;

    const k = this.view.k;
    const mode = this.labelsMode;
    const activeId = this.hovered || this.selected;
    const pathNodes = this.searchPathNodes;

    // Fast lookup of incident neighbors and incident edge segments
    const incidentNeighbors = new Map();
    const incidentEdges = new Map();
    for (const l of this.links) {
      if (!l.source || !l.target) continue;
      const sid = String(l.source.id ?? l.source);
      const tid = String(l.target.id ?? l.target);
      const sNode = this.byId.get(sid);
      const tNode = this.byId.get(tid);
      if (!sNode || !tNode) continue;

      if (!incidentNeighbors.has(sid)) incidentNeighbors.set(sid, []);
      if (!incidentNeighbors.has(tid)) incidentNeighbors.set(tid, []);
      incidentNeighbors.get(sid).push(tNode);
      incidentNeighbors.get(tid).push(sNode);

      if (!incidentEdges.has(sid)) incidentEdges.set(sid, []);
      if (!incidentEdges.has(tid)) incidentEdges.set(tid, []);
      const edgeSeg = { x1: sNode.x || 0, y1: sNode.y || 0, x2: tNode.x || 0, y2: tNode.y || 0 };
      incidentEdges.get(sid).push(edgeSeg);
      incidentEdges.get(tid).push(edgeSeg);
    }

    // Geometry helpers for segment-box intersection
    const lineIntersectsLine = (x1, y1, x2, y2, x3, y3, x4, y4) => {
      const denom = (y4 - y3) * (x2 - x1) - (x4 - x3) * (y2 - y1);
      if (Math.abs(denom) < 1e-6) return false;
      const ua = ((x4 - x3) * (y1 - y3) - (y4 - y3) * (x1 - x3)) / denom;
      const ub = ((x2 - x1) * (y1 - y3) - (y2 - y1) * (x1 - x3)) / denom;
      return ua >= 0 && ua <= 1 && ub >= 0 && ub <= 1;
    };

    const lineIntersectsBox = (p1x, p1y, p2x, p2y, box) => {
      if (p1x >= box.x1 && p1x <= box.x2 && p1y >= box.y1 && p1y <= box.y2) return true;
      if (p2x >= box.x1 && p2x <= box.x2 && p2y >= box.y1 && p2y <= box.y2) return true;
      if (Math.max(p1x, p2x) < box.x1 || Math.min(p1x, p2x) > box.x2) return false;
      if (Math.max(p1y, p2y) < box.y1 || Math.min(p1y, p2y) > box.y2) return false;
      return (
        lineIntersectsLine(p1x, p1y, p2x, p2y, box.x1, box.y1, box.x2, box.y1) ||
        lineIntersectsLine(p1x, p1y, p2x, p2y, box.x2, box.y1, box.x2, box.y2) ||
        lineIntersectsLine(p1x, p1y, p2x, p2y, box.x2, box.y2, box.x1, box.y2) ||
        lineIntersectsLine(p1x, p1y, p2x, p2y, box.x1, box.y2, box.x1, box.y1)
      );
    };

    // Candidate ranking & density control
    const candidates = [];
    for (const d of this.nodes) {
      let mustShow = false;
      let rank = 6;

      const isAnswerNode = this.answerPathNodes?.has(d.id);

      if (this.answerPathNodes && this.answerPathNodes.size > 0) {
        // When answer is focused: show highlighted/relevant nodes or currently selected/hovered node
        if (d.id === activeId) {
          mustShow = true;
          rank = 1;
        } else if (isAnswerNode) {
          mustShow = true;
          rank = (d.type === "Company") ? 2 : 3;
        } else {
          // Hide unrelated labels completely to eliminate clutter!
          d.labelVisible = false;
          continue;
        }
      } else if (d.id === activeId) {
        mustShow = true;
        rank = 1;
      } else if (pathNodes?.has(d.id)) {
        mustShow = true;
        rank = 2;
      } else if (d.type === "Company") {
        mustShow = (mode !== "none");
        rank = 3;
      } else if (d.type === "Filing") {
        rank = 4;
      } else if (d.degree >= 5 || this.cited.has(d.id)) {
        rank = 5;
      }

      if (mode === "none" && !mustShow) {
        d.labelVisible = false;
        continue;
      }

      if (mode === "smart" && !mustShow) {
        if (d.type === "Filing" && k < 0.4) {
          d.labelVisible = false;
          continue;
        }
        if (rank >= 5 && k < 0.6) {
          d.labelVisible = false;
          continue;
        }
        if (rank >= 6 && k < 0.8) {
          d.labelVisible = false;
          continue;
        }
      }

      candidates.push({ node: d, rank, mustShow });
    }

    // Priority sorting: important / central nodes claim their optimal positions first
    candidates.sort((a, b) => a.rank - b.rank);

    const placedBoxes = []; // Array of { x1, y1, x2, y2, id }

    for (const { node, mustShow } of candidates) {
      const text = node.name.length > 28 ? `${node.name.slice(0, 27)}…` : node.name;
      const isCompany = node.type === "Company";
      const charWidth = isCompany ? 7.4 : 6.2;
      const labelHeight = isCompany ? 14 : 12;
      const labelWidth = text.length * charWidth + 8;
      const r = node.r || 8;

      const nx = node.x || 0;
      const ny = node.y || 0;

      // Incident edges average vector
      const neighbors = incidentNeighbors.get(node.id) || [];
      let avgEdgeDx = 0;
      let avgEdgeDy = 0;
      for (const nb of neighbors) {
        const dx = (nb.x ?? nx) - nx;
        const dy = (nb.y ?? ny) - ny;
        const dist = Math.hypot(dx, dy) || 1;
        avgEdgeDx += dx / dist;
        avgEdgeDy += dy / dist;
      }

      // Base direction pointing into the clearest open wedge
      const baseAngle = (neighbors.length > 0)
        ? Math.atan2(avgEdgeDy, avgEdgeDx) + Math.PI
        : 0;

      // Generate 16 candidate angles around node
      const angles = [];
      for (let i = 0; i < 16; i++) {
        const offset = (i % 2 === 0) ? (i / 2) * (Math.PI / 8) : -Math.ceil(i / 2) * (Math.PI / 8);
        angles.push(baseAngle + offset);
      }

      // Multi-distance tiers: Tier 1 (snug), Tier 2 (outer clearance), Tier 3 (extended)
      const distanceTiers = [
        r + 6,
        r + 15,
        r + 26,
      ];

      let bestCand = null;
      let minPenalty = Infinity;

      // Retrieve nearby edges for edge-crossing collision avoidance
      const localEdges = incidentEdges.get(node.id) || [];

      for (const dist of distanceTiers) {
        for (const angle of angles) {
          const cosA = Math.cos(angle);
          const sinA = Math.sin(angle);
          const dx = cosA * dist;
          const dy = sinA * dist;

          let anchor = "start";
          let boxX1 = nx + dx;
          let boxX2 = nx + dx + labelWidth;

          if (cosA > 0.38) {
            anchor = "start";
            boxX1 = nx + dx;
            boxX2 = nx + dx + labelWidth;
          } else if (cosA < -0.38) {
            anchor = "end";
            boxX1 = nx + dx - labelWidth;
            boxX2 = nx + dx;
          } else {
            anchor = "middle";
            boxX1 = nx + dx - labelWidth / 2;
            boxX2 = nx + dx + labelWidth / 2;
          }

          let textY = dy + 3.5;
          let boxY1 = ny + dy - labelHeight * 0.65;
          let boxY2 = ny + dy + labelHeight * 0.35;

          if (sinA < -0.38) {
            textY = dy - 2;
            boxY1 = ny + dy - labelHeight - 2;
            boxY2 = ny + dy - 2;
          } else if (sinA > 0.38) {
            textY = dy + labelHeight * 0.85;
            boxY1 = ny + dy;
            boxY2 = ny + dy + labelHeight;
          }

          const box = { x1: boxX1, y1: boxY1, x2: boxX2, y2: boxY2 };
          let penalty = 0;

          // 1. Collision avoidance with other node circles (minimum 6px clearance)
          for (const other of this.nodes) {
            if (other.id === node.id) continue;
            const ox = other.x || 0;
            const oy = other.y || 0;
            const dSq = (ox - nx) ** 2 + (oy - ny) ** 2;
            if (dSq > 36000) continue; // > 190px away

            const cx = Math.max(box.x1, Math.min(ox, box.x2));
            const cy = Math.max(box.y1, Math.min(oy, box.y2));
            const cdx = ox - cx;
            const cdy = oy - cy;
            const minClear = (other.r || 8) + 6;
            if (cdx * cdx + cdy * cdy < minClear * minClear) {
              penalty += 7000;
            }
          }

          // 2. Collision avoidance with already placed labels (minimum 8px gap)
          const labelGap = 8;
          for (const placed of placedBoxes) {
            const overlap = !(
              box.x2 + labelGap < placed.x1 ||
              box.x1 - labelGap > placed.x2 ||
              box.y2 + labelGap < placed.y1 ||
              box.y1 - labelGap > placed.y2
            );
            if (overlap) {
              penalty += 9000;
            }
          }

          // 3. Collision avoidance with incident edges (never cross edge segments)
          for (const seg of localEdges) {
            if (lineIntersectsBox(seg.x1, seg.y1, seg.x2, seg.y2, box)) {
              penalty += 4000;
            }
          }

          // 4. Direction preference: bonus for pointing away from edge bundle
          const dot = cosA * avgEdgeDx + sinA * avgEdgeDy;
          if (dot > 0.2) penalty += 50 * dot;
          else if (dot < -0.2) penalty -= 40 * (-dot);

          // Small distance tier penalty so snug labels are preferred if clear
          penalty += (dist - (r + 6)) * 2;

          if (penalty < minPenalty) {
            minPenalty = penalty;
            bestCand = { dx, dy: textY, anchor, box };
            if (penalty <= 0) break; // Found an ideal spot in this tier
          }
        }
        if (minPenalty <= 0) break; // Found ideal spot
      }

      if (minPenalty < 4000) {
        // Clear of nodes and other labels
        node.labelX = bestCand.dx;
        node.labelY = bestCand.dy;
        node.labelAnchor = bestCand.anchor;
        node.labelVisible = true;
        placedBoxes.push({ ...bestCand.box, id: node.id });
      } else if (mustShow) {
        // High-priority node must display; choose least penalty candidate
        node.labelX = bestCand.dx;
        node.labelY = bestCand.dy;
        node.labelAnchor = bestCand.anchor;
        node.labelVisible = true;
        placedBoxes.push({ ...bestCand.box, id: node.id });
      } else {
        node.labelVisible = false;
      }
    }

    // D3 Relaxation Pass: Resolve any subtle residual overlaps
    const placedNodes = candidates.filter((c) => c.node.labelVisible).map((c) => c.node);
    for (let iter = 0; iter < 4; iter++) {
      let anyAdjusted = false;
      for (let i = 0; i < placedNodes.length; i++) {
        const a = placedNodes[i];
        const aBox = placedBoxes[i];
        if (!aBox) continue;

        for (let j = i + 1; j < placedNodes.length; j++) {
          const b = placedNodes[j];
          const bBox = placedBoxes[j];
          if (!bBox) continue;

          const minGap = 6;
          const ox = Math.min(aBox.x2, bBox.x2) - Math.max(aBox.x1, bBox.x1) + minGap;
          const oy = Math.min(aBox.y2, bBox.y2) - Math.max(aBox.y1, bBox.y1) + minGap;

          if (ox > 0 && oy > 0) {
            anyAdjusted = true;
            if (ox < oy) {
              const sign = (a.x + a.labelX) < (b.x + b.labelX) ? -1 : 1;
              const shift = ox * 0.5;
              a.labelX += sign * shift;
              b.labelX -= sign * shift;
              aBox.x1 += sign * shift; aBox.x2 += sign * shift;
              bBox.x1 -= sign * shift; bBox.x2 -= sign * shift;
            } else {
              const sign = (a.y + a.labelY) < (b.y + b.labelY) ? -1 : 1;
              const shift = oy * 0.5;
              a.labelY += sign * shift;
              b.labelY -= sign * shift;
              aBox.y1 += sign * shift; aBox.y2 += sign * shift;
              bBox.y1 -= sign * shift; bBox.y2 -= sign * shift;
            }
          }
        }
      }
      if (!anyAdjusted) break;
    }

    // Apply collision-free coordinates and visibility to DOM
    this.layerNodes.selectAll("text.g-node-label")
      .attr("x", (d) => d.labelX ?? 0)
      .attr("y", (d) => d.labelY ?? ((d.r || 8) + 14))
      .attr("text-anchor", (d) => d.labelAnchor ?? "middle")
      .style("display", (d) => (d.labelVisible ? null : "none"));
  }

  #applyView() {
    this.root.setAttribute("transform", `translate(${this.view.x},${this.view.y}) scale(${this.view.k})`);
  }

  /* ── Search & Path Highlighting ────────────────────────────────────────── */

  /**
   * Focus and highlight a matching entity and its provenance path (Company → Filing → Leaf),
   * while cleanly dimming all unrelated clusters.
   */
  highlightSearchPath(nodeOrId) {
    const id = typeof nodeOrId === "object" ? nodeOrId?.id : nodeOrId;
    const node = this.byId.get(String(id));
    if (!node) return false;

    this.searchPathTargetId = node.id;
    const pathNodes = new Set([node.id]);
    const pathEdges = new Set();

    // 1. If leaf/metric: trace to parent filing and company
    if (node.type === "FinancialMetric" || node.type === "Segment" || node.type === "DisclosureEvent") {
      const parentFilingLinks = this.links.filter(
        (l) => (l.source.id === node.id || l.target.id === node.id) &&
               (l.relation === "REPORTS_METRIC" || l.relation === "DISCLOSES_EVENT" || l.relation === "DISAGGREGATED_BY")
      );
      for (const fl of parentFilingLinks) {
        pathEdges.add(EDGE_KEY(fl));
        const filingNode = fl.source.id === node.id ? fl.target : fl.source;
        pathNodes.add(filingNode.id);

        // From filing to company
        const compLinks = this.links.filter(
          (l) => (l.source.id === filingNode.id || l.target.id === filingNode.id) &&
                 (l.relation === "SUBMITTED" || l.relation === "FILED")
        );
        for (const cl of compLinks) {
          pathEdges.add(EDGE_KEY(cl));
          pathNodes.add(cl.source.id === filingNode.id ? cl.target.id : cl.source.id);
        }
      }
    } else if (node.type === "Filing" || node.type === "Document") {
      // 2. If filing: trace to company, and include immediate child metrics
      const compLinks = this.links.filter(
        (l) => (l.source.id === node.id || l.target.id === node.id) &&
               (l.relation === "SUBMITTED" || l.relation === "FILED")
      );
      for (const cl of compLinks) {
        pathEdges.add(EDGE_KEY(cl));
        pathNodes.add(cl.source.id === node.id ? cl.target.id : cl.source.id);
      }
      const childMetricLinks = this.links.filter(
        (l) => l.source.id === node.id && (l.relation === "REPORTS_METRIC" || l.relation === "DISCLOSES_EVENT")
      );
      for (const ml of childMetricLinks.slice(0, 15)) {
        pathEdges.add(EDGE_KEY(ml));
        pathNodes.add(ml.target.id);
      }
    } else if (node.type === "Company") {
      // 3. If company: highlight its whole cluster
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

    // Smoothly focus camera onto target
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

    // If answer focus is currently active, preserve the answer graph completely:
    // Select the node, display its metadata in the inspector, and redraw.
    if (this.answerPathNodes && this.answerPathNodes.size > 0) {
      this.selected = node.id;
      this.#showDetailPanel(node);
      this.#draw();
      this.handlers.onSelect?.(node);
      return;
    }

    // Default graph state: trace and highlight search/selection branch
    this.highlightSearchPath(node);
    this.handlers.onSelect?.(node);
  }

  #showDetailPanel(node) {
    if (!this.inspectorEl) return;
    this.inspectorEl.hidden = false;

    const incident = this.allLinks.filter((l) => l.source.id === node.id || l.target.id === node.id);
    const company = node.companyName || node.companyId || (node.type === "Company" ? node.name : "—");

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
          <span class="chip chip--dot" style="color: ${typeColor(node.type)}">${escapeHtml(prettyType(node.type))}</span>
          <h3 class="inspector-title">${escapeHtml(node.name)}</h3>
          <button class="btn btn--ghost btn--icon inspector-close" id="inspector-close" title="Close details (Esc)">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
          </button>
        </div>
      </div>
      <div class="inspector-body">
        <div class="inspector-props">
          <div class="inspector-prop"><span class="inspector-prop__key">Entity ID:</span> <span class="inspector-prop__val font-mono">${escapeHtml(node.id)}</span></div>
          <div class="inspector-prop"><span class="inspector-prop__key">Company Anchor:</span> <span class="inspector-prop__val font-bold" style="color:var(--primary)">${escapeHtml(company)}</span></div>
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

    const companies = this.allNodes.filter((n) => n.type === "Company");
    const companyOptions = [
      `<option value="ALL" ${this.companyFilter === "ALL" ? "selected" : ""}>All Companies (Separate Clusters)</option>`,
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
          <span class="side-panel-dot" style="background:${typeColor(type)}"></span>
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

      const searchInput = this.sidePanelEl.querySelector("#entity-search");
      searchInput?.addEventListener("input", (e) => {
        const q = e.target.value.toLowerCase().trim();
        if (!q) {
          this.clearSelection();
          return;
        }
        const match = this.nodes.find((n) => n.name.toLowerCase().includes(q) || n.id.toLowerCase().includes(q));
        if (match) {
          this.highlightSearchPath(match);
        }
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
    setTimeout(() => this.fit(), 300);
  }

  setNodeScale(scale) {
    this.nodeScale = Math.max(0.4, Math.min(2.5, scale));
    for (const n of this.allNodes) {
      const extra = Math.min(5, Math.sqrt(n.degree) * 1.2);
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

    this.#startFlow(
      this.links.filter((l) => this.cited.has(l.source.id) && this.cited.has(l.target.id))
    );
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
      const r = (node.r || 8) + 24;
      minX = Math.min(minX, node.x - r); maxX = Math.max(maxX, node.x + r);
      minY = Math.min(minY, node.y - r); maxY = Math.max(maxY, node.y + r);
    }

    const spanX = Math.max(1, maxX - minX);
    const spanY = Math.max(1, maxY - minY);
    const k = Math.max(0.12, Math.min(1.4, 0.92 * Math.min(width / spanX, height / spanY)));

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
    this.nodes.forEach((n) => { n.x = null; n.y = null; n.vx = 0; n.vy = 0; });
    this.#seedSeparateClusters();
    this.#buildSimulation();
    this.simulation?.alpha(0.85).restart();
    this.#run();
    setTimeout(() => this.fit(), 450);
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
        (enter) => enter.append("path").attr("class", "g-edge-flow is-cited"),
        (update) => update,
        (exit) => exit.remove(),
      )
      .attr("d", (l) => this.#edgePath(l))
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
      <div class="graph-tooltip__type" style="color: ${typeColor(node.type)}">${escapeHtml(prettyType(node.type))}</div>
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
    if (!this.fitted && this.nodes.length) {
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
