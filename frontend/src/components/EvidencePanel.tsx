import type { Citation, EvidenceItem } from '@/types'
import { CitationItem } from './CitationItem'
import { FileText, FileSearch, AlertTriangle, ShieldAlert } from 'lucide-react'

const EVIDENCE_ICONS: Record<string, React.ElementType> = {
  STATED: FileText,
  DERIVED: FileSearch,
  INFERRED: AlertTriangle,
  EXTERNAL: ShieldAlert,
  GAP: AlertTriangle,
}

export function EvidencePanel({
  citations,
  evidence,
}: {
  citations: Citation[]
  evidence: EvidenceItem[]
}) {
  return (
    <div className="bg-slate-900 rounded-xl p-4 text-slate-200">
      <div className="flex items-center gap-2 mb-3">
        <FileText className="w-4 h-4 text-india-saffron" />
        <h3 className="text-sm font-semibold text-white">Evidence ({citations.length} cited)</h3>
      </div>

      <div className="space-y-2">
        {citations.map((c, i) => (
          <CitationItem key={i} citation={c} />
        ))}
        {citations.length === 0 && (
          <div className="p-2 rounded bg-red-500/10 border border-red-500/30 text-xs text-red-300">
            No citations — answer could not be grounded in evidence.
          </div>
        )}
      </div>

      {evidence.length > 0 && (
        <div className="mt-4 pt-4 border-t border-slate-700">
          <div className="flex items-center gap-2 mb-2 text-xs text-slate-400 uppercase tracking-wider font-semibold">
            <ShieldAlert className="w-3 h-3" /> Provenance detail
          </div>
          <div className="space-y-1 text-xs">
            {evidence.map((e) => {
              const Icon = EVIDENCE_ICONS[e.provenance] || FileText
              return (
                <div key={e.tag} className="flex items-center gap-2">
                  <Icon className="w-3 h-3 text-slate-500" />
                  <span className="font-mono">{e.tag}</span>
                  <span className="text-slate-500">· {e.provenance}</span>
                  <span className="text-slate-500 ml-2">{e.source}</span>
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
