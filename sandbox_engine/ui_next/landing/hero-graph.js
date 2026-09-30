/* FinGraph landing — animated hero background.
   Visualization of flowing financial data streams & neural knowledge graph:
   Electric Orange-Red (#FF3C00) -> Warm Amber (#FF6B35) connections, company hub,
   metric nodes, document nodes, and flowing data packets.
   Paused off-screen; honours prefers-reduced-motion. */

const DPR_MAX = 1.75;
const CURVE = 0.10;        // edge bow factor
const AMP = 0.007;         // drift amplitude (fraction of min-side)

export class HeroGraph {
  constructor(canvas, container, { interactive = true } = {}) {
    this.canvas = canvas;
    this.container = container;
    this.ctx = canvas.getContext("2d");
    this.interactive = interactive;
    this.reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    this.running = false;
    this.raf = 0;
    this.time = 0;
    this.pointer = { x: 0.5, y: 0.5, targetX: 0.5, targetY: 0.5 };
    this.touching = false;

    this.build();
    this.bind();
    this.resize();
  }

  build() {
    // centre company node + cluster hubs (angle, radius fraction)
    const hubs = [
      { id: "market",    angle: 215, leaves: ["S&P 500", "Sector", "Macro"], kind: "metric" },
      { id: "geography", angle: 145, leaves: ["Americas", "EMEA", "APAC"], kind: "doc" },
      { id: "revenue",   angle: 335, leaves: ["Product", "Services", "Segments"], kind: "metric" },
      { id: "filings",   angle: 25,  leaves: null, kind: "doc" },
    ];
    const filingsSub = [
      { id: "metrics", angle: -8,  leaves: ["Net sales", "Growth"], kind: "metric" },
      { id: "events",  angle: 58,  leaves: ["Earnings", "Guidance"], kind: "doc" },
    ];

    const nodes = [];
    const edges = [];
    const node = (id, radius, angle, kind, parent) =>
      nodes.push({ id, kind, parent, base: polar(radius, angle), o: rndSeed(id) });

    node("company", 0, 0, "company", null);
    for (const hub of hubs) {
      node(hub.id, 1.0, hub.angle, hub.kind === "metric" ? "metric" : "doc", "company");
      edges.push({ from: "company", to: hub.id });
      if (!hub.leaves) continue;
      for (let i = 0; i < hub.leaves.length; i++) {
        const l = hub.leaves[i];
        node(`${hub.id}__${l}`, 1.0, hub.angle, "leaf", hub.id);
        const sub = nodes.find((n) => n.id === `${hub.id}__${l}`);
        sub.base = polar(1.0, hub.angle).add(
          polar(0.12 * (i + 1), hub.angle).rot(70)
        );
        edges.push({ from: hub.id, to: sub.id });
      }
    }
    for (const hub of filingsSub) {
      node(hub.id, 1.0, hub.angle, hub.kind === "metric" ? "metric" : "doc", "filings");
      edges.push({ from: "filings", to: hub.id });
      for (let i = 0; i < hub.leaves.length; i++) {
        const l = hub.leaves[i];
        node(`${hub.id}__${l}`, 1.0, hub.angle, "leaf", hub.id);
        const sub = nodes.find((n) => n.id === `${hub.id}__${l}`);
        sub.base = polar(1.0, hub.angle).add(
          polar(0.1 * (i + 1), hub.angle).rot(70)
        );
        edges.push({ from: hub.id, to: sub.id });
      }
    }
    // cross-data edges
    edges.push({ from: "company", to: "filings" });
    edges.push({ from: "revenue__Services", to: "metrics" });

    this.nodes = nodes;
    this.edges = edges.map((e, i) => ({
      a: nodes.find((n) => n.id === e.from),
      b: nodes.find((n) => n.id === e.to),
      direction: i % 2 === 0 ? 1 : -1,       // alternate flow direction
      phase: rndSeed(`${e.from}>${e.to}`) * Math.PI * 2,
    }));

    // background neural wave paths
    this.waves = [
      { yRatio: 0.38, freq: 0.0018, amp: 28, speed: 0.45, color: "rgba(255, 60, 0, 0.07)" },
      { yRatio: 0.52, freq: 0.0014, amp: 36, speed: 0.35, color: "rgba(255, 107, 53, 0.05)" },
      { yRatio: 0.65, freq: 0.0020, amp: 30, speed: 0.55, color: "rgba(255, 60, 0, 0.06)" },
    ];
  }

  bind() {
    this.onPointer = (event) => {
      if (event.pointerType === "touch") { this.touching = true; }
      const rect = this.container.getBoundingClientRect();
      this.pointer.targetX = (event.clientX - rect.left) / rect.width;
      this.pointer.targetY = (event.clientY - rect.top) / rect.height;
    };
    this.clearPointer = () => {
      this.touching = false;
      this.pointer.targetX = this.pointer.targetY = 0.5;
    };
    this.onResize = () => this.resize();
    this.onResume = (entries) => {
      const inView = entries.some((e) => e.isIntersecting);
      if (inView) this.start(); else this.stop();
    };

    if (this.interactive && !this.reduced) {
      this.container.addEventListener("pointermove", this.onPointer, { passive: true });
      this.container.addEventListener("pointerleave", this.clearPointer, { passive: true });
    }
    window.addEventListener("resize", this.onResize, { passive: true });
    if ("IntersectionObserver" in window) {
      this.io = new IntersectionObserver(this.onResume, { rootMargin: "0px 0px -8% 0px" });
      this.io.observe(this.container);
    }
  }

