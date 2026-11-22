import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import type { GraphData, GraphNode, AskResponse } from '@/types'
import { getGraph } from '@/services/api'
import { Share2, Maximize, Minimize, RefreshCw, Search, Info, X, Map as MapIcon } from 'lucide-react'

// ─── Color palette - distinct, accessible colors for each node type ───
const NODE_COLORS: Record<string, { main: string; light: string; dark: string }> = {
  Company: { main: '#1e3a8a', light: '#3b82f6', dark: '#1e40af' },       // Deep blue
  Filing: { main: '#166534', light: '#22c55e', dark: '#15803d' },        // Emerald green
  FinancialMetric: { main: '#9a3412', light: '#f97316', dark: '#c2410c' }, // Orange/amber
  Announcement: { main: '#7c2d12', light: '#ea580c', dark: '#9a3412' },   // Red-orange
  default: { main: '#475569', light: '#94a3b8', dark: '#334155' },        // Slate
}

// ─── Node size by type ───
const NODE_SIZE: Record<string, number> = {
  Company: 18,
  Filing: 14,
  FinancialMetric: 12,
  Announcement: 10,
  default: 10,
}

// ─── Force-directed simulation parameters ───
const SIMULATION_CONFIG = {
  width: 800,
  height: 600,
  linkDistance: 120,
  chargeStrength: -400,
  centerStrength: 0.02,
  collisionRadius: 8,
  alphaDecay: 0.02,
  velocityDecay: 0.4,
  maxIterations: 300,
}

interface GraphViewProps {
  sessionId: string
  cin?: string | null
  lastResponse?: AskResponse | null  // For answer flow highlighting
  onNodeClick?: (node: GraphNode, evidence: any[]) => void
}

interface SimNode extends GraphNode {
  x: number
  y: number
  vx: number
  vy: number
  fx?: number | null
  fy?: number | null
  // Highlight state
  isHighlighted: boolean
  isAnswerFlow: boolean
  isNeighbor: boolean
}

interface SimEdge {
  source: SimNode | string
  target: SimNode | string
  rel: string
  description?: string
  value?: string | number
  scale?: string
  period?: string
}

