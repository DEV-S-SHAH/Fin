import type { Citation } from '@/types'

export function CitationItem({ citation }: { citation: Citation }) {
  return (
    <div className="mb-2 p-3 rounded-lg border border-slate-200 bg-slate-50/50 text-sm">
      <div className="flex justify-between items-start mb-1">
        <span className="font-mono text-xs text-india-saffron font-medium">[{citation.tag}]</span>
        <span className="text-xs text-slate-500">{citation.source}</span>
      </div>
      <p className="text-slate-700">{citation.text}</p>
    </div>
  )
}