  resize() {
    const rect = this.container.getBoundingClientRect();
    this.w = Math.max(1, rect.width);
    this.h = Math.max(1, rect.height);
    this.dpr = Math.min(DPR_MAX, window.devicePixelRatio || 1);
    this.canvas.width = Math.round(this.w * this.dpr);
    this.canvas.height = Math.round(this.h * this.dpr);
    this.ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    if (this.reduced) this.draw(0);           // single static frame
  }

  start() {
    if (this.running) return;
    if (this.reduced) { this.draw(0); return; }
    this.running = true;
    let last = performance.now();
    const loop = (now) => {
      if (!this.running) return;
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      this.time += dt;
      this.draw(dt);
      this.raf = requestAnimationFrame(loop);
    };
    this.raf = requestAnimationFrame(loop);
  }

  stop() {
    this.running = false;
    cancelAnimationFrame(this.raf);
  }

  destroy() {
    this.stop();
    window.removeEventListener("resize", this.onResize);
    if (this.io) this.io.disconnect();
    if (this.interactive && !this.reduced) {
      this.container.removeEventListener("pointermove", this.onPointer);
      this.container.removeEventListener("pointerleave", this.clearPointer);
    }
  }

  /* ---------------------------------------------------------------- render */

  nodeX(n, cx, s, pxn) {
    return cx + n.base.x * s * 0.30 +
      Math.sin(this.time * (0.5 + n.o * 0.6) + n.o * 6.28) * s * AMP + pxn;
  }

  nodeY(n, cy, s, pyn) {
    return cy + n.base.y * s * 0.30 +
      Math.cos(this.time * (0.42 + n.o * 0.6) + n.o * 6.28) * s * AMP + pyn;
  }

