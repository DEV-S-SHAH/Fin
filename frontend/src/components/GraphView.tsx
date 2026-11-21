import { useState, useEffect } from 'react'
import type { GraphData } from '@/types'
import { getGraph } from '@/services/api'
import { Share2, Maximize, Minimize, RefreshCw } from 'lucide-react'

const NODE_COLORS: Record<string, string> = {
  Company: '#000080',
  Filing: '#138808',
  FinancialMetric: '#FF9933',
  Announcement: '#FF9933',
}

export function GraphView({ sessionId, cin }: { sessionId: string; cin?: string | null }) {
  const [expanded, setExpanded] = useState(false)
  const [graph, setGraph] = useState<GraphData | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!cin) {
      setGraph(null)
      return
    }
    setLoading(true)
    setError(null)
    getGraph(sessionId)
      .then(setGraph)
      .catch((err) => {
        setError(err instanceof Error ? err.message : 'Failed to load graph')
      })
      .finally(() => setLoading(false))
  }, [sessionId, cin])

  if (!cin) return null

  const nodes = graph ? graph.nodes : []
  const edges = graph ? graph.edges : []

  const nodeCount = nodes.length
  const edgeCount = edges.length
  const companies = nodes.filter((n) => n.type === 'Company')
  const metrics = nodes.filter((n) => n.type === 'FinancialMetric')
  const filings = nodes.filter((n) => n.type === 'Filing')
  const announcements = nodes.filter((n) => n.type === 'Announcement')

  return (
    <div className="bg-slate-900 rounded-xl p-4 text-slate-200 relative overflow-hidden">
      <div className="absolute top-4 right-4 z-10 flex gap-2">
        <button
          onClick={() => setExpanded(!expanded)}
          className="p-2 rounded-lg bg-slate-800 text-white hover:bg-slate-700 transition-colors"
          title={expanded ? 'Collapse' : 'Expand'}
        >
          {expanded ? <Minimize className="w-4 h-4" /> : <Maximize className="w-4 h-4" />}
        </button>
        <button
          onClick={() => {
            setLoading(true)
            getGraph(sessionId)
              .then(setGraph)
              .finally(() => setLoading(false))
          }}
          className="p-2 rounded-lg bg-slate-800 text-white hover:bg-slate-700 transition-colors opacity-70 hover:opacity-100"
          title="Refresh graph"
        >
          <RefreshCw className="w-4 h-4" />
        </button>
        <button
          className="p-2 rounded-lg bg-slate-800 text-white hover:bg-slate-700 transition-colors"
          title="Share graph"
        >
          <Share2 className="w-4 h-4" />
        </button>
      </div>

      {loading && (
        <div className="absolute inset-0 flex items-center justify-center bg-slate-900/80 z-20">
          <span className="text-slate-400 text-xs">Loading graph…</span>
        </div>
      )}

      {error && (
        <div className="absolute bottom-4 left-4 right-4 z-20 p-3 rounded-lg bg-red-500/10 border border-red-500/30 text-xs text-red-300 text-center">
          {error}
        </div>
      )}

      {!loading && graph ? (
        <div className={`relative h-64 ${expanded ? 'h-[500px]' : ''} transition-all duration-300`}>
          {/* Graph visualization using SVG */}
          <svg className="w-full h-full" viewBox="0 0 100 100">
            <defs>
              <marker id="arrow" markerWidth="10" markerHeight="7" refX="8" refY="3.5" orient="auto">
                <polygon points="0 0, 10 3.5, 0 7" fill="#94a3b8" />
              </marker>
            </defs>

            {/* Edges */}
            {edges.map((edge, i) => {
              const srcNode = nodes.find((n) => n.id === edge.source)
              const tgtNode = nodes.find((n) => n.id === edge.target)
              if (!srcNode || !tgtNode) return null
              const angle = (2 * Math.PI * i) / Math.max(nodes.length, 1)
              const cx = 50, cy = 50, r = 35
              const x1 = cx + r * Math.cos(angle)
              const y1 = cy + r * Math.sin(angle)
              const angle2 = (2 * Math.PI * ((i + 1) % nodes.length)) / Math.max(nodes.length, 1)
              const x2 = cx + r * Math.cos(angle2)
              const y2 = cy + r * Math.sin(angle2)
              return (
                <line
                  key={i}
                  x1={x1} y1={y1} x2={x2} y2={y2}
                  stroke="#334155"
                  strokeWidth="1.5"
                  marker-end="url(#arrow)"
                />
              )
            })}

            {/* Nodes */}
            {nodes.map((node, i) => {
              const angle = (2 * Math.PI * i) / nodes.length
              const cx = 50, cy = 50, r = 35
              const x = cx + r * Math.cos(angle)
              const y = cy + r * Math.sin(angle)
              const color = NODE_COLORS[node.type] || '#94a3b8'
              return (
                <g key={node.id} className="cursor-pointer" style={{ cursor: 'pointer' }}>
                  <circle cx={x} cy={y} r={5} fill={color} stroke="#0f172a" strokeWidth="1" />
                  <text
                    x={x + 7}
                    y={y + 4}
                    fontSize="10"
                    fill="#cbd5e1"
                    fontFamily="monospace"
                  >
                    {(node.label || node.name || node.type).slice(0, 12)}
                  </text>
                </g>
              )
            })}
          </svg>
        </div>
      ) : (
        <div className="h-64 flex items-center justify-center text-slate-500 text-sm">
          {error ? '' : 'Build graph by asking a question.'}
        </div>
      )}

      {/* Summary stats */}
      <div className="mt-4 grid grid-cols-2 gap-3">
        <div className="p-2 rounded-lg bg-slate-800/50 border border-slate-700/50">
          <div className="text-xs text-slate-400 uppercase tracking-wider">Nodes</div>
          <div className="text-sm font-semibold text-white">{nodeCount}</div>
        </div>
        <div className="p-2 rounded-lg bg-slate-800/50 border border-slate-700/50">
          <div className="text-xs text-slate-400 uppercase tracking-wider">Edges</div>
          <div className="text-sm font-semibold text-white">{edgeCount}</div>
        </div>
      </div>

      <div className="mt-3 flex gap-2">
        <Badge label="Company" count={companies.length} color="#000080" />
        <Badge label="Filing" count={filings.length} color="#138808" />
        <Badge label="Metric" count={metrics.length} color="#FF9933" />
        <Badge label="Announcement" count={announcements.length} color="#FF9933" />
      </div>

      <div className="mt-3 text-xs text-slate-500 font-mono">
        CIN: {graph ? graph.cin : cin} · {graph ? graph.company_name : ''}
      </div>
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
