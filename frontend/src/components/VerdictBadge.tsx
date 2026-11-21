import type { Verdict } from '@/types'

const VERDICT_STYLE: Record<Verdict, { bg: string; text: string; border: string; dot: string }> = {
  SUPPORTED: { bg: 'bg-green-100', text: 'text-green-800', border: 'border-green-200', dot: 'bg-green-500' },
  QUALIFIED: { bg: 'bg-amber-100', text: 'text-amber-800', border: 'border-amber-200', dot: 'bg-amber-500' },
  REFUSED:   { bg: 'bg-red-100',   text: 'text-red-800',   border: 'border-red-200',   dot: 'bg-red-500' },
}

export function VerdictBadge({ verdict }: { verdict: Verdict }) {
  const style = VERDICT_STYLE[verdict]
  return (
    <span className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-full text-xs font-semibold ${style.bg} ${style.text} ${style.border}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${style.dot}`} />
      {verdict}
    </span>
  )
}
