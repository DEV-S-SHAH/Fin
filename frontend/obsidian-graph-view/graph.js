/**
 * obsidian-graph.js
 * -----------------
 * Framework-agnostic, Obsidian-style force-directed graph view.
 *
 *   Nodes   = notes / entities / documents (circles)
 *   Edges   = links between two nodes (lines)
 *   Layout  = live d3-force simulation (BSD-3), HTML5 Canvas rendering
 *
 * Runtime dependencies: d3-force only (BSD-3). Everything else is vanilla JS +
 * HTML5 Canvas. No API keys, no SaaS, no network calls at runtime — vendor
 * d3-force and the whole thing works offline.
 *
 * Public API
 * ----------
 *   const graph = new ObsidianGraph(container, {
 *     settings: {...},        // partial settings, merged with defaults
 *     createUI: true,         // built-in settings popover (cog button)
 *     callbacks: {
 *       onNodeClick: (id, event) => {},        // open the note / entity
 *       onNodeContextMenu: (id, event, actions) => {},
 *       onSettingsChange: (patch) => {},        // persist / mirror to another instance
 *       onRender: () => {},
 *     },
 *   })
 *   graph.load({ nodes, edges })
 *   graph.focus('note-id', 2)   // local graph mode, depth 1-4
 *   graph.unfocus()             // back to global graph
 *   graph.setSettings({ linkDistance: 120 })
 *   graph.resize()              // call when the container size changes
 *   graph.destroy()
 *
 * Data model
 * ----------
 *   nodes: [{ id, label, created, group, tags[], isAttachment, exists, ... }]
 *   edges: [{ source, target, resolved, ... }]
 *   Edge targets (or sources) missing from `nodes` become dimmed "ghost" nodes
 *   automatically (unresolved links). Node size scales with incoming-link count.
 */

import * as d3 from 'd3-force'

// ══════════════════════════════════════════════════════════════════════
// Defaults — Obsidian's force set + display defaults
// ══════════════════════════════════════════════════════════════════════
const DEFAULTS = {
  // ---- Forces (§4 sliders) ----
  centerForce: 0.05,    // pull toward center (0 = off)
  repelForce: 180,      // node-node repulsion (0 = off)
  linkForce: 0.6,       // link "rubber band" tightness (0..1)
  linkDistance: 100,    // resting link length in px (20..400)
  // ---- Display ----
  showArrows: false,      // arrowheads source → target
  textFadeThreshold: 0.5, // zoom k at which labels fade in (0..2)
  nodeSizeScale: 13,      // px per log1p(incoming links)
  nodeBaseSize: 7,        // min node radius px
  linkThickness: 1.1,     // px
  animate: false,         // chronological time-lapse
  darkTheme: true,
  // ---- Filters ----
  search: '',
  showTags: true,
  showAttachments: true,
  existingOnly: false,    // hide unresolved ghost nodes
  showOrphans: true,      // hide degree-0 notes
  exclusions: [],         // patterns matched against id/label/group/tags
  groups: [],             // [{ id, query, color, halo }]
  // ---- Local graph ----
  localMode: false,
  focusNode: null,
  localDepth: 2,          // 1..4
}

const THEME = {
  dark: {
    bg: '#0b0c10',
    node: '#94a3b8',        // neutral default fill
    nodeStroke: '#f1f5f9',
    edge: '#334155',
    edgeHighlight: '#f9731d',
    ghost: '#64748b',
    text: '#cbd5e1',
    haloGlow: 'rgba(249, 115, 29, 0.35)',
  },
  light: {
    bg: '#ffffff',
    node: '#64748b',
    nodeStroke: '#0f172a',
    edge: '#94a3b8',
    edgeHighlight: '#f9731d',
    ghost: '#94a3b8',
    text: '#475569',
    haloGlow: 'rgba(249, 115, 29, 0.45)',
  },
}

const MIN_ZOOM = 0.2
const MAX_ZOOM = 8
const LOW_OPACITY_NODE = 0.12
const LOW_OPACITY_EDGE = 0.06
const GHOST_OPACITY = 0.5
const GHOST_SIZE_FACTOR = 0.85

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v))

