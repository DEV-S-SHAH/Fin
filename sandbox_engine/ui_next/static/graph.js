/* The graph canvas.
 *
 * d3 supplies three things here and nothing else: the force simulation, the
 * zoom behaviour and the drag behaviour. Every pixel is drawn by hand below,
 * because the interesting part of this view is not the layout — it is showing
 * which nodes an answer leaned on, and that is a styling problem, not a
 * charting one.
 *
 * Rendering follows the d3 idiom of binding once and re-running the join on each
 * tick. Clearing and rebuilding the DOM instead turns every frame of a
 * simulation into a layout thrash, and it is why the old page's graph only ever
 * looked settled on a small graph.
 */

import { svgEl, typeColor, prettyType } from "./util.js";

const EDGE_KEY = (l) => `${l.source.id}|${l.target.id}|${l.relation}`;

export class GraphView {
  /**
   * @param {SVGSVGElement} canvas
   * @param {{onSelect: (id: string) => void}} handlers
   */
  constructor(canvas, handlers = {}) {
    this.canvas = canvas;
    this.handlers = handlers;
    this.nodes = [];
    this.links = [];
    this.seeds = new Set();
    this.cited = new Set();
    this.hiddenTypes = new Set();
    this.showLabels = true;
    this.view = { x: 0, y: 0, k: 1 };
    this.frame = null;
    this.nodeDragging = false;
    this.suppressClick = false;
    this.dragTravel = 0;
    this.dragOrigin = null;
    this.disposed = false;

    this.#buildSkeleton();
    this.#buildBehaviours();

    this.observer = new ResizeObserver(() => this.#onResize());
    this.observer.observe(canvas);
  }

  /* ── construction ────────────────────────────────────────────────────── */

  #buildSkeleton() {
    /* The skeleton is assembled with plain DOM calls — append, insertBefore,
     * setAttribute — so the layers are kept as elements and wrapped with
     * d3.select() where a selection is actually needed. */
    this.root = svgEl("g", { class: "g-root" });
    this.layerEdges = d3.select(svgEl("g", { class: "g-edges" }));
    this.layerEdgeLabels = d3.select(svgEl("g", { class: "g-edge-labels" }));
    this.layerNodes = d3.select(svgEl("g", { class: "g-nodes" }));
    this.root.append(this.layerEdges.node(), this.layerEdgeLabels.node(), this.layerNodes.node());
    this.canvas.append(this.root);

    const defs = svgEl("defs");
    // Three markers rather than one styled by class: `marker-end` cannot inherit
    // the stroke colour of the line it caps, so a highlighted edge would still
    // get a grey arrowhead.
    for (const [id, cls] of [["arrow", ""], ["arrow-cited", "is-cited"], ["arrow-incident", "is-incident"]]) {
      const marker = svgEl("marker", {
        id, viewBox: "0 0 8 8", refX: 7, refY: 4,
        markerWidth: 5, markerHeight: 5, orient: "auto-start-reverse",
        markerUnits: "userSpaceOnUse",
      });
      marker.append(svgEl("path", {
        d: "M0 0.6 L7.4 4 L0 7.4 z",
        fill: cls === "is-cited" ? "var(--green)" : cls === "is-incident" ? "var(--accent)" : "var(--border-strong)",
      }));
      defs.append(marker);
    }
    this.canvas.insertBefore(defs, this.root);
    this.markers = defs;
  }

  #buildBehaviours() {
    const zoom = d3.zoom()
      .scaleExtent([0.08, 4])
      .filter((event) => !this.nodeDragging && !event.button)
      .on("start", () => this.canvas.classList.add("is-panning"))
      .on("zoom", (event) => {
        this.view = { x: event.transform.x, y: event.transform.y, k: event.transform.k };
        this.#applyView();
        this.#draw();
      })
      .on("end", () => this.canvas.classList.remove("is-panning"));
    this.zoom = zoom;
    d3.select(this.canvas).call(zoom).on("dblclick.zoom", null);

