import type { Verdict } from '@/types'
import { VerdictBadge } from './VerdictBadge'
import { User, Bot, Clock } from 'lucide-react'

type MessageProps = {
  role: 'user' | 'assistant'
  content: string
  verdict?: Verdict
  timestamp?: string
}

export function Message({ role, content, verdict, timestamp }: MessageProps) {
  const isUser = role === 'user'
  return (
    <div className={`flex gap-3 mb-4 ${isUser ? 'flex-row-reverse' : ''}`}>
      <div className={`w-8 h-8 rounded-full flex items-center justify-center shrink-0 ${
        isUser ? 'bg-india-saffron text-white' : 'bg-slate-100 text-slate-600'
      }`}>
        {isUser ? <User className="w-4 h-4" /> : <Bot className="w-4 h-4" />}
      </div>
      <div className={`max-w-[85%] rounded-lg p-3 ${
        isUser
          ? 'bg-india-saffron text-white text-left'
          : 'bg-white border border-slate-200 text-slate-900 text-left'
      }`}>
        <div className="prose prose-sm max-w-none">
          {content.split('\n').map((line, i) => (
            <p key={i} className={i === 0 ? 'mt-0' : 'mt-2'}>
              {line}
            </p>
          ))}
        </div>
        {verdict && (
          <div className="mt-2 flex items-center gap-1.5">
            <VerdictBadge verdict={verdict} />
            {timestamp && (
              <span className="ml-auto text-xs text-slate-400 flex items-center gap-1">
                <Clock className="w-3 h-3" /> {timestamp}
              </span>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