function escapeRegExp(s) {
  return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

function matchPattern(value, pattern) {
  // User exclusion patterns: try regex, fall back to case-insensitive substring.
  try {
    return new RegExp(pattern, 'i').test(value)
  } catch {
    return String(value).toLowerCase().includes(String(pattern).toLowerCase())
  }
}

// ══════════════════════════════════════════════════════════════════════
// ObsidianGraph
// ══════════════════════════════════════════════════════════════════════
export class ObsidianGraph {
  constructor(container, opts = {}) {
    this.container = container
    this.callbacks = opts.callbacks || {}
    this.createUI = opts.createUI !== false
    this.settings = { ...DEFAULTS, ...(opts.settings || {}) }

    this.rawNodes = []
    this.rawEdges = []
    this.nodeById = new Map()        // id → raw node (ghosts added on load)

    this.simNodes = []               // nodes fed to the simulation
    this.simEdges = []               // edges fed to the simulation
    this.simNodeById = new Map()     // id → sim node (rebuilt each _recompute)
    this.visibleIds = new Set()

    this.t = { x: 0, y: 0, k: 1 }    // canvas zoom/pan transform (CSS px)
    this.hoveredId = null
    this.draggingId = null
    this.dragStart = null
    this.dragMoved = false
    this.isPanning = false
    this.panStart = null

    // Touch
    this.touch = { id: null, p1: null, p2: null, dist: 0, mid: null }

    // Animation (time-lapse)
    this.anim = { active: false, startedAt: 0, revealed: new Set() }

    this.raf = null
    this.dirty = true
    this.sim = null

    this._initCanvas()
    this._bindInput()
    if (this.createUI) this._buildUI()
    this._onResize = () => this.resize()
    window.addEventListener('resize', this._onResize)
    this.resize()
    this.scheduleRender()
  }

  // ─── Lifecycle ─────────────────────────────────────────────────────

  _initCanvas() {
    const cs = getComputedStyle(this.container)
    if (cs.position === 'static') this.container.style.position = 'relative'

    this.canvas = document.createElement('canvas')
    this.canvas.style.display = 'block'
    this.canvas.style.width = '100%'
    this.canvas.style.height = '100%'
    this.canvas.style.background = this.settings.darkTheme ? THEME.dark.bg : THEME.light.bg
    this.canvas.tabIndex = 0
    this.container.appendChild(this.canvas)
    this.ctx = this.canvas.getContext('2d', { alpha: false })
  }

  _bindInput() {
    const c = this.canvas

    // Wheel zoom centered on the cursor
    c.addEventListener('wheel', (e) => {
      if (e.ctrlKey || e.metaKey) return
      e.preventDefault()
      const rect = c.getBoundingClientRect()
      const mx = (e.clientX - rect.left - this.t.x) / this.t.k
      const my = (e.clientY - rect.top - this.t.y) / this.t.k
      this._zoomTo(mx, my, e.deltaY > 0 ? 0.9 : 1.1)
    }, { passive: false })

    c.addEventListener('mousedown', (e) => {
      if (e.button !== 0) return
      c.focus()
      this.dragMoved = false
      const pos = this._screenToGraph(e.clientX, e.clientY)
      const hit = this._hitNode(pos.x, pos.y)
      if (hit) {
        this._startDrag(hit, pos)
      } else {
        this._startPan(e.clientX, e.clientY)
      }
    })

    const move = (e) => {
      this._updateHover(this._screenToGraph(e.clientX, e.clientY))
      if (this.draggingId) {
        e.preventDefault()
        this.dragMoved = true
        const simNode = this.simNodeById.get(this.draggingId)
        if (simNode) {
          const p = this._screenToGraph(e.clientX, e.clientY)
          simNode.fx = p.x
          simNode.fy = p.y
          this.sim.alpha(0.3).restart()
        }
        this.scheduleRender()
      } else if (this.isPanning) {
        this.t.x = e.clientX - this.panStart.offsetX
        this.t.y = e.clientY - this.panStart.offsetY
        this.scheduleRender()
      }
    }
    const up = () => {
      this._endDrag()
      this._endPan()
    }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)

    c.addEventListener('click', (e) => {
      if (this.dragMoved) { this.dragMoved = false; return }
      const pos = this._screenToGraph(e.clientX, e.clientY)
      const hit = this._hitNode(pos.x, pos.y)
      if (hit) this.callbacks.onNodeClick?.(hit, e)
    })

    c.addEventListener('contextmenu', (e) => {
      const pos = this._screenToGraph(e.clientX, e.clientY)
      const hit = this._hitNode(pos.x, pos.y)
      if (hit) {
        e.preventDefault()
        const actions = [
          { label: 'Open note', action: 'open' },
          { label: 'Copy link', action: 'copylink' },
        ]
        if (!this._nodeExists(hit)) actions.push({ label: 'Create note', action: 'create' })
        actions.push({ label: 'Center on node', action: 'center' })
        this.callbacks.onNodeContextMenu?.(hit, e, { actions, x: e.clientX, y: e.clientY })
      }
    })

    // Touch
    c.addEventListener('touchstart', (e) => {
      if (e.touches.length === 2) {
        this._touchStartPinch(e)
        return
      }
      const t = e.touches[0]
      const pos = this._screenToGraph(t.clientX, t.clientY)
      const hit = this._hitNode(pos.x, pos.y)
      if (hit) {
        this.touch.id = t.identifier
        this._startDrag(hit, pos)
      } else {
        this._startPan(t.clientX, t.clientY)
        this.touch.id = t.identifier
      }
    }, { passive: false })

    c.addEventListener('touchmove', (e) => {
      if (this.touch.p1 && this.touch.p2) {
        e.preventDefault()
        this._touchMovePinch(e)
        return
      }
      if (this.touch.id !== null && (this.draggingId || this.isPanning)) {
        e.preventDefault()
        const t = Array.from(e.touches).find(t => t.identifier === this.touch.id)
        if (!t) return
        if (this.draggingId) {
          this.dragMoved = true
          const p = this._screenToGraph(t.clientX, t.clientY)
          const simNode = this.simNodeById.get(this.draggingId)
          if (simNode) { simNode.fx = p.x; simNode.fy = p.y; this.sim.alpha(0.3).restart() }
          this.scheduleRender()
        } else if (this.isPanning) {
          this.t.x = t.clientX - this.panStart.offsetX
          this.t.y = t.clientY - this.panStart.offsetY
          this.scheduleRender()
        }
      }
    }, { passive: false })

    c.addEventListener('touchend', (e) => {
      if (this.touch.p1 || this.touch.p2) { this.touch.p1 = null; this.touch.p2 = null; this.touch.dist = 0; this.touch.mid = null }
      if (this.touch.id !== null) {
        this._endDrag()
        this._endPan()
        this.touch.id = null
      }
    })

    // Keyboard: +/- zoom, arrow pan (Shift = faster)
    c.addEventListener('keydown', (e) => {
      if (e.key === '+' || e.key === '=') { e.preventDefault(); this._zoomAtCenter(1.25) }
      else if (e.key === '-') { e.preventDefault(); this._zoomAtCenter(0.8) }
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight' || e.key === 'ArrowUp' || e.key === 'ArrowDown') {
        e.preventDefault()
        const step = (e.shiftKey ? 120 : 40) / this.t.k
        if (e.key === 'ArrowLeft') this.t.x += step
        if (e.key === 'ArrowRight') this.t.x -= step
        if (e.key === 'ArrowUp') this.t.y += step
        if (e.key === 'ArrowDown') this.t.y -= step
        this.scheduleRender()
      }
    })
  }

  _startDrag(nodeId, pos) {
    this.draggingId = nodeId
    this.dragStart = { x: pos.x, y: pos.y }
    const simNode = this.simNodeById.get(nodeId)
    if (simNode) { simNode.fx = pos.x; simNode.fy = pos.y }
    this.sim?.alpha(0.3).restart()
  }

  _endDrag() {
    if (!this.draggingId) return
    const simNode = this.simNodeById.get(this.draggingId)
    if (simNode) { simNode.fx = null; simNode.fy = null }
    this.sim?.alpha(0.2).restart()
    this.draggingId = null
    this.dragStart = null
    this.dragMoved = false
  }

  _startPan(clientX, clientY) {
    this.isPanning = true
    this.panStart = { offsetX: clientX - this.t.x, offsetY: clientY - this.t.y }
    this.canvas.style.cursor = 'grabbing'
  }

  _endPan() {
    this.isPanning = false
    this.canvas.style.cursor = ''
    this.panStart = null
  }

  _updateHover({ x: gx, y: gy }) {
    const hit = this._hitNode(gx, gy)
    if (hit !== this.hoveredId) {
      this.hoveredId = hit
      this.scheduleRender()
    }
  }

  _hitNode(gx, gy) {
    const rScale = 1 / this.t.k
    for (let i = this.simNodes.length - 1; i >= 0; i--) {
      const n = this.simNodes[i]
      if (!n.visible) continue
      const r = (n.radius + 6) * rScale
      if (Math.hypot(gx - n.x, gy - n.y) <= r) return n.id
    }
    return null
  }

  _screenToGraph(clientX, clientY) {
    const rect = this.canvas.getBoundingClientRect()
    return {
      x: (clientX - rect.left - this.t.x) / this.t.k,
      y: (clientY - rect.top - this.t.y) / this.t.k,
    }
  }

  _zoomTo(mx, my, zoomFactor) {
    // mx, my are graph-space coords of the cursor; pin them to the same screen pixel.
    const newK = clamp(this.t.k * zoomFactor, MIN_ZOOM, MAX_ZOOM)
    this.t.x = mx * this.t.k + this.t.x - mx * newK
    this.t.y = my * this.t.k + this.t.y - my * newK
    this.t.k = newK
    this.scheduleRender()
  }

  _zoomAtCenter(zoomFactor) {
    const rect = this.canvas.getBoundingClientRect()
    const mx = (rect.width / 2 - this.t.x) / this.t.k
    const my = (rect.height / 2 - this.t.y) / this.t.k
    this._zoomTo(mx, my, zoomFactor)
  }

  // ─── Data ──────────────────────────────────────────────────────────

  /**
   * Load { nodes, edges }. Nodes referenced by edges but missing from `nodes`
   * become dimmed ghost (unresolved) nodes automatically.
   */
  load(data) {
    this.rawNodes = Array.isArray(data.nodes) ? data.nodes : []
    this.rawEdges = Array.isArray(data.edges) ? data.edges : []

    this.nodeById = new Map()
    for (const n of this.rawNodes) {
      this.nodeById.set(n.id, { ...n, exists: n.exists !== false })
    }
    for (const e of this.rawEdges) {
      for (const end of [e.source, e.target]) {
        if (end && !this.nodeById.has(end)) {
          this.nodeById.set(end, {
            id: end, label: end, group: 'unresolved', exists: false,
            isAttachment: false, tags: [], created: 0,
          })
        }
      }
    }

    this.anim.revealed = new Set()
    this._recompute()
    this._initSimulation()
    this.scheduleRender()
  }

  // ─── Filtering / local graph ───────────────────────────────────────

  _localSubgraphIds() {
    if (!this.settings.localMode || !this.settings.focusNode) return null
    const depth = clamp(Math.round(this.settings.localDepth), 1, 4)
    const visited = new Set([this.settings.focusNode])
    let frontier = [this.settings.focusNode]
    const adj = this._adjacency()
    for (let d = 0; d < depth; d++) {
      const next = []
      for (const id of frontier) {
        for (const nb of (adj.get(id) || [])) {
          if (!visited.has(nb)) { visited.add(nb); next.push(nb) }
        }
      }
      frontier = next
      if (frontier.length === 0) break
    }
    return visited
  }

  _adjacency() {
    const adj = new Map()
    for (const e of this.rawEdges) {
      if (!e.source || !e.target) continue
      if (!adj.has(e.source)) adj.set(e.source, [])
      if (!adj.has(e.target)) adj.set(e.target, [])
      adj.get(e.source).push(e.target)
      adj.get(e.target).push(e.source)
    }
    return adj
  }

  _incomingCount(id) {
    let n = 0
    for (const e of this.rawEdges) if (e.target === id) n++
    return n
  }

  _degree(id) {
    let n = 0
    for (const e of this.rawEdges) if (e.source === id || e.target === id) n++
    return n
  }

  _nodeExists(id) {
    const n = this.nodeById.get(id)
    return n ? n.exists !== false : false
  }

  _tagsOf(node) {
    return Array.isArray(node.tags) ? node.tags.map(String).map(t => t.toLowerCase()) : []
  }

  _haystack(node) {
    return [node.id, node.label, node.group, ...this._tagsOf(node)].join(' ').toLowerCase()
  }

  _matchSearch(node) {
    const s = this.settings.search.trim()
    if (!s) return true
    const q = s.toLowerCase()
    if (q.startsWith('#')) return this._tagsOf(node).includes(q.slice(1))
    return this._haystack(node).includes(q)
  }

  _matchExclusions(node) {
    const fields = [
      node.id,
      node.label,
      node.group,
      Array.isArray(node.tags) ? node.tags.join(' ') : '',
    ]
    for (const pat of this.settings.exclusions) {
      if (!pat) continue
      for (const f of fields) {
        if (matchPattern(f, pat)) return true
      }
    }
    return false
  }

  _matchGroup(node) {
    for (const g of this.settings.groups) {
      if (!g.query) continue
      const q = g.query.toLowerCase()
      if (q.startsWith('#')) { if (this._tagsOf(node).includes(q.slice(1))) return g }
      else if (this._haystack(node).includes(q)) return g
    }
    return null
  }

  _recompute() {
    const localIds = this._localSubgraphIds()

    const visible = new Set()
    for (const [id, node] of this.nodeById) {
      if (localIds && !localIds.has(id)) continue
      if (!this._matchSearch(node)) continue
      if (!this.settings.showTags && node.group === 'tag') continue
      if (!this.settings.showAttachments && node.isAttachment) continue
      if (this.settings.existingOnly && !this._nodeExists(id)) continue
      if (!this.settings.showOrphans && this._degree(id) === 0) continue
      if (this._matchExclusions(node)) continue
      visible.add(id)
    }
    this.visibleIds = visible

    const simEdges = this.rawEdges.filter(
      e => e.source && e.target && visible.has(e.source) && visible.has(e.target))

    const simNodes = []
    for (const id of visible) {
      const node = this.nodeById.get(id)
      const incoming = this._incomingCount(id)
      const size = clamp(
        this.settings.nodeBaseSize + this.settings.nodeSizeScale * Math.log1p(Math.max(0, incoming)),
        this.settings.nodeBaseSize, 64)
      const g = this._matchGroup(node)
      const exists = this._nodeExists(id)
      simNodes.push({
        id, ...node,
        visible: true,
        incoming,
        radius: size,
        size,
        color: g ? g.color : (exists ? null : 'ghost'),
        halo: g ? g.halo : false,
        exists,
        alpha: exists ? 1 : GHOST_OPACITY,
        scale: exists ? 1 : GHOST_SIZE_FACTOR,
      })
    }

    this.simNodes = simNodes
    this.simEdges = simEdges.map(e => ({ ...e, resolved: e.resolved !== false }))
    this.simNodeById = new Map(this.simNodes.map(n => [n.id, n]))
  }

  // ─── Simulation ────────────────────────────────────────────────────

  _initSimulation() {
    this.sim?.stop()
    this.sim = null
    if (this.simNodes.length === 0) { this.scheduleRender(); return }

    const w = this.canvas.clientWidth / 2
    const h = this.canvas.clientHeight / 2
    const spiralR = Math.min(w, h)
    this.simNodes.forEach((n, i) => {
      const angle = (i * 2.39996323) % (2 * Math.PI) // golden angle
      const radius = spiralR * 0.2 + i * 1.4
      n.x = radius * Math.cos(angle)
      n.y = radius * Math.sin(angle)
      n.vx = 0
      n.vy = 0
      n.fx = null
      n.fy = null
    })

    const sim = d3.forceSimulation(this.simNodes)
      .force('link', d3.forceLink(this.simEdges)
        .id(d => d.id)
        .distance(d => d.linkDistance !== undefined ? d.linkDistance : this.settings.linkDistance)
        .strength(d => d.linkStrength !== undefined ? d.linkStrength : this.settings.linkForce))
      .force('charge', d3.forceManyBody().strength(-this.settings.repelForce))
      .force('center', d3.forceCenter(0, 0).strength(this.settings.centerForce))
      .force('collide', d3.forceCollide().radius(d => d.radius + 4).iterations(2))
      .alphaDecay(0.022)
      .velocityDecay(0.4)

    sim.on('tick', () => { this.dirty = true; this.scheduleRender() })
    sim.on('end', () => { this.dirty = true; this.scheduleRender() })
    sim.restart()
    this.sim = sim
  }

  _updateForces() {
    if (!this.sim) return
    const link = this.sim.force('link')
    if (link) {
      link.distance(d => d.linkDistance !== undefined ? d.linkDistance : this.settings.linkDistance)
        .strength(d => d.linkStrength !== undefined ? d.linkStrength : this.settings.linkForce)
    }
    const charge = this.sim.force('charge')
    if (charge) charge.strength(-this.settings.repelForce)
    const center = this.sim.force('center')
    if (center) center.strength(this.settings.centerForce)
    const collide = this.sim.force('collide')
    if (collide) collide.radius(d => d.radius + 4)
    this.sim.alpha(1).restart()
  }

  // ─── Render ────────────────────────────────────────────────────────

  scheduleRender() {
    if (this.raf) return
    this.raf = requestAnimationFrame(() => { this.raf = null; this._frame() })
  }

  _frame() {
    if (this.anim.active) {
      this._animateStep(performance.now())
    }
    this.render()
    if (this.anim.active) this.scheduleRender()
  }

  render() {
    const { ctx, canvas, t, settings } = this
    const dpr = window.devicePixelRatio || 1
    const rect = canvas.getBoundingClientRect()
    const width = rect.width * dpr
    const height = rect.height * dpr
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width
      canvas.height = height
    }
    ctx.setTransform(1, 0, 0, 1, 0, 0)
    ctx.globalAlpha = 1
    ctx.clearRect(0, 0, width, height)

    const theme = THEME[settings.darkTheme ? 'dark' : 'light']
    ctx.fillStyle = theme.bg
    ctx.fillRect(0, 0, width, height)

    ctx.save()
    ctx.translate(t.x * dpr, t.y * dpr)
    ctx.scale(t.k * dpr, t.k * dpr)

    const labelAlpha = clamp((t.k - settings.textFadeThreshold) / 0.4, 0, 1)
    const hover = this.hoveredId
    const neighborIds = hover ? this._neighbors(hover) : null

    // ---- Edges ----
    for (const e of this.simEdges) {
      const s = this.simNodeById.get(e.source)
      const o = this.simNodeById.get(e.target)
      if (!s || !o || !s.visible || !o.visible) continue

      const sHighlighted = s.id === hover || (neighborIds && neighborIds.has(s.id))
      const oHighlighted = o.id === hover || (neighborIds && neighborIds.has(o.id))
      const highlighted = sHighlighted || oHighlighted

      const ghostFade = (!s.exists || !o.exists) ? GHOST_OPACITY : 1
      const fade = highlighted ? 1 : (hover ? LOW_OPACITY_EDGE : 1) * ghostFade

      ctx.beginPath()
      ctx.moveTo(s.x, s.y)
      ctx.lineTo(o.x, o.y)
      ctx.strokeStyle = highlighted ? theme.edgeHighlight : theme.edge
      ctx.lineWidth = (settings.linkThickness * (e.resolved ? 1 : 0.6)) / t.k
      ctx.globalAlpha = fade
      ctx.stroke()

      if (settings.showArrows) {
        const dx = o.x - s.x
        const dy = o.y - s.y
        const dist = Math.hypot(dx, dy) || 1
        const ux = dx / dist, uy = dy / dist
        const ax = o.x - ux * (o.radius + 2)
        const ay = o.y - uy * (o.radius + 2)
        const head = 5 / t.k
        ctx.beginPath()
        ctx.moveTo(ax, ay)
        ctx.lineTo(ax - ux * head + uy * head * 0.5, ay - uy * head - ux * head * 0.5)
        ctx.lineTo(ax - ux * head - uy * head * 0.5, ay - uy * head + ux * head * 0.5)
        ctx.closePath()
        ctx.fillStyle = highlighted ? theme.edgeHighlight : theme.edge
        ctx.globalAlpha = fade
        ctx.fill()
      }
    }

    // ---- Nodes ----
    for (const n of this.simNodes) {
      if (!n.visible) continue
      const highlighted = n.id === hover || (neighborIds && neighborIds.has(n.id))
      const alpha = n.alpha * (highlighted ? 1 : (hover ? LOW_OPACITY_NODE : 1))

      const r = n.radius * n.scale
      const color = n.color === 'ghost' ? theme.ghost : (n.color || theme.node)

      if (n.halo) {
        ctx.beginPath()
        ctx.arc(n.x, n.y, r + 7, 0, 2 * Math.PI)
        ctx.strokeStyle = n.color
        ctx.lineWidth = 3 / t.k
        ctx.globalAlpha = 0.55 * alpha
        ctx.stroke()
      }

      if (highlighted) {
        ctx.beginPath()
        ctx.arc(n.x, n.y, r + 4, 0, 2 * Math.PI)
        ctx.strokeStyle = theme.haloGlow
        ctx.lineWidth = 2 / t.k
        ctx.globalAlpha = 0.5 * alpha
        ctx.stroke()
      }

      ctx.beginPath()
      ctx.arc(n.x, n.y, r, 0, 2 * Math.PI)
      ctx.fillStyle = color
      ctx.globalAlpha = alpha
      ctx.fill()
      ctx.strokeStyle = theme.nodeStroke
      ctx.lineWidth = 1.2 / t.k
      ctx.globalAlpha = 0.7 * alpha
      ctx.stroke()

      if (!n.exists) {
        ctx.beginPath()
        ctx.arc(n.x, n.y, r + 2, 0, 2 * Math.PI)
        ctx.strokeStyle = theme.ghost
        ctx.lineWidth = 1.4 / t.k
        ctx.setLineDash([4, 4])
        ctx.globalAlpha = 0.6 * alpha
        ctx.stroke()
        ctx.setLineDash([])
      }

      if (labelAlpha > 0.05) {
        ctx.fillStyle = theme.text
        ctx.globalAlpha = clamp(labelAlpha * alpha, 0, 1)
        ctx.font = `bold ${10 / t.k}px system-ui, -apple-system, sans-serif`
        ctx.textAlign = 'center'
        ctx.textBaseline = 'middle'
        ctx.fillText(n.label || n.id, n.x, n.y + r + 13 / t.k)
      }
    }

    ctx.restore()
    this.dirty = false
    this.callbacks.onRender?.()
  }

  _neighbors(id) {
    const set = new Set()
    for (const e of this.simEdges) {
      if (e.source === id) set.add(e.target)
      if (e.target === id) set.add(e.source)
    }
    return set
  }

  // ─── Settings ──────────────────────────────────────────────────────

  setSettings(patch) {
    const forceKeys = ['centerForce', 'repelForce', 'linkForce', 'linkDistance']
    const changesForce = forceKeys.some(k => k in patch)
    const anythingElse = Object.keys(patch).some(k => !forceKeys.includes(k))

    this.settings = { ...this.settings, ...patch }

    if (patch.focusNode !== undefined || patch.localMode !== undefined || patch.localDepth !== undefined || anythingElse) {
      this._recompute()
      this._initSimulation()
    }

    if (changesForce) this._updateForces()

    if (patch.animate !== undefined) {
      this.anim.active = !!patch.animate
      if (this.anim.active) { this.anim.startedAt = performance.now(); this.anim.revealed = new Set() }
      else this.anim.revealed = new Set()
    }

    this.canvas.style.background = this.settings.darkTheme ? THEME.dark.bg : THEME.light.bg
    if (this.createUI) this._renderUI()
    this.callbacks.onSettingsChange?.({ ...this.settings })
    this.dirty = true
    this.scheduleRender()
  }

  getSettings() {
    return { ...this.settings }
  }

  // ─── Local graph ───────────────────────────────────────────────────

  focus(nodeId, depth = 2) {
    if (!this.nodeById.has(nodeId)) return
    this.setSettings({ localMode: true, focusNode: nodeId, localDepth: clamp(Math.round(depth), 1, 4) })
    const n = this.simNodeById.get(nodeId)
    if (n) this._zoomToNode(n)
  }

  /** Re-center the viewport on the graph origin (screen center at 1:1 zoom). */
  resetView() {
    this.t = { x: 0, y: 0, k: 1 }
    this.scheduleRender()
  }

  /** Zoom/pan so all visible nodes fit in the viewport. */
  fitToView() {
    if (!this.simNodes.length) return
    const rect = this.canvas.getBoundingClientRect()
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity
    for (const n of this.simNodes) {
      const r = n.radius
      minX = Math.min(minX, n.x - r); maxX = Math.max(maxX, n.x + r)
      minY = Math.min(minY, n.y - r); maxY = Math.max(maxY, n.y + r)
    }
    const pad = 40
    const scaleX = (rect.width - 2 * pad) / (maxX - minX || 1)
    const scaleY = (rect.height - 2 * pad) / (maxY - minY || 1)
    const k = clamp(Math.min(scaleX, scaleY, 2), MIN_ZOOM, MAX_ZOOM)
    const cx = (minX + maxX) / 2
    const cy = (minY + maxY) / 2
    this.t.x = rect.width / 2 - cx * k
    this.t.y = rect.height / 2 - cy * k
    this.t.k = k
    this.scheduleRender()
  }

  unfocus() {
    this.setSettings({ localMode: false, focusNode: null, localDepth: 2 })
  }

  _zoomToNode(node) {
    const rect = this.canvas.getBoundingClientRect()
    this.t.x = rect.width / 2 - node.x * this.t.k
    this.t.y = rect.height / 2 - node.y * this.t.k
    this.scheduleRender()
  }

  // ─── Built-in settings UI ──────────────────────────────────────────

  _buildUI() {
    const container = this.container

    const cog = document.createElement('button')
    cog.type = 'button'
    cog.className = 'og-cog'
    cog.title = 'Graph settings'
    cog.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82-.33l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33h.09a1.65 1.65 0 0 0 1.51 1 2 2 0 0 1 2 2 2 2 0 0 1 2-2h.09a1.65 1.65 0 0 0 1.51-1l.06-.06a1.65 1.65 0 0 0-.33-1.82 1.65 1.65 0 0 0-1.51-1H9a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 19.4 15z"/></svg>'

    const panel = document.createElement('div')
    panel.className = 'og-panel og-hidden'
    panel.innerHTML = '<div class="og-tabs"></div>'

    const root = document.createElement('div')
    root.className = 'og-ui'
    root.appendChild(cog)
    root.appendChild(panel)
    container.appendChild(root)

    this._uiPanel = panel
    this._uiRoot = root
    cog.addEventListener('click', () => {
      const open = !panel.classList.contains('og-hidden')
      panel.classList.toggle('og-hidden', open)
      if (!open) this._renderUI()
    })

    this._renderUI()
  }

  _renderUI() {
    const panel = this._uiPanel
    const S = this.settings
    const tabs = ['Filters', 'Groups', 'Display', 'Forces']
    panel.innerHTML = ''

    const tabBar = document.createElement('div')
    tabBar.className = 'og-tabs'
    const views = {}
    tabs.forEach(name => {
      const b = document.createElement('button')
      b.type = 'button'
      b.className = 'og-tab' + (name === 'Filters' ? ' og-active' : '')
      b.textContent = name
      tabBar.appendChild(b)
      const v = document.createElement('div')
      v.className = 'og-tab-view' + (name === 'Filters' ? '' : ' og-hidden')
      views[name] = v
      panel.appendChild(v)
    })
    panel.insertBefore(tabBar, panel.firstChild)

    tabBar.addEventListener('click', (e) => {
      if (!e.target.classList.contains('og-tab')) return
      tabs.forEach(name => {
        const active = e.target.textContent === name
        views[name].classList.toggle('og-hidden', !active)
      })
      Array.from(tabBar.children).filter(n => n.classList && n.classList.contains('og-tab'))
        .forEach(n => n.classList.toggle('og-active', n.textContent === e.target.textContent))
    })

    const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')

    // ---- Filters ----
    views.Filters.innerHTML = `
      <div class="og-section"><h4>Search files</h4>
        <input type="text" data-ui="search" placeholder="label, path, #tag…" value="${esc(S.search)}" style="width:100%"/>
      </div>
      <div class="og-section">
        <label><input type="checkbox" data-toggle="showTags" ${S.showTags ? 'checked' : ''}> Tag nodes</label>
        <label style="margin-left:12px"><input type="checkbox" data-toggle="showAttachments" ${S.showAttachments ? 'checked' : ''}> Attachments</label>
        <label style="margin-left:12px"><input type="checkbox" data-toggle="existingOnly" ${S.existingOnly ? 'checked' : ''}> Existing files only</label>
        <label style="margin-left:12px"><input type="checkbox" data-toggle="showOrphans" ${S.showOrphans ? 'checked' : ''}> Orphans</label>
        <label style="margin-left:12px"><input type="checkbox" data-toggle="localMode" ${S.localMode ? 'checked' : ''}> Local graph mode</label>
      </div>
      <div class="og-section" id="og-local-box" style="${S.localMode ? '' : 'display:none'}">
        <label>Local depth <select data-ui="localDepth">${[1, 2, 3, 4].map(d => `<option value="${d}" ${d === S.localDepth ? 'selected' : ''}>${d}</option>`).join('')}</select></label>
      </div>
      <div class="og-section">
        <h4>Exclusions</h4>
        <textarea data-ui="exclusions" rows="3" placeholder="one pattern per line (regex or substring)">${esc(S.exclusions.join('\n'))}</textarea>
        <p class="og-hint">Matched against id, label, group and tags.</p>
      </div>
    `

    // ---- Groups ----
    views.Groups.innerHTML = `
      <div class="og-section">
        <h4>Groups</h4>
        <p class="og-hint">Each group = a search query + a color. Matching nodes take that color (+ optional halo ring).</p>
        <div class="og-group-list" data-ui="groupList"></div>
        <button type="button" data-ui="addGroup" class="og-btn-sm">+ New group</button>
      </div>
    `
    this._renderGroups()

    // ---- Display ----
    views.Display.innerHTML = `
      <div class="og-section">
        <label><input type="checkbox" data-toggle="showArrows" ${S.showArrows ? 'checked' : ''}> Arrows (source → target)</label>
        <label style="margin-left:12px"><input type="checkbox" data-toggle="animate" ${S.animate ? 'checked' : ''}> Animate time-lapse</label>
        <label style="margin-left:12px"><input type="checkbox" data-toggle="darkTheme" ${S.darkTheme ? 'checked' : ''}> Dark theme</label>
      </div>
      <div class="og-section"><h4>Text fade threshold</h4>
        <div data-slider="textFadeThreshold">
          <input type="range" min="0" max="2" step="0.05" value="${S.textFadeThreshold}"/>
          <input type="number" min="0" max="2" step="0.05" value="${S.textFadeThreshold}"/>
          <span class="og-val">${S.textFadeThreshold}</span> × zoom
        </div>
        <p class="og-hint">Labels fade out below this zoom so the graph isn't smothered in text.</p>
      </div>
      <div class="og-section"><h4>Node size</h4>
        <div data-slider="nodeSizeScale"><input type="range" min="1" max="40" step="1" value="${S.nodeSizeScale}"/><span class="og-val">${S.nodeSizeScale}</span> px per incoming link (log scale)</div>
        <div data-slider="nodeBaseSize"><input type="range" min="3" max="20" step="1" value="${S.nodeBaseSize}"/><span class="og-val">${S.nodeBaseSize}</span> px base radius</div>
      </div>
      <div class="og-section"><h4>Link thickness</h4>
        <div data-slider="linkThickness"><input type="range" min="0.2" max="4" step="0.1" value="${S.linkThickness}"/><span class="og-val">${S.linkThickness}</span> px</div>
      </div>
    `

    // ---- Forces ----
    views.Forces.innerHTML = `
      <div class="og-section"><h4>Center force</h4>
        <div data-slider="centerForce"><input type="range" min="0" max="1" step="0.01" value="${S.centerForce}"/><span class="og-val">${S.centerForce}</span></div>
        <p class="og-hint">Pull toward the center. Higher = more compact, circular shape.</p>
      </div>
      <div class="og-section"><h4>Repel force</h4>
        <div data-slider="repelForce"><input type="range" min="0" max="600" step="10" value="${S.repelForce}"/><span class="og-val">${S.repelForce}</span></div>
        <p class="og-hint">How strongly nodes push apart. Higher = more spread out.</p>
      </div>
      <div class="og-section"><h4>Link force</h4>
        <div data-slider="linkForce"><input type="range" min="0" max="1" step="0.05" value="${S.linkForce}"/><span class="og-val">${S.linkForce}</span></div>
        <p class="og-hint">Rubber-band tightness of each link. Higher = tighter.</p>
      </div>
      <div class="og-section"><h4>Link distance</h4>
        <div data-slider="linkDistance"><input type="range" min="20" max="400" step="5" value="${S.linkDistance}"/><span class="og-val">${S.linkDistance}</span> px</div>
        <p class="og-hint">Resting length of each link line.</p>
      </div>
      <div class="og-section"><button type="button" data-ui="restoreDefaults" class="og-btn">Restore default settings</button></div>
    `

    this._bindUI()
  }

  _renderGroups() {
    const list = this._uiPanel.querySelector('[data-ui="groupList"]')
    if (!list) return
    const S = this.settings
    list.innerHTML = ''
    if (S.groups.length === 0) {
      list.innerHTML = '<p class="og-hint">No groups yet. Add one to color nodes by query.</p>'
      return
    }
    S.groups.forEach((g, idx) => {
      const row = document.createElement('div')
      row.className = 'og-group-row'
      row.innerHTML = `
        <input type="text" data-idx="${idx}" data-field="query" value="${esc(g.query)}" placeholder="query (substring of id / label / #tag / group)"/>
        <input type="color" data-idx="${idx}" data-field="color" value="${esc(g.color || '#f9731d')}"/>
        <label style="margin-left:6px"><input type="checkbox" data-idx="${idx}" data-field="halo" ${g.halo ? 'checked' : ''}> halo</label>
        <button type="button" data-idx="${idx}" data-field="remove" class="og-btn-sm">×</button>
      `
      list.appendChild(row)
    })
    list.addEventListener('input', (e) => {
      const t = e.target
      const idx = Number(t.dataset.idx)
      const field = t.dataset.field
      const groups = Array.from(S.groups)
      if (field === 'color') groups[idx] = { ...groups[idx], color: t.value }
      else groups[idx] = { ...groups[idx], [field]: t.type === 'checkbox' ? t.checked : t.value }
      this.setSettings({ groups })
    })
    list.addEventListener('click', (e) => {
      const t = e.target
      if (t.dataset.field === 'remove') {
        const idx = Number(t.dataset.idx)
        this.setSettings({ groups: S.groups.filter((_, i) => i !== idx) })
      }
    })
  }

  _bindUI() {
    const panel = this._uiPanel
    const S = this.settings
    const onChange = (key, value) => this.setSettings({ [key]: value })

    const slider = (key, min, max, step) => {
      const wrap = panel.querySelector(`[data-slider="${key}"]`)
      if (!wrap) return
      const input = wrap.querySelector('input[type=range]')
      const text = wrap.querySelector('input[type=number]')
      const show = wrap.querySelector('.og-val')
      const render = () => { show.textContent = input.value; if (text) text.value = input.value }
      input.value = S[key]
      if (text) text.value = S[key]
      show.textContent = input.value
      input.addEventListener('input', () => { render(); onChange(key, parseFloat(input.value)) })
      text?.addEventListener('change', () => { input.value = text.value; render(); onChange(key, parseFloat(text.value)) })
    }

    slider('centerForce', 0, 1, 0.01)
    slider('repelForce', 0, 600, 10)
    slider('linkForce', 0, 1, 0.05)
    slider('linkDistance', 20, 400, 5)
    slider('textFadeThreshold', 0, 2, 0.05)
    slider('nodeSizeScale', 1, 40, 1)
    slider('nodeBaseSize', 3, 20, 1)
    slider('linkThickness', 0.2, 4, 0.1)

    const toggle = (key) => {
      const el = panel.querySelector(`[data-toggle="${key}"]`)
      if (!el) return
      el.checked = !!S[key]
      el.addEventListener('change', () => onChange(key, el.checked))
    }
    toggle('showArrows')
    toggle('animate')
    toggle('darkTheme')
    toggle('showTags')
    toggle('showAttachments')
    toggle('existingOnly')
    toggle('showOrphans')
    toggle('localMode')

    const search = panel.querySelector('[data-ui="search"]')
    if (search) {
      search.value = S.search
      let timer = null
      search.addEventListener('input', (e) => {
        clearTimeout(timer)
        timer = setTimeout(() => onChange('search', e.target.value), 150)
      })
    }

    const excl = panel.querySelector('[data-ui="exclusions"]')
    if (excl) {
      excl.value = S.exclusions.join('\n')
      let timer = null
      excl.addEventListener('input', (e) => {
        clearTimeout(timer)
        timer = setTimeout(() => {
          onChange('exclusions', e.target.value.split('\n').map(l => l.trim()).filter(Boolean))
        }, 200)
      })
    }

    const depth = panel.querySelector('[data-ui="localDepth"]')
    if (depth) {
      depth.value = S.localDepth
      depth.addEventListener('change', () => onChange('localDepth', parseInt(depth.value, 10)))
    }

    const localBox = panel.querySelector('#og-local-box')
    const localToggle = panel.querySelector('[data-toggle="localMode"]')
    if (localToggle && localBox) {
      localToggle.addEventListener('change', () => {
        localBox.style.display = localToggle.checked ? '' : 'none'
      })
    }

    const addGroup = panel.querySelector('[data-ui="addGroup"]')
    if (addGroup) addGroup.addEventListener('click', () => {
      const groups = Array.from(S.groups).concat({ id: 'g' + Date.now(), query: '', color: '#f9731d', halo: false })
      this.setSettings({ groups })
    })

    const restore = panel.querySelector('[data-ui="restoreDefaults"]')
    if (restore) restore.addEventListener('click', () => {
      this.settings = { ...DEFAULTS }
      this.anim.active = false
      this.anim.revealed = new Set()
      this._recompute()
      this._initSimulation()
      this.canvas.style.background = THEME.dark.bg
      this._renderUI()
      this.callbacks.onSettingsChange?.({ ...this.settings })
      this.dirty = true
      this.scheduleRender()
    })
  }

  // ─── Animation (time-lapse) ────────────────────────────────────────

  _animateStep(now) {
    const nodes = this.simNodes
    if (nodes.length === 0) return
    const createds = nodes.map(n => Number(n.created || 0))
    const minC = Math.min(...createds)
    const maxC = Math.max(...createds)
    const span = Math.max(maxC - minC, 86400000) // at least one day
    const elapsed = (now - this.anim.startedAt) / 1000
    const duration = 12 // seconds for a full time-lapse pass
    const t = clamp(elapsed / duration, 0, 1)
    const cutoff = minC + t * span

    const revealed = new Set(this.anim.revealed)
    for (const n of nodes) {
      const c = Number(n.created || 0)
      if (!c || c <= cutoff) revealed.add(n.id)
    }
    this.anim.revealed = revealed

    for (const n of nodes) n.visible = this.visibleIds.has(n.id) && revealed.has(n.id)
    for (const e of this.simEdges) {
      const s = this.simNodeById.get(e.source)
      const o = this.simNodeById.get(e.target)
      e.visible = !!(s && o && s.visible && o.visible)
    }
    if (t >= 1) this.anim.active = false
  }

  // ─── Touch pinch ───────────────────────────────────────────────────

  _touchStartPinch(e) {
    const t1 = e.touches[0], t2 = e.touches[1]
    this.touch.p1 = { id: t1.identifier, x: t1.clientX, y: t1.clientY }
    this.touch.p2 = { id: t2.identifier, x: t2.clientX, y: t2.clientY }
    this.touch.dist = Math.hypot(t1.clientX - t2.clientX, t1.clientY - t2.clientY)
    this.touch.mid = { x: (t1.clientX + t2.clientX) / 2, y: (t1.clientY + t2.clientY) / 2 }
  }

  _touchMovePinch(e) {
    const t1 = Array.from(e.touches).find(t => t.identifier === this.touch.p1.id)
    const t2 = Array.from(e.touches).find(t => t.identifier === this.touch.p2.id)
    if (!t1 || !t2) return
    e.preventDefault()
    const dist = Math.hypot(t1.clientX - t2.clientX, t1.clientY - t2.clientY)
    const mid = { x: (t1.clientX + t2.clientX) / 2, y: (t1.clientY + t2.clientY) / 2 }
    const scale = dist / this.touch.dist || 1

    const gm = this._screenToGraph(this.touch.mid.x, this.touch.mid.y)
    this._zoomTo(gm.x, gm.y, scale)
    this.t.x += mid.x - this.touch.mid.x
    this.t.y += mid.y - this.touch.mid.y

    this.touch.dist = dist
    this.touch.mid = mid
  }

  // ─── Public API ────────────────────────────────────────────────────

  resize() {
    if (!this.canvas) return
    const rect = this.canvas.getBoundingClientRect()
    this.canvas.width = rect.width * (window.devicePixelRatio || 1)
    this.canvas.height = rect.height * (window.devicePixelRatio || 1)
    if (this.simNodes.length > 0) {
      const w = rect.width / 2
      const h = rect.height / 2
      const spiralR = Math.min(w, h)
      this.simNodes.forEach((n, i) => {
        if (n.fx != null || n.fy != null) return
        const angle = (i * 2.39996323) % (2 * Math.PI)
        const radius = spiralR * 0.2 + i * 1.4
        n.x = radius * Math.cos(angle)
        n.y = radius * Math.sin(angle)
      })
      this.sim?.alpha(0.5).restart()
    }
    this.scheduleRender()
  }

  destroy() {
    window.removeEventListener('resize', this._onResize)
    this.sim?.stop()
    if (this.raf) cancelAnimationFrame(this.raf)
    this.canvas?.remove()
    this._uiRoot?.remove()
  }
}
