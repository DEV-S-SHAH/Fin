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

/* How far a curved edge bows away from the straight line between its two
 * nodes, as a fraction of that line's length. Small on purpose: a graph reads as
 * a web of relationships, and a strong bow turns it into a bowl. */
const BEND = 0.11;

    /* How long the one-shot bloom on a freshly cited node lasts. It has to outlast
     * its own animation, or the node is still mid-bloom when the class is dropped
     * and the animation snaps to its start. */
const FLASH_MS = 1300;


/* The marching dots that run along an edge when an answer's subgraph appears, and
 * how long they last. It has to outlast the longest staggered animation plus one
 * of its own passes -- 450ms of stagger and a 1400ms travel -- so the layer is
 * torn down only after the dots have faded out on their own. Removing them early
 * would cut a stream off mid-stride, which reads as a glitch rather than an end.
 * It is an event, not a state: a graph where everything moves forever is a graph
 * nobody can read a label on. */
const FLOW_MS = 2100;

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
    this.fresh = new Set();
    this.hovered = null;
    this.hiddenTypes = new Set();
    this.showLabels = true;
    this.view = { x: 0, y: 0, k: 1 };
    this.frame = null;
    this.nodeDragging = false;
    this.suppressClick = false;
    this.dragTravel = 0;
    this.dragOrigin = null;
    this.flashTimer = 0;
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
     * d3.select() where a selection is actually needed.
     *
     * Edges occupy two layers rather than one. The lower one carries a wide,
     * faint copy of every curve; the upper one carries the crisp line. A single
     * path cannot be both hairline-thin and glowing, and a stroke-width gradient
     * would be the only way to fake it, which costs a paint per edge per frame.
     * */
    this.root = svgEl("g", { class: "g-root" });
    this.layerEdgeGlow = d3.select(svgEl("g", { class: "g-edge-glows" }));
    this.layerEdges = d3.select(svgEl("g", { class: "g-edges" }));
    /* The marching dots sit above the crisp line, not below it. A 3px dot on a
     * 1.1px hairline is wider than the line whichever side it is on, and above
     * is the one that reads as a dot travelling *along* something rather than
     * as a break in it. */
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

    /* The bloom behind a cited node. The region is given in percentages well
     * past the 10%/20% a browser assumes, because a 5px blur on a 20px circle
     * spills further than that and the glow would be clipped into a disc. */
    const bloom = svgEl("filter", {
      id: "g-bloom", x: "-150%", y: "-150%", width: "400%", height: "400%",
      filterUnits: "objectBoundingBox",
    });
    bloom.append(svgEl("feGaussianBlur", { stdDeviation: "5", result: "blur" }));
    const merge = svgEl("feMerge");
    merge.append(svgEl("feMergeNode", { in: "blur" }));
    bloom.append(merge);
    defs.append(bloom);

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
        if (this.hovered !== null) { this.hovered = null; this.#draw(); }
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

    /* Two relationships between the same pair of nodes would otherwise trace the
     * same curve twice, and the reader sees one line where the data says two.
     * The bow is signed, so a group splays around the straight line instead of
     * stacking on it, and a lone edge keeps the neutral bow. */
    const pairs = new Map();
    for (const link of this.links) {
      const key = link.source.id < link.target.id
        ? `${link.source.id}\u0000${link.target.id}`
        : `${link.target.id}\u0000${link.source.id}`;
      if (!pairs.has(key)) pairs.set(key, []);
      pairs.get(key).push(link);
    }
    for (const group of pairs.values()) {
      group.forEach((link, i) => { link.curve = (i - (group.length - 1) / 2) * 1.6; });
    }

    // Degree is a better size signal than name length: a metric named by nine
    // filings is more important than a segment named by one.
    const maxDegree = this.nodes.reduce((max, n) => Math.max(max, n.degree), 0);
    for (const node of this.nodes) {
      node.r = 5 + (maxDegree ? (node.degree / maxDegree) * 6 : 0);
    }

    this.seeds = new Set((payload?.seeds || []).filter((id) => byId.has(id)));
    const nextCited = new Set(cited.filter((id) => byId.has(id)));
    this.#flash(nextCited);
    this.cited = nextCited;
    this.hovered = null;
    this.byId = byId;
    this.#buildSimulation(fresh);
    /* Painted once here, before the layout starts moving anything, rather than
     * left to the first frame of the animation loop. requestAnimationFrame is
     * throttled in a background tab, so a reader who switched away mid-load
     * would come back to a canvas that had never been drawn. */
    this.#draw();
    this.#run();
    if (fresh) setTimeout(() => this.fit(), 620);
    /* Started after the first paint, so the dots are laid along edges that are
     * already on screen. Before it they would be stamped against a `d` the
     * simulation is about to change, and the first frames would show them
     * sliding out from under their own line. */
    this.#startFlow(this.links.filter((l) => {
      const f = this.#edgeFlags(l);
      return f.cited || f.incident;
    }));
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
    this.#drawFlow();

    /* `cited` / `incident` are a ranking, not two independent flags, so the
     * class strings below all go through the same helper — otherwise a layer
     * added later can quietly disagree with the layers above it. */
    const rank = (f) => (f.cited ? "is-cited" : f.incident ? "is-incident" : "");

    /* edges: the wide underlay first, then the crisp line on top of it. */
    this.layerEdgeGlow.selectAll("path.g-edge-glow")
      .data(this.links, EDGE_KEY)
      .join(
        (enter) => enter.append("path").attr("class", "g-edge-glow"),
        (update) => update,
        (exit) => exit.remove(),
      )
      .attr("d", (l) => this.#edgePath(l))
      .attr("class", (l) => {
        const f = this.#edgeFlags(l);
        return ["g-edge-glow", rank(f), f.hot ? "is-hot" : ""].filter(Boolean).join(" ");
      });

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
        const f = this.#edgeFlags(l);
        return ["g-edge", rank(f), f.dim ? "is-dim" : "", f.hot ? "is-hot" : ""]
          .filter(Boolean).join(" ");
      })
      .attr("marker-end", (l) => {
        const f = this.#edgeFlags(l);
        return f.cited ? "url(#arrow-cited)" : f.incident ? "url(#arrow-incident)" : "url(#arrow)";
      });

    /* The hover text is a child of each edge, created on enter and rewritten in
     * place. Appending it from the join instead would add a second <title> on
     * every simulation tick — a few hundred of them within a few seconds. */
    edges.select("title").text((l) =>
      `${l.source.name} —${String(l.relation || "").replace(/_/g, " ")}→ ${l.target.name}` +
      (l.description ? `\n${l.description}` : ""));

    /* edge labels. The answer's own relationships are always named — those are
     * the ones a reader came for — as are the ones that reach out of them. The
     * rest of the graph waits for room. */
    const labelled = this.links.filter((l) => {
      const f = this.#edgeFlags(l);
      if (f.dim) return false;
      if (f.cited || f.incident || f.hot) return true;
      return this.view.k > 0.6;
    });
    this.layerEdgeLabels.selectAll("text.g-edge-label")
      .data(labelled, EDGE_KEY)
      .join(
        (enter) => enter.append("text").attr("class", "g-edge-label"),
        (update) => update,
        (exit) => exit.remove(),
      )
      .attr("text-anchor", "middle")
      .attr("x", (l) => this.#edgePoint(l, 0.5).x)
      .attr("y", (l) => this.#edgePoint(l, 0.5).y - 4)
      .attr("class", (l) => {
        const f = this.#edgeFlags(l);
        return ["g-edge-label", rank(f), f.hot ? "is-hot" : ""].filter(Boolean).join(" ");
      })
      .text((l) => String(l.relation || "").replace(/_/g, " ").toLowerCase());

    /* nodes */
    const groups = this.layerNodes.selectAll("g.g-node")
      .data(this.nodes, (d) => d.id)
      .join(
        (enter) => {
          const g = enter.append("g").attr("class", "g-node");
          // bloom → halo → core, back to front: the soft glow sits behind a crisp
          // ring, which sits behind the dot the reader is actually looking at.
          g.append("circle").attr("class", "bloom");
          g.append("circle").attr("class", "halo");
          g.append("circle").attr("class", "core");
          g.append("title");
          g.append("text").attr("text-anchor", "middle");
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
        this.fresh.has(d.id) ? "is-fresh" : "",
        this.hovered === d.id ? "is-hot" : "",
        this.#dimmed(d, true) ? "is-dim" : "",
      ].filter(Boolean).join(" "))
      .attr("transform", (d) => `translate(${d.x || 0},${d.y || 0})`);

    groups.select("circle.bloom")
      .attr("r", (d) => (d.r || 8) + 11);
    groups.select("circle.halo")
      .attr("r", (d) => (d.r || 8) + 5);
    groups.select("circle.core")
      .attr("r", (d) => d.r || 8)
      .attr("fill", (d) => typeColor(d.type));
    groups.select("text")
      .attr("y", (d) => (d.r || 8) + 13)
      .text((d) => (d.name.length > 26 ? `${d.name.slice(0, 25)}…` : d.name))
      .style("display", (d) => (this.showLabels && (this.view.k > 0.42 || this.seeds.has(d.id) || this.cited.has(d.id) || this.hovered === d.id))
        ? null : "none");
    groups.select("title")
      .text((d) => `${d.name} (${prettyType(d.type)}${d.description ? ` — ${d.description}` : ""})`);
  }

  #applyView() {
    this.root.setAttribute("transform", `translate(${this.view.x},${this.view.y}) scale(${this.view.k})`);
  }

  /**
   * What an edge is doing, in one place.
   *
   * The states are ranked rather than independent: cited, then incident, then
   * neither, then dimmed. That matters because hover would otherwise land on top
   * of cited and repaint the answer's own proof in a neutral grey the moment
   * the pointer drifted across it — and a glow the reader can switch off by
   * moving the mouse is not a signal. Hover is allowed to do exactly one thing
   * here: pull a relationship out of the dimmed mass.
   */
  #edgeFlags(link) {
    const { source: s, target: t } = link;
    const sCited = this.cited.has(s.id);
    const tCited = this.cited.has(t.id);
    const cited = sCited && tCited;
    const incident = !cited && (sCited || tCited || this.seeds.has(s.id) || this.seeds.has(t.id));
    const touching = this.hovered === s.id || this.hovered === t.id;
    return {
      cited,
      incident,
      dim: !touching && this.#dimmed(s) && this.#dimmed(t),
      hot: touching && !cited && !incident,
    };
  }

  /** `ignoreHover` when asking about the node under the pointer itself. */
  #dimmed(node, ignoreHover = false) {
    if (this.hiddenTypes.size && this.hiddenTypes.has(String(node.type).toLowerCase())) return true;
    if (!this.seeds.size && !this.cited.size) return false;
    if (!ignoreHover && this.hovered === node.id) return false;
    return !this.seeds.has(node.id) && !this.cited.has(node.id);
  }

  /** The bow, and the parameters at which each end stops clear of its node. */
  #edgeGeometry(link) {
    const { source: s, target: t } = link;
    const dx = t.x - s.x;
    const dy = t.y - s.y;
    const bow = BEND * (link.curve ?? 1);
    const cx = (s.x + t.x) / 2 - dy * bow;
    const cy = (s.y + t.y) / 2 + dx * bow;
    /* Both ends stop just outside the circles they join. For a quadratic the
     * tangent length at an endpoint is twice the distance to the control point,
     * which is enough to solve for the parameter that lands there — and without
     * the trim the arrowhead is buried inside the node it points at, which
     * makes a cited edge look like it stops short of its own target. */
    const out = 2 * Math.hypot(cx - s.x, cy - s.y) || 1;
    const into = 2 * Math.hypot(t.x - cx, t.y - cy) || 1;
    return {
      cx, cy,
      u0: Math.min(0.45, (s.r + 4) / out),
      u1: 1 - Math.min(0.45, (t.r + 7) / into),
    };
  }

  /** A point on an edge's curve, at parameter `u`. Recomputes the control point
   *  rather than taking it as an argument — this is called twice per label per
   *  frame and the cost is two `hypot` calls, which is not worth an extra
   *  argument threaded through both call sites. */
  #edgePoint(link, u) {
    const { source: s, target: t } = link;
    const { cx, cy } = this.#edgeGeometry(link);
    const m = 1 - u;
    return {
      x: m * m * s.x + 2 * m * u * cx + u * u * t.x,
      y: m * m * s.y + 2 * m * u * cy + u * u * t.y,
    };
  }

  #edgePath(link) {
    const { source: s, target: t } = link;
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
    // One decimal is well under a pixel at any sane zoom, and a path that does
    // not change string between frames does not have to be re-parsed by the
    // renderer on every tick of the simulation.
    const f = (n) => Math.round(n * 10) / 10;
    return `M${f(a.x)},${f(a.y)} Q${f(cx)},${f(cy)} ${f(b.x)},${f(b.y)}`;
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
    const next = new Set((ids || []).filter((id) => this.byId?.has(id)));
    this.#flash(next);
    this.cited = next;
    this.#draw();
  }

  /** How much of the graph the current answer actually leans on. The count is
   *  taken from the same state the drawing uses, so the number on the badge can
   *  never disagree with what is glowing on the canvas. */
  get citationSpread() {
    let edges = 0;
    for (const link of this.links) if (this.#edgeFlags(link).cited) edges += 1;
    return { nodes: this.cited.size, edges };
  }

  /**
   * Bloom once on the nodes an answer has just claimed.
   *
   * A steady glow says *these are the citations*; a single bloom says *this
   * answer is the one you are looking at*. Without the second one, a reader who
   * has already seen a previous answer's glow has nothing to tell the two
   * apart. Only newly cited nodes flash — re-asking a question re-lights the
   * whole page otherwise, and the reader cannot see what changed.
   */
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

  /* ── marching dots ───────────────────────────────────────────────────── */

  /**
   * Run dots along the relationships an answer just leaned on.
   *
   * The glow says which nodes are cited; this says which *relationships* carried
   * the answer, and it does it in the one motion that already means "travelling
   * along" everywhere else on screen. It is drawn on the answer's own edges
   * rather than on everything, because a subgraph can be two hundred and fifty
   * relationships and a graph where all of them move at once is a graph nobody
   * can read. The cited and incident ones are the ones the reader came for.
   *
   * The dots are zero-length dashes with a round cap on a duplicate of the edge's
   * own path, so there is nothing to position by hand: the browser re-lays the
   * dash pattern along whatever `d` currently is, which matters because the force
   * simulation is still moving these nodes while the dots run.
   */
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

  /** Re-stamp the flow paths whenever the graph is painted. Called from #draw so
   *  the dots stay on their edges as the simulation settles, and cheap when no
   *  flow is running. */
  #drawFlow() {
    const links = this.flowLinks;
    if (!links || !links.length) return;
    this.layerEdgeFlow
      .selectAll("path.g-edge-flow")
      .data(links, EDGE_KEY)
      .join(
        (enter) => enter.append("path").attr("class", "g-edge-flow"),
        (update) => update,
      )
      .attr("d", (l) => this.#edgePath(l))
      .attr("class", (l) => {
        const f = this.#edgeFlags(l);
        return ["g-edge-flow", f.cited ? "is-cited" : "is-incident"].filter(Boolean).join(" ");
      })
      /* Staggered in graph order so the streams start a beat apart instead of
       * pulsing as one. A per-edge delay as a custom property rather than a
       * class per step: the step count would be a combinatorial mess of classes
       * to keep in step with the palette. Wrapping the index keeps the spread
       * bounded, so a dense answer does not leave its last edges waiting. */
      .style("--flow-delay", (_, i) => `${(i % 6) * 90}ms`);
  }

  #stopFlow() {
    clearTimeout(this.flowTimer);
    this.flowTimer = 0;
    this.flowLinks = null;
    if (!this.disposed) this.layerEdgeFlow.selectAll("path.g-edge-flow").remove();
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
    clearTimeout(this.flashTimer);
    clearTimeout(this.flowTimer);
    this.observer?.disconnect();
    this.root?.remove();
  }
}

const HTML_ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
function escapeText(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => HTML_ESCAPES[c]);
}