  draw(dt) {
    const { ctx, w, h } = this;
    const s = Math.min(w, h);
    const cx = w * 0.5;
    const cy = h * 0.46;
    ctx.clearRect(0, 0, w, h);

    if (this.interactive && !this.touching) {
      this.pointer.x += (this.pointer.targetX - this.pointer.x) * Math.min(1, dt * 4);
      this.pointer.y += (this.pointer.targetY - this.pointer.y) * Math.min(1, dt * 4);
    }
    const pxn = (this.pointer.x - 0.5) * 14;
    const pyn = (this.pointer.y - 0.5) * 14;

    // 0. subtle flowing financial neural wave streams across the background
    for (const wave of this.waves) {
      const yBase = h * wave.yRatio;
      ctx.beginPath();
      ctx.strokeStyle = wave.color;
      ctx.lineWidth = 1;
      const step = 24;
      for (let x = 0; x <= w + step; x += step) {
        const y = yBase + Math.sin(x * wave.freq + this.time * wave.speed) * wave.amp +
          Math.cos(x * wave.freq * 0.5 - this.time * wave.speed * 0.6) * (wave.amp * 0.4);
        if (x === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }

    // quadratic bezier helper
    const sampleEdge = (a, b) => {
      const mx = (a.x + b.x) / 2;
      const my = (a.y + b.y) / 2;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const len = Math.hypot(dx, dy) || 1;
      const nx = -dy / len;
      const ny = dx / len;
      const bow = CURVE * len;
      const p = { x: mx + nx * bow, y: my + ny * bow };
      return { p, len };
    };

    ctx.lineWidth = 1;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";

    // 1. static hairline connection with orange-red gradient for every edge
    for (const e of this.edges) {
      const a = { x: this.nodeX(e.a, cx, s, pxn), y: this.nodeY(e.a, cy, s, pyn) };
      const b = { x: this.nodeX(e.b, cx, s, pxn), y: this.nodeY(e.b, cy, s, pyn) };
      const { p } = sampleEdge(a, b);

      const grad = ctx.createLinearGradient(a.x, a.y, b.x, b.y);
      grad.addColorStop(0, "rgba(255, 60, 0, 0.22)");   // Electric Orange-Red
      grad.addColorStop(1, "rgba(255, 107, 53, 0.18)");   // Warm Coral Amber
      ctx.strokeStyle = grad;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.quadraticCurveTo(p.x, p.y, b.x, b.y);
      ctx.stroke();
    }

    // 2. flowing data packets / energy traveling along connections (Orange-Red -> Warm Amber)
    const dash = Math.min(8, Math.max(3, s * 0.010));
    const gap = dash * 4.5;
    ctx.setLineDash([dash, gap]);
    for (const e of this.edges) {
      const a = { x: this.nodeX(e.a, cx, s, pxn), y: this.nodeY(e.a, cy, s, pyn) };
      const b = { x: this.nodeX(e.b, cx, s, pxn), y: this.nodeY(e.b, cy, s, pyn) };
      const { p } = sampleEdge(a, b);
      const flow = (this.time * (0.32 + e.phase * 0.14) + e.phase) % 1;
      ctx.lineDashOffset = e.direction * (flow * (dash + gap));

      const flowGrad = ctx.createLinearGradient(a.x, a.y, b.x, b.y);
      flowGrad.addColorStop(0, "rgba(255, 60, 0, 0.55)");   // Electric Orange-Red
      flowGrad.addColorStop(1, "rgba(255, 107, 53, 0.65)");   // Warm Amber
      ctx.strokeStyle = flowGrad;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.quadraticCurveTo(p.x, p.y, b.x, b.y);
      ctx.stroke();
    }
    ctx.setLineDash([]);

    // 3. Graph nodes:
    //    Company nodes -> Electric Orange-Red (#FF3C00)
    //    Metric nodes  -> Warm Amber (#FF6B35)
    //    Document/evidence nodes -> Soft Coral (rgba(255, 138, 80, 0.6))
    //    Active/closest node -> Brighter orange-red glow
    let closestNode = null;
    let closestDist = Infinity;
    if (this.interactive && !this.reduced) {
      const mx = cx + pxn;
      const my = cy + pyn;
      for (const n of this.nodes) {
        const x = this.nodeX(n, cx, s, pxn);
        const y = this.nodeY(n, cy, s, pyn);
        const d = Math.hypot(x - mx, y - my);
        if (d < closestDist && d < s * 0.12) {
          closestDist = d;
          closestNode = n;
        }
      }
    }

    for (const n of this.nodes) {
      const x = this.nodeX(n, cx, s, pxn);
      const y = this.nodeY(n, cy, s, pyn);
      const isActive = n === closestNode;

      if (n.kind === "company") {
        // Company Node: Electric Orange-Red (#FF3C00) with subtle breath glow
        const pulse = Math.sin(this.time * 1.3 + n.o * 6.28) * 0.5 + 0.5;
        // Outer soft glow
        const glowRad = s * 0.016 + pulse * s * 0.006;
        const glow = ctx.createRadialGradient(x, y, 0, x, y, glowRad);
        glow.addColorStop(0, "rgba(255, 60, 0, 0.45)");
        glow.addColorStop(1, "rgba(255, 60, 0, 0)");
        ctx.fillStyle = glow;
        ctx.beginPath();
        ctx.arc(x, y, glowRad, 0, Math.PI * 2);
        ctx.fill();

        // Inner core
        ctx.fillStyle = "#FF3C00";
        ctx.beginPath();
        ctx.arc(x, y, s * 0.0055, 0, Math.PI * 2);
        ctx.fill();

        // Center dot
        ctx.fillStyle = "#F5F5F7";
        ctx.beginPath();
        ctx.arc(x, y, s * 0.002, 0, Math.PI * 2);
        ctx.fill();
      } else if (n.kind === "metric") {
        // Metric Node: Warm Amber (#FF6B35)
        if (isActive) {
          ctx.fillStyle = "rgba(255, 60, 0, 0.85)";
          ctx.beginPath();
          ctx.arc(x, y, s * 0.0065, 0, Math.PI * 2);
          ctx.fill();
        }
        ctx.fillStyle = "#FF6B35";
        ctx.beginPath();
        ctx.arc(x, y, s * 0.0036, 0, Math.PI * 2);
        ctx.fill();
      } else if (n.kind === "doc") {
        // Document/evidence node: Soft Coral (#FF8A50)
        if (isActive) {
          ctx.fillStyle = "rgba(255, 107, 53, 0.75)";
          ctx.beginPath();
          ctx.arc(x, y, s * 0.006, 0, Math.PI * 2);
          ctx.fill();
        }
        ctx.fillStyle = "rgba(255, 138, 80, 0.75)";
        ctx.beginPath();
        ctx.arc(x, y, s * 0.0032, 0, Math.PI * 2);
        ctx.fill();
      } else {
        // Leaf nodes: Muted warm slate anchors
        ctx.fillStyle = "rgba(156, 163, 175, 0.45)";
        ctx.beginPath();
        ctx.arc(x, y, s * 0.0022, 0, Math.PI * 2);
        ctx.fill();
      }
    }
  }
}

/* ---------------------------------------------------------------- helpers */

function polar(r, deg) {
  const rad = (deg * Math.PI) / 180;
  return {
    x: r * Math.cos(rad),
    y: r * Math.sin(rad),
    add(v) { return { x: this.x + v.x, y: this.y + v.y }; },
    rot(deg2) {
      const rad2 = (deg2 * Math.PI) / 180;
      const c = Math.cos(rad2);
      const s2 = Math.sin(rad2);
      return { x: this.x * c - this.y * s2, y: this.x * s2 + this.y * c };
    },
  };
}

function rndSeed(key) {
  let h = 2166136261;
  for (let i = 0; i < key.length; i++) {
    h ^= key.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return ((h >>> 16) % 10000) / 10000;
}