    const drag = d3.drag()
      .on("start", (event, d) => {
        this.nodeDragging = true;
        this.suppressClick = false;
        this.dragTravel = 0;
        this.dragOrigin = { x: event.x, y: event.y };
        d.fx = d.x; d.fy = d.y;
        if (!event.active && this.simulation) this.simulation.alpha(Math.max(this.simulation.alpha(), 0.7));
        this.canvas.classList.add("is-panning");
        this.#hideTooltip();
      })
      .on("drag", (event, d) => {
        // d3.pointer is in graph space here — the nodes sit under the zoomed
        // root — so the drag lands directly on the simulation's coordinates.
        d.fx = event.x; d.fy = event.y;
        this.dragTravel = Math.max(this.dragTravel, Math.hypot(event.x - this.dragOrigin.x, event.y - this.dragOrigin.y));
        if (this.simulation) this.simulation.alpha(Math.max(this.simulation.alpha(), 0.35));
        this.#run();
      })
      .on("end", (event, d) => {
        d.fx = null; d.fy = null;
        this.nodeDragging = false;
        this.canvas.classList.remove("is-panning");
        this.suppressClick = this.dragTravel > 4;
        if (this.suppressClick) setTimeout(() => { this.suppressClick = false; }, 0);
      });
    this.drag = drag;
  }

  /* ── data ────────────────────────────────────────────────────────────── */

  /**
   * Replace the view.
   *
   * Nodes already on screen keep their position, so focusing a neighbour does
   * not throw the whole layout away — the part of the graph the reader was
   * looking at stays where it was.
   */
  setData(payload, { cited = [], fresh = false } = {}) {
    const previous = new Map(this.nodes.map((n) => [n.id, n]));
    const raw = payload?.nodes || [];
    const { width, height } = this.#size();

    this.nodes = raw
      .filter((n) => n && n.id !== undefined && n.id !== null)
      .map((n) => {
        // Two shapes reach this function: /api/graph says `entity_type`, the
        // payload from /api/ask says `type`. Normalised once, here.
        const type = n.type || n.entity_type || "Unspecified";
        const name = n.name || n.id;
        const old = previous.get(n.id);
        return {
          ...n,
          type,
          name,
          r: 5 + Math.min(6, (name.length || 8) / 6),
          // Fresh nodes land in a deterministic ring around the centre rather
          // than dead centre on top of each other; the simulation fans them out
          // from there.
          x: old?.x ?? width / 2 + Math.cos(this.nodes.length * 2.4) * Math.min(120, width / 4),
          y: old?.y ?? height / 2 + Math.sin(this.nodes.length * 2.4) * Math.min(120, height / 4),
          vx: 0, vy: 0,
          degree: 0,
        };
      });

    const byId = new Map(this.nodes.map((n) => [n.id, n]));
    this.links = (payload?.edges || [])
      .filter((e) => byId.has(e.source) && byId.has(e.target))
      .map((e) => ({
        ...e,
        source: byId.get(e.source),
        target: byId.get(e.target),
      }));

    for (const link of this.links) {
      link.source.degree += 1;
      link.target.degree += 1;
    }
    // Degree is a better size signal than name length: a metric named by nine
    // filings is more important than a segment named by one.
    const maxDegree = this.nodes.reduce((max, n) => Math.max(max, n.degree), 0);
    for (const node of this.nodes) {
      node.r = 5 + (maxDegree ? (node.degree / maxDegree) * 6 : 0);
    }

    this.seeds = new Set((payload?.seeds || []).filter((id) => byId.has(id)));
    this.cited = new Set(cited.filter((id) => byId.has(id)));
    this.byId = byId;
    this.#buildSimulation(fresh);
    /* Painted once here, before the layout starts moving anything, rather than
     * left to the first frame of the animation loop. requestAnimationFrame is
     * throttled in a background tab, so a reader who switched away mid-load
     * would come back to a canvas that had never been drawn. */
    this.#draw();
    this.#run();
    if (fresh) setTimeout(() => this.fit(), 620);
    return { nodes: this.nodes.length, links: this.links.length };
  }

  #buildSimulation(fresh) {
    const { width, height } = this.#size();
    this.simulation?.stop();
    this.simulation = d3.forceSimulation(this.nodes)
      .force("link", d3.forceLink(this.links).id((d) => d.id).distance(96).strength(0.09))
      .force("charge", d3.forceManyBody().strength((d) => -170 - 6 * (d.r || 9)))
      .force("x", d3.forceX(width / 2).strength(0.045))
      .force("y", d3.forceY(height / 2).strength(0.045))
      .force("center", d3.forceCenter(width / 2, height / 2))
      // Repulsion alone cannot promise that two labels stay legible; collide is
      // what actually keeps them apart.
      .force("collide", d3.forceCollide((d) => (d.r || 8) + 8).iterations(2))
      .stop();
    this.simulation.alpha(fresh ? 1 : 0.65);
  }

  #run() {
    if (this.frame) cancelAnimationFrame(this.frame);
    const loop = () => {
      if (this.disposed) return;
      if (this.simulation) this.simulation.tick();
      this.#draw();
      // The simulation is the clock: keep animating while its energy is above
      // the floor, stop the moment it settles instead of spinning rAF forever.
      const settled = !this.simulation || this.simulation.alpha() <= this.simulation.alphaMin();
      this.frame = settled ? null : requestAnimationFrame(loop);
    };
    this.frame = requestAnimationFrame(loop);
  }

  #size() {
    const rect = this.canvas.getBoundingClientRect();
    return {
      width: rect.width || 640,
      height: rect.height || 420,
    };
  }

  #onResize() {
    if (this.disposed) return;
    const { width, height } = this.#size();
    this.simulation
      ?.force("x", d3.forceX(width / 2).strength(0.045))
      .force("y", d3.forceY(height / 2).strength(0.045))
      .force("center", d3.forceCenter(width / 2, height / 2));
    this.#draw();
  }

  /* ── draw ────────────────────────────────────────────────────────────── */

  #draw() {
    this.#applyView();
    const focused = this.seeds.size > 0 || this.cited.size > 0;
    const zoomedIn = this.view.k > 0.6;

    /* edges */
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
      );

    edges
      .attr("d", (l) => this.#edgePath(l))
      .attr("class", (l) => {
        const cited = this.cited.has(l.source.id) && this.cited.has(l.target.id);
        const incident = this.seeds.has(l.source.id) || this.seeds.has(l.target.id);
        return [
          "g-edge",
          cited ? "is-cited" : incident ? "is-incident" : "",
          this.#dimmed(l.source) && this.#dimmed(l.target) ? "is-dim" : "",
        ].filter(Boolean).join(" ");
      })
      .attr("marker-end", (l) => {
        const cited = this.cited.has(l.source.id) && this.cited.has(l.target.id);
        const incident = this.seeds.has(l.source.id) || this.seeds.has(l.target.id);
        return cited ? "url(#arrow-cited)" : incident ? "url(#arrow-incident)" : "url(#arrow)";
      });

    /* The hover text is a child of each edge, created on enter and rewritten in
     * place. Appending it from the join instead would add a second <title> on
     * every simulation tick — a few hundred of them within a few seconds. */
    edges.select("title").text((l) =>
      `${l.source.name} —${String(l.relation || "").replace(/_/g, " ")}→ ${l.target.name}` +
      (l.description ? `\n${l.description}` : ""));

    /* edge labels, only when there is room for them */
    this.layerEdgeLabels.selectAll("text.g-edge-label")
      .data(zoomedIn || focused ? this.links : [], EDGE_KEY)
      .join(
        (enter) => enter.append("text").attr("class", "g-edge-label"),
        (update) => update,
        (exit) => exit.remove(),
      )
      .attr("text-anchor", "middle")
      .attr("x", (l) => (l.source.x + l.target.x) / 2)
      .attr("y", (l) => (l.source.y + l.target.y) / 2 - 4)
      .attr("opacity", (l) => (this.#dimmed(l.source) && this.#dimmed(l.target)) ? 0 : 1)
      .text((l) => String(l.relation || "").replace(/_/g, " ").toLowerCase());

    /* nodes */
    const groups = this.layerNodes.selectAll("g.g-node")
      .data(this.nodes, (d) => d.id)
      .join(
        (enter) => {
          const g = enter.append("g").attr("class", "g-node");
          g.append("circle").attr("class", "halo");
          g.append("circle").attr("class", "core");
          g.append("title");
          g.append("text").attr("text-anchor", "middle");
          g.call(this.drag);
          g.on("pointerenter", (event, d) => this.#showTooltip(event, d))
            .on("pointerleave", () => this.#hideTooltip())
            .on("click", (event, d) => {
              event.stopPropagation();
              if (!this.suppressClick) this.handlers.onSelect?.(d.id);
            });
          return g;
        },
        (update) => update,
        (exit) => exit.remove(),
      )
      .attr("class", (d) => [
        "g-node",
        this.seeds.has(d.id) ? "is-seed" : "",
        this.cited.has(d.id) ? "is-cited" : "",
        this.#dimmed(d) ? "is-dim" : "",
      ].filter(Boolean).join(" "))
      .attr("transform", (d) => `translate(${d.x || 0},${d.y || 0})`);

    groups.select("circle.halo")
      .attr("r", (d) => (d.r || 8) + 5);
    groups.select("circle.core")
      .attr("r", (d) => d.r || 8)
      .attr("fill", (d) => typeColor(d.type));
    groups.select("text")
      .attr("y", (d) => (d.r || 8) + 13)
      .text((d) => (d.name.length > 26 ? `${d.name.slice(0, 25)}…` : d.name))
      .style("display", (d) => (this.showLabels && (this.view.k > 0.42 || this.seeds.has(d.id) || this.cited.has(d.id)))
        ? null : "none");
    groups.select("title")
      .text((d) => `${d.name} (${prettyType(d.type)}${d.description ? ` — ${d.description}` : ""})`);
  }

  #applyView() {
    this.root.setAttribute("transform", `translate(${this.view.x},${this.view.y}) scale(${this.view.k})`);
  }

  #dimmed(node) {
    if (this.hiddenTypes.size && this.hiddenTypes.has(String(node.type).toLowerCase())) return true;
    if (!this.seeds.size && !this.cited.size) return false;
    return !this.seeds.has(node.id) && !this.cited.has(node.id);
  }

  #edgePath(link) {
    const { source: s, target: t } = link;
    if (!s || !t) return "";
    // A gentle curve: straight lines stack into an unreadable bundle wherever
    // two nodes sit at the same point, which they do for the first few ticks of
    // every simulation.
    const mx = (s.x + t.x) / 2;
    const my = (s.y + t.y) / 2;
    const dx = t.x - s.x;
    const dy = t.y - s.y;
    const bend = 0.12;
    const cx = mx - dy * bend;
    const cy = my + dx * bend;
    return `M${s.x},${s.y} Q${cx},${cy} ${t.x},${t.y}`;
  }

  /* ── viewport controls ───────────────────────────────────────────────── */

  #setView(x, y, k) {
    const clamped = Math.max(0.08, Math.min(4, k));
    this.view = { x, y, k: clamped };
    d3.select(this.canvas).call(this.zoom.transform, d3.zoomIdentity.translate(x, y).scale(clamped));
  }

  fit() {
    const { width, height } = this.#size();
    if (!this.nodes.length) {
      this.#setView(width / 2, height / 2, 1);
      return;
    }
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    for (const node of this.nodes) {
      const r = (node.r || 8) + 22;
      minX = Math.min(minX, node.x - r); maxX = Math.max(maxX, node.x + r);
      minY = Math.min(minY, node.y - r); maxY = Math.max(maxY, node.y + r);
    }
    // 0.94 leaves a margin so labels at the extremes are not clipped by the edge.
    const k = Math.max(0.08, Math.min(2.2, 0.94 * Math.min(
      width / Math.max(1, maxX - minX),
      height / Math.max(1, maxY - minY),
    )));
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
    if (!this.simulation) return;
    this.simulation.alpha(1).restart();
    this.#run();
    setTimeout(() => this.fit(), 700);
  }

  /** Centre on a node without refetching — used by citation clicks. */
  focus(id) {
    const node = this.byId?.get(id);
    if (!node) return false;
    const { width, height } = this.#size();
    this.#setView(width / 2 - node.x * this.view.k, height / 2 - node.y * this.view.k, Math.max(this.view.k, 0.6));
    this.#draw();
    return true;
  }

  setCited(ids) {
    this.cited = new Set((ids || []).filter((id) => this.byId?.has(id)));
    this.#draw();
  }

  setHiddenTypes(types) {
    this.hiddenTypes = new Set(types || []);
    this.#draw();
  }

  setLabels(on) {
    this.showLabels = !!on;
    this.#draw();
  }

  /* ── tooltip ─────────────────────────────────────────────────────────── */

  #showTooltip(event, node) {
    const host = this.handlers.tooltipHost || this.canvas.parentElement;
    const tip = this.handlers.tooltip;
    if (!host || !tip) return;
    const relations = this.links
      .filter((l) => l.source === node || l.target === node)
      .slice(0, 6)
      .map((l) => {
        const other = l.source === node ? l.target : l.source;
        return `<div>—${l.relation || "related to"}→ ${escapeText(other.name)}</div>`;
      })
      .join("");
    tip.innerHTML =
      `<div class="graph-tooltip__name">${escapeText(node.name)}</div>` +
      `<div class="graph-tooltip__type">${escapeText(prettyType(node.type))}</div>` +
      (node.description ? `<div class="graph-tooltip__desc">${escapeText(node.description)}</div>` : "") +
      (relations ? `<div class="graph-tooltip__rel">${relations}</div>` : "");

    const bounds = host.getBoundingClientRect();
    tip.classList.add("is-visible");
    const width = tip.offsetWidth;
    const height = tip.offsetHeight;
    let x = event.clientX - bounds.left + 14;
    let y = event.clientY - bounds.top + 14;
    if (x + width > bounds.width) x = event.clientX - bounds.left - width - 14;
    if (y + height > bounds.height) y = bounds.height - height - 8;
    tip.style.left = `${Math.max(4, x)}px`;
    tip.style.top = `${Math.max(4, y)}px`;
  }

  #hideTooltip() {
    this.handlers.tooltip?.classList.remove("is-visible");
  }

  destroy() {
    this.disposed = true;
    this.simulation?.stop();
    if (this.frame) cancelAnimationFrame(this.frame);
    this.observer?.disconnect();
    this.root?.remove();
  }
}

const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
function escapeText(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => HTML_ESCAPES[c]);
}