export function GraphView({ sessionId, cin, lastResponse, onNodeClick }: GraphViewProps) {
  const [expanded, setExpanded] = useState(false)
  const [showMinimap, setShowMinimap] = useState(false)
  const [graph, setGraph] = useState<GraphData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [simNodes, setSimNodes] = useState<SimNode[]>([])
  const [simEdges, setSimEdges] = useState<SimEdge[]>([])
  const [simulationRunning, setSimulationRunning] = useState(false)
  const [selectedNode, setSelectedNode] = useState<SimNode | null>(null)
  const [hoveredNode, setHoveredNode] = useState<SimNode | null>(null)
  const [transform, setTransform] = useState({ x: 0, y: 0, k: 1 })

  const svgRef = useRef<SVGSVGElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const animationFrameRef = useRef<number | null>(null)
  const simulationRef = useRef<{
    nodes: SimNode[]
    edges: SimEdge[]
    alpha: number
  } | null>(null)

  // ─── Extract cited node IDs from last response ───
  const citedNodeIds = useMemo(() => {
    if (!lastResponse?.citations) return new Set<string>()
    const tags = lastResponse.citations.map(c => c.tag)
    // Tags are like E1, E2, etc. We need to map them to node IDs
    // The graph's tag_map has tag -> node_id mapping
    // For now, we'll match by tag pattern in node data
    return new Set(tags)
  }, [lastResponse])

  // ─── Fetch graph data ───
  useEffect(() => {
    if (!cin) {
      setGraph(null)
      setSimNodes([])
      setSimEdges([])
      return
    }
    setLoading(true)
    setError(null)
    getGraph(sessionId)
      .then(data => {
        setGraph(data)
        initializeSimulation(data)
      })
      .catch(err => {
        setError(err instanceof Error ? err.message : 'Failed to load graph')
      })
      .finally(() => setLoading(false))
  }, [sessionId, cin])

  // ─── Initialize force-directed simulation ───
  const initializeSimulation = useCallback((data: GraphData) => {
    const { nodes, edges } = data
    const width = containerRef.current?.clientWidth || SIMULATION_CONFIG.width
    const height = containerRef.current?.clientHeight || SIMULATION_CONFIG.height
    const centerX = width / 2
    const centerY = height / 2

    // Create simulation nodes with initial positions in a spiral to reduce initial overlap
    const simNodes: SimNode[] = nodes.map((node, i) => {
      const angle = (i * 2.39996323) % (2 * Math.PI) // Golden angle
      const radius = Math.min(width, height) * 0.15 + i * 2
      const tagged = citedNodeIds.has(`E${i + 1}`)
      return {
        ...node,
        x: centerX + radius * Math.cos(angle),
        y: centerY + radius * Math.sin(angle),
        vx: 0,
        vy: 0,
        isHighlighted: false,
        isAnswerFlow: tagged,
        isNeighbor: false,
      }
    })

    // Create simulation edges with node references
    const nodeMap = new Map(simNodes.map(n => [n.id, n] as [string, SimNode]))
    const simEdges: SimEdge[] = edges.map(edge => ({
      ...edge,
      source: nodeMap.get(edge.source) || edge.source,
      target: nodeMap.get(edge.target) || edge.target,
    }))

    simulationRef.current = {
      nodes: simNodes,
      edges: simEdges,
      alpha: 1,
    }

    setSimNodes(simNodes)
    setSimEdges(simEdges)
    setSimulationRunning(true)
    startSimulation()
  }, [citedNodeIds])

  // ─── Force-directed simulation tick ───
  const startSimulation = useCallback(() => {
    if (animationFrameRef.current) {
      cancelAnimationFrame(animationFrameRef.current)
    }

    const tick = () => {
      const sim = simulationRef.current
      if (!sim || sim.alpha < 0.001) {
        setSimulationRunning(false)
        return
      }

      const { nodes, edges, alpha } = sim
      const { width, height, linkDistance, chargeStrength, centerStrength, collisionRadius, velocityDecay } = SIMULATION_CONFIG

      // Apply forces
      nodes.forEach(node => {
        if (node.fx !== undefined && node.fy !== undefined) return // Fixed nodes don't move

        // Center force
        const cx = width / 2 - node.x
        const cy = height / 2 - node.y
        node.vx += cx * centerStrength * alpha
        node.vy += cy * centerStrength * alpha

        // Charge force (repulsion)
        nodes.forEach(other => {
          if (node === other) return
          const dx = other.x - node.x
          const dy = other.y - node.y
          const dist = Math.sqrt(dx * dx + dy * dy) || 1
          const force = (chargeStrength * alpha) / (dist * dist)
          node.vx -= (dx / dist) * force
          node.vy -= (dy / dist) * force
        })

        // Collision force
        nodes.forEach(other => {
          if (node === other) return
          const dx = other.x - node.x
          const dy = other.y - node.y
          const dist = Math.sqrt(dx * dx + dy * dy) || 1
          const minDist = (NODE_SIZE[node.type] || NODE_SIZE.default) + (NODE_SIZE[other.type] || NODE_SIZE.default) + collisionRadius
          if (dist < minDist) {
            const force = (minDist - dist) * 0.5 * alpha
            node.vx -= (dx / dist) * force
            node.vy -= (dy / dist) * force
          }
        })
      })

      // Link force (attraction)
      edges.forEach(edge => {
        const source = typeof edge.source === 'string' ? nodes.find(n => n.id === edge.source) : edge.source
        const target = typeof edge.target === 'string' ? nodes.find(n => n.id === edge.target) : edge.target
        if (!source || !target) return

        const dx = target.x - source.x
        const dy = target.y - source.y
        const dist = Math.sqrt(dx * dx + dy * dy) || 1
        const force = (dist - linkDistance) * 0.1 * alpha
        const fx = (dx / dist) * force
        const fy = (dy / dist) * force

        if (source.fx === undefined) {
          source.vx += fx
          source.vy += fy
        }
        if (target.fx === undefined) {
          target.vx -= fx
          target.vy -= fy
        }
      })

      // Update positions
      nodes.forEach(node => {
        if (node.fx !== undefined && node.fy !== undefined) return
        node.vx *= velocityDecay
        node.vy *= velocityDecay
        node.x += node.vx
        node.y += node.vy

        // Boundary constraints
        const margin = 50
        node.x = Math.max(margin, Math.min(width - margin, node.x))
        node.y = Math.max(margin, Math.min(height - margin, node.y))
      })

      sim.alpha *= (1 - SIMULATION_CONFIG.alphaDecay)
      setSimNodes([...nodes]) // Trigger re-render
      animationFrameRef.current = requestAnimationFrame(tick)
    }

    animationFrameRef.current = requestAnimationFrame(tick)
  }, [])

  // ─── Cleanup on unmount ───
  useEffect(() => {
    return () => {
      if (animationFrameRef.current) {
        cancelAnimationFrame(animationFrameRef.current)
      }
    }
  }, [])

  // ─── Zoom/pan handlers ───
  const handleWheel = useCallback((e: React.WheelEvent) => {
    e.preventDefault()
    const rect = containerRef.current?.getBoundingClientRect()
    if (!rect) return

    const mouseX = e.clientX - rect.left
    const mouseY = e.clientY - rect.top

    const zoomFactor = e.deltaY > 0 ? 0.9 : 1.1
    const newK = Math.max(0.1, Math.min(5, transform.k * zoomFactor))

    const newX = mouseX - (mouseX - transform.x) * (newK / transform.k)
    const newY = mouseY - (mouseY - transform.y) * (newK / transform.k)

    setTransform({ x: newX, y: newY, k: newK })
  }, [transform])

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    if (e.button !== 0 || e.target !== svgRef.current) return
    e.preventDefault()
    const startX = e.clientX - transform.x
    const startY = e.clientY - transform.y

    const moveHandler = (e: MouseEvent) => {
      setTransform(prev => ({
        ...prev,
        x: e.clientX - startX,
        y: e.clientY - startY,
      }))
    }

    const upHandler = () => {
      window.removeEventListener('mousemove', moveHandler)
      window.removeEventListener('mouseup', upHandler)
    }

    window.addEventListener('mousemove', moveHandler)
    window.addEventListener('mouseup', upHandler)
  }, [transform])

  const resetView = useCallback(() => {
    setTransform({ x: 0, y: 0, k: 1 })
  }, [])

  const fitToView = useCallback(() => {
    if (simNodes.length === 0) return
    const width = containerRef.current?.clientWidth || SIMULATION_CONFIG.width
    const height = containerRef.current?.clientHeight || SIMULATION_CONFIG.height

    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity
    simNodes.forEach(node => {
      const r = NODE_SIZE[node.type] || NODE_SIZE.default
      minX = Math.min(minX, node.x - r)
      maxX = Math.max(maxX, node.x + r)
      minY = Math.min(minY, node.y - r)
      maxY = Math.max(maxY, node.y + r)
    })

    const graphWidth = maxX - minX
    const graphHeight = maxY - minY
    const padding = 50
    const scaleX = (width - padding * 2) / graphWidth
    const scaleY = (height - padding * 2) / graphHeight
    const k = Math.min(scaleX, scaleY, 3)

    const centerX = (minX + maxX) / 2
    const centerY = (minY + maxY) / 2
    const x = width / 2 - centerX * k
    const y = height / 2 - centerY * k

    setTransform({ x, y, k })
  }, [simNodes])

  // ─── Node interaction ───
  const handleNodeClick = useCallback((node: SimNode, e: React.MouseEvent) => {
    e.stopPropagation()
    setSelectedNode(node)
    onNodeClick?.(node, graph?.nodes || [])
  }, [graph, onNodeClick])

  const handleNodeMouseEnter = useCallback((node: SimNode) => {
    setHoveredNode(node)
    // Highlight neighbors
    setSimNodes(prev => {
      const neighborIds = new Set<string>()
      simEdges.forEach(edge => {
        const sourceId = typeof edge.source === 'string' ? edge.source : edge.source.id
        const targetId = typeof edge.target === 'string' ? edge.target : edge.target.id
        if (sourceId === node.id) neighborIds.add(targetId)
        if (targetId === node.id) neighborIds.add(sourceId)
      })
      return prev.map(n => ({
        ...n,
        isHighlighted: n.id === node.id,
        isNeighbor: neighborIds.has(n.id),
      }))
    })
  }, [simEdges])

  const handleNodeMouseLeave = useCallback(() => {
    setHoveredNode(null)
    setSimNodes(prev => prev.map(n => ({ ...n, isHighlighted: false, isNeighbor: false })))
  }, [])

  // ─── Compute answer flow nodes ───
  const answerFlowNodeIds = useMemo(() => {
    if (!lastResponse?.citations || !graph) return new Set<string>()
    const tagMap = new Map<string, string>([])
    // Build tag map from graph nodes (E1, E2, ...)
    graph.nodes.forEach((node, i) => {
      tagMap.set(`E${i + 1}`, node.id)
    })
    const ids = new Set<string>()
    lastResponse.citations.forEach(c => {
      const nodeId = tagMap.get(c.tag)
      if (nodeId) ids.add(nodeId)
    })
    return ids
  }, [lastResponse, graph])

  // ─── Update answer flow highlighting when lastResponse changes ───
  useEffect(() => {
    if (!lastResponse || !graph) return
    const flowIds = answerFlowNodeIds
    setSimNodes(prev => prev.map(n => ({
      ...n,
      isAnswerFlow: flowIds.has(n.id),
    })))
  }, [answerFlowNodeIds, lastResponse])

  // ─── Render ───
  if (!cin) return null

  const nodeCount = simNodes.length
  const edgeCount = simEdges.length
  const companies = simNodes.filter(n => n.type === 'Company')
  const metrics = simNodes.filter(n => n.type === 'FinancialMetric')
  const filings = simNodes.filter(n => n.type === 'Filing')
  const announcements = simNodes.filter(n => n.type === 'Announcement')
  const answerFlowCount = simNodes.filter(n => n.isAnswerFlow).length

  return (
    <div className="bg-slate-900 rounded-xl p-4 text-slate-200 relative overflow-hidden h-full flex flex-col">
      {/* Toolbar */}
      <div className="absolute top-4 left-4 right-4 z-10 flex flex-wrap gap-2 justify-between">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-semibold text-white">Company Graph</h3>
          {answerFlowCount > 0 && (
            <span className="px-2 py-0.5 text-xs bg-amber-500/20 text-amber-300 rounded-full border border-amber-500/30 flex items-center gap-1">
              <Info className="w-3 h-3" />
              {answerFlowCount} nodes in answer flow
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={fitToView}
            className="p-2 rounded-lg bg-slate-800 text-white hover:bg-slate-700 transition-colors"
            title="Fit to view"
          >
            <Search className="w-4 h-4" />
          </button>
          <button
            onClick={resetView}
            className="p-2 rounded-lg bg-slate-800 text-white hover:bg-slate-700 transition-colors"
            title="Reset view"
          >
            <Maximize className="w-4 h-4" />
          </button>
          <button
            onClick={() => setExpanded(!expanded)}
            className="p-2 rounded-lg bg-slate-800 text-white hover:bg-slate-700 transition-colors"
            title={expanded ? 'Collapse' : 'Expand'}
          >
            {expanded ? <Minimize className="w-4 h-4" /> : <Maximize className="w-4 h-4" />}
          </button>
          <button
            onClick={() => setShowMinimap(!showMinimap)}
            className="p-2 rounded-lg bg-slate-800 text-white hover:bg-slate-700 transition-colors"
            title={showMinimap ? 'Hide minimap' : 'Show minimap'}
          >
            <MapIcon className="w-4 h-4" />
          </button>
          <button
            onClick={() => {
              setLoading(true)
              getGraph(sessionId)
                .then(data => {
                  setGraph(data)
                  initializeSimulation(data)
                })
                .finally(() => setLoading(false))
            }}
            className="p-2 rounded-lg bg-slate-800 text-white hover:bg-slate-700 transition-colors opacity-70 hover:opacity-100"
            title="Refresh graph"
            disabled={loading}
          >
            <RefreshCw className={`w-4 h-4 ${simulationRunning ? 'animate-spin' : ''}`} />
          </button>
          <button
            className="p-2 rounded-lg bg-slate-800 text-white hover:bg-slate-700 transition-colors"
            title="Share graph"
          >
            <Share2 className="w-4 h-4" />
          </button>
        </div>
      </div>

      {loading && (
        <div className="absolute inset-0 flex items-center justify-center bg-slate-900/80 z-20">
          <div className="flex flex-col items-center gap-2 text-slate-400">
            <div className="w-8 h-8 border-3 border-india-saffron border-t-transparent rounded-full animate-spin" />
            <span className="text-xs">{simulationRunning ? 'Simulating layout…' : 'Loading graph…'}</span>
          </div>
        </div>
      )}

      {error && (
        <div className="absolute bottom-4 left-4 right-4 z-20 p-3 rounded-lg bg-red-500/10 border border-red-500/30 text-xs text-red-300 text-center">
          {error}
        </div>
      )}

      {/* Main graph area */}
      <div
        ref={containerRef}
        className={`relative flex-1 ${expanded ? 'h-[600px]' : 'h-96'} transition-all duration-300`}
        onWheel={handleWheel}
        onMouseDown={handleMouseDown}
      >
        <svg
          ref={svgRef}
          className="w-full h-full cursor-grab active:cursor-grabbing"
          style={{ transformOrigin: '0 0' }}
        >
          <defs>
            {/* Arrow marker for edges */}
            <marker id="arrowhead" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto">
              <polygon points="0 0, 10 3.5, 0 7" fill="#475569" />
            </marker>
            {/* Highlight glow filter */}
            <filter id="glow" x="-50%" y="-50%" width="200%" height="200%">
              <feGaussianBlur stdDeviation="3" result="coloredBlur" />
              <feMerge>
                <feMergeNode in="coloredBlur" />
                <feMergeNode in="SourceGraphic" />
              </feMerge>
            </filter>
            {/* Answer flow pulse animation */}
            <style>{`
              @keyframes pulse {
                0%, 100% { opacity: 0.6; }
                50% { opacity: 1; }
              }
              .answer-flow-node { animation: pulse 2s ease-in-out infinite; }
              .highlighted-node { filter: url(#glow); }
            `}</style>
          </defs>

          {/* Zoom/pan transform group */}
          <g transform={`translate(${transform.x}, ${transform.y}) scale(${transform.k})`}>
            {/* Edges */}
            <g className="edges">
              {simEdges.map((edge, i) => {
                const source = typeof edge.source === 'string' 
                  ? simNodes.find(n => n.id === edge.source) 
                  : edge.source
                const target = typeof edge.target === 'string' 
                  ? simNodes.find(n => n.id === edge.target) 
                  : edge.target
                if (!source || !target) return null

                const sourceR = NODE_SIZE[source.type] || NODE_SIZE.default
                const targetR = NODE_SIZE[target.type] || NODE_SIZE.default

                // Calculate edge endpoints at node boundaries
                const dx = target.x - source.x
                const dy = target.y - source.y
                const dist = Math.sqrt(dx * dx + dy * dy) || 1
                const ux = dx / dist
                const uy = dy / dist

                const x1 = source.x + ux * sourceR
                const y1 = source.y + uy * sourceR
                const x2 = target.x - ux * targetR
                const y2 = target.y - uy * targetR

                // Midpoint for label
                const mx = (x1 + x2) / 2
                const my = (y1 + y2) / 2

                const isHighlighted = source.isHighlighted || target.isHighlighted || source.isNeighbor || target.isNeighbor
                const isAnswerFlow = source.isAnswerFlow && target.isAnswerFlow

                return (
                  <g key={i} className="edge-group">
                    <line
                      x1={x1} y1={y1} x2={x2} y2={y2}
                      stroke={isAnswerFlow ? '#f97316' : isHighlighted ? '#f97316' : '#334155'}
                      strokeWidth={isAnswerFlow ? 2.5 : isHighlighted ? 2 : 1.5}
                      strokeDasharray={isAnswerFlow ? '4 2' : 'none'}
                      marker-end="url(#arrowhead)"
                      className={isAnswerFlow ? 'answer-flow-node' : ''}
                    />
                    {/* Edge label */}
                    {(edge.rel || edge.description) && (
                      <text
                        x={mx}
                        y={my - 4}
                        fontSize="8"
                        fill="#64748b"
                        textAnchor="middle"
                        fontFamily="monospace"
                        pointerEvents="none"
                        className="edge-label"
                      >
                        {edge.rel?.replace(/_/g, ' ')}
                        {edge.description && ` — ${edge.description.slice(0, 30)}${edge.description.length > 30 ? '…' : ''}`}
                      </text>
                    )}
                  </g>
                )
              })}
            </g>

            {/* Nodes */}
            <g className="nodes">
              {simNodes.map((node) => {
                const color = NODE_COLORS[node.type] || NODE_COLORS.default
                const size = NODE_SIZE[node.type] || NODE_SIZE.default
                const label: string = (node.label || node.name || node.metric_name || node.title || node.type) as string

                const isHighlighted = node.isHighlighted || node.isNeighbor
                const isAnswerFlow = node.isAnswerFlow
                const isSelected = selectedNode?.id === node.id

                return (
                  <g
                    key={node.id}
                    className="cursor-pointer node-group"
                    onClick={(e) => handleNodeClick(node, e)}
                    onMouseEnter={() => handleNodeMouseEnter(node)}
                    onMouseLeave={handleNodeMouseLeave}
                    style={{
                      filter: isHighlighted || isSelected ? 'url(#glow)' : 'none',
                    }}
                  >
                    {/* Outer ring for answer flow */}
                    {isAnswerFlow && (
                      <circle
                        cx={node.x} cy={node.y} r={size + 4}
                        fill="none"
                        stroke={color.light}
                        strokeWidth={2}
                        strokeDasharray="6 4"
                        className="answer-flow-node"
                        pointerEvents="none"
                      />
                    )}

                    {/* Selection ring */}
                    {isSelected && (
                      <circle
                        cx={node.x} cy={node.y} r={size + 6}
                        fill="none"
                        stroke="#22c55e"
                        strokeWidth={2}
                        pointerEvents="none"
                      />
                    )}

                    {/* Main node circle */}
                    <circle
                      cx={node.x} cy={node.y} r={size}
                      fill={isAnswerFlow ? color.light : color.main}
                      stroke={isHighlighted ? '#f97316' : '#0f172a'}
                      strokeWidth={isHighlighted ? 2.5 : 1.5}
                    />

                    {/* Node type indicator */}
                    <circle
                      cx={node.x + size * 0.6} cy={node.y - size * 0.6} r={4}
                      fill={color.main}
                      stroke="#0f172a"
                      strokeWidth={1}
                    />

                    {/* Label */}
                    <text
                      x={node.x + size + 4}
                      y={node.y + size * 0.35}
                      fontSize="9"
                      fill={isHighlighted || isSelected ? '#f97316' : '#cbd5e1'}
                      fontFamily="monospace"
                      fontWeight={isAnswerFlow || isHighlighted || isSelected ? '600' : '400'}
                      pointerEvents="none"
                    >
                      {label.slice(0, 20)}
                    </text>

                    {/* Tooltip on hover */}
                    {hoveredNode?.id === node.id && (
                      <g className="tooltip">
                        <rect
                          x={node.x + size + 4}
                          y={node.y - 60}
                          width={200}
                          height={55}
                          rx={4}
                          fill="#1e293b"
                          stroke="#334155"
                          strokeWidth={1}
                          pointerEvents="none"
                        />
                        <text x={node.x + size + 8} y={node.y - 50} fontSize="8" fill="#f97316" fontFamily="monospace" fontWeight="600" pointerEvents="none">
                          {node.type as string}
                        </text>
                        <text x={node.x + size + 8} y={node.y - 40} fontSize="7" fill="#94a3b8" fontFamily="monospace" pointerEvents="none">
                          {label.slice(0, 35)}
                        </text>
                        {(node.metric_name as string) && (
                          <text x={node.x + size + 8} y={node.y - 30} fontSize="7" fill="#f97316" fontFamily="monospace" pointerEvents="none">
                            {node.metric_name as string}
                          </text>
                        )}
                        {node.value !== undefined && (
                          <text x={node.x + size + 8} y={node.y - 20} fontSize="7" fill="#22c55e" fontFamily="monospace" pointerEvents="none">
                            Value: {node.value} {(node.unit as string) || (node.scale as string) || ''}
                          </text>
                        )}
                        {(node.period as string) && (
                          <text x={node.x + size + 8} y={node.y - 10} fontSize="7" fill="#94a3b8" fontFamily="monospace" pointerEvents="none">
                            Period: {node.period as string}
                          </text>
                        )}
                      </g>
                    )}
                  </g>
                )
              })}
            </g>
          </g>
        </svg>

        {/* Minimap */}
        {showMinimap && simNodes.length > 0 && (
          <div className="absolute bottom-4 right-4 w-48 h-36 bg-slate-900/90 border border-slate-700 rounded overflow-hidden pointer-events-none">
            <svg className="w-full h-full" viewBox="0 0 800 600">
              <rect width="800" height="600" fill="#0f172a" />
              {simEdges.map((edge, i) => {
                const source = typeof edge.source === 'string' ? simNodes.find(n => n.id === edge.source) : edge.source
                const target = typeof edge.target === 'string' ? simNodes.find(n => n.id === edge.target) : edge.target
                if (!source || !target) return null
                return (
                  <line
                    key={i}
                    x1={source.x} y1={source.y} x2={target.x} y2={target.y}
                    stroke="#334155" strokeWidth="0.5" opacity="0.5"
                  />
                )
              })}
              {simNodes.map(node => {
                const color = NODE_COLORS[node.type] || NODE_COLORS.default
                const size = 3
                return (
                  <circle
                    key={node.id}
                    cx={node.x} cy={node.y} r={size}
                    fill={color.main}
                  />
                )
              })}
              {/* Viewport indicator */}
              <rect
                x={-transform.x / transform.k}
                y={-transform.y / transform.k}
                width={(containerRef.current?.clientWidth || 800) / transform.k}
                height={(containerRef.current?.clientHeight || 600) / transform.k}
                fill="none"
                stroke="#f97316"
                strokeWidth={1}
                strokeDasharray="2 2"
              />
            </svg>
          </div>
        )}

        {/* Legend */}
        <div className="absolute bottom-4 left-4 flex flex-wrap gap-3 text-xs">
          {Object.entries(NODE_COLORS).filter(([k]) => k !== 'default').map(([type, color]) => {
            const count = simNodes.filter(n => n.type === type).length
            if (count === 0) return null
            return (
              <div key={type} className="flex items-center gap-1.5 px-2 py-1 rounded bg-slate-800/80 border border-slate-700/50 backdrop-blur-sm">
                <span className="w-2 h-2 rounded-full" style={{ backgroundColor: color.main }} />
                <span className="text-slate-300">{type}</span>
                <span className="text-slate-500 font-mono">{count}</span>
              </div>
            )
          })}
        </div>
      </div>

      {/* Selected node detail panel */}
      {selectedNode && (
        <div className="absolute bottom-4 right-4 left-4 z-20 max-h-64 overflow-y-auto">
          <div className="bg-slate-800/95 border border-slate-700 rounded-xl p-4 backdrop-blur-sm">
            <div className="flex items-start justify-between gap-2 mb-3">
              <div>
                <div className="flex items-center gap-2">
                  <span className="w-2 h-2 rounded-full" style={{ backgroundColor: (NODE_COLORS[selectedNode.type as string] || NODE_COLORS.default).main }} />
                  <span className="font-semibold text-white">{selectedNode.type as string}</span>
                  <span className="text-xs text-slate-400 font-mono">{selectedNode.id}</span>
                </div>
                <div className="text-sm text-slate-300 mt-1 font-mono">{(selectedNode.label || selectedNode.name || selectedNode.metric_name || selectedNode.title) as string}</div>
              </div>
              <button onClick={() => setSelectedNode(null)} className="p-1 text-slate-400 hover:text-white"><X className="w-4 h-4" /></button>
            </div>
            <div className="space-y-1 text-xs text-slate-300 font-mono max-h-40 overflow-y-auto">
              {Object.entries(selectedNode).map(([key, value]) => {
                if (key === 'id' || key === 'type' || key === 'label' || key === 'name' || key === 'x' || key === 'y' || key === 'vx' || key === 'vy' || key === 'fx' || key === 'fy' || key === 'isHighlighted' || key === 'isAnswerFlow' || key === 'isNeighbor') return null
                if (value === undefined || value === '' || value === null) return null
                return (
                  <div key={key} className="flex gap-2">
                    <span className="text-slate-500 w-24">{key}:</span>
                    <span className="text-slate-200 break-all">{String(value)}</span>
                  </div>
                )
              })}
            </div>
            {selectedNode.isAnswerFlow && (
              <div className="mt-3 p-2 rounded bg-amber-500/10 border border-amber-500/30 text-xs text-amber-300 flex items-center gap-1">
                <Info className="w-3 h-3" />
                This node was cited in the latest answer
              </div>
            )}
          </div>
        </div>
      )}

      {/* Summary stats */}
      <div className="mt-4 grid grid-cols-2 sm:grid-cols-4 gap-3">
        <StatCard label="Nodes" value={nodeCount} icon="●" color="#94a3b8" />
        <StatCard label="Edges" value={edgeCount} icon="─" color="#64748b" />
        <StatCard label="Answer Flow" value={answerFlowCount} icon="◉" color="#f97316" />
        <StatCard label="Simulation" value={simulationRunning ? 'Running' : 'Settled'} icon={simulationRunning ? '⟳' : '✓'} color={simulationRunning ? '#f97316' : '#22c55e'} />
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        <Badge label="Company" count={companies.length} color="#1e3a8a" />
        <Badge label="Filing" count={filings.length} color="#166534" />
        <Badge label="Metric" count={metrics.length} color="#9a3412" />
        <Badge label="Announcement" count={announcements.length} color="#7c2d12" />
      </div>

      <div className="mt-3 text-xs text-slate-500 font-mono">
        CIN: {graph?.cin || cin} · {graph?.company_name || ''}
      </div>
    </div>
  )
}

// ─── Helper components ───

function StatCard({ label, value, icon, color }: { label: string; value: number | string; icon: string; color: string }) {
  return (
    <div className="p-2 rounded-lg bg-slate-800/50 border border-slate-700/50">
      <div className="flex items-center gap-1.5 mb-1">
        <span className="text-xs" style={{ color }}>{icon}</span>
        <span className="text-xs text-slate-400 uppercase tracking-wider">{label}</span>
      </div>
      <div className="text-sm font-semibold text-white">{value}</div>
    </div>
  )
}

type BadgeProps = { label: string; count: number; color: string }
function Badge({ label, count, color }: BadgeProps) {
  return (
    <div className="flex items-center gap-1.5 px-2 py-1 rounded bg-slate-800 border border-slate-700/50">
      <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: color }} />
      <span className="text-xs text-slate-400">{label}</span>
      <span className="text-xs font-mono text-slate-200">{count}</span>
    </div>
  )
}