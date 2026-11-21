import { useState, useRef, useCallback, useEffect } from 'react'
import type { AskResponse, Verdict } from '@/types'
import { askQuestion } from '@/services/api'
import { Message } from './Message'
import { EvidencePanel } from './EvidencePanel'
import { RefreshCw } from 'lucide-react'

type ChatProps = {
  sessionId: string
  cin: string | null
  companyName: string | null
  onBindCompany: (ticker: string, name: string) => void
}

export function Chat({ sessionId, cin, companyName, onBindCompany }: ChatProps) {
  const [messages, setMessages] = useState<{ role: 'user' | 'assistant'; content: string; verdict?: Verdict; time: string }[]>([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [lastResponse, setLastResponse] = useState<AskResponse | null>(null)
  const [showEvidence, setShowEvidence] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    inputRef.current?.focus()
  }, [])

  const handleAsk = useCallback(async (e: React.FormEvent) => {
    e.preventDefault()
    const q = input.trim()
    if (!q || !cin || loading) return

    setLoading(true)
    const userMsg = { role: 'user' as const, content: q, time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) }
    setMessages((prev) => [...prev, userMsg])
    setInput('')
    setLastResponse(null)

    try {
      const res: AskResponse = await askQuestion(sessionId, q)
      const botMsg = {
        role: 'assistant' as const,
        content: res.answer,
        verdict: res.verdict,
        time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
      }
      setMessages((prev) => [...prev, botMsg])
      setLastResponse(res)
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        {
          role: 'assistant',
          content: `Error: ${err instanceof Error ? err.message : 'Unknown error'}`,
          time: new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
        },
      ])
    } finally {
      setLoading(false)
    }
  }, [input, cin, loading, sessionId])

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleAsk(e as unknown as React.FormEvent)
    }
  }

  return (
    <div className="flex flex-col h-full bg-white border border-slate-200 rounded-xl shadow-sm">
      {/* Header */}
      <div className="px-4 py-3 border-b border-slate-100 bg-slate-50 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <div className="w-2 h-2 rounded-full bg-india-green" />
          <span className="text-sm font-medium text-slate-900">
            {companyName ? `Chatting about {companyName} ({cin})` : 'No company selected'}
          </span>
        </div>
        <button
          onClick={() => {
            const t = window.prompt('Enter NSE/BSE ticker (e.g. RELIANCE)')
            if (t) onBindCompany(t.trim().toUpperCase(), t.trim().toUpperCase())
          }}
          className="text-xs font-medium text-india-saffron hover:text-india-saffron/80 flex items-center gap-1"
        >
          <RefreshCw className="w-3 h-3" /> Change company
        </button>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {messages.length === 0 && (
          <div className="h-full flex flex-col items-center justify-center text-slate-400">
            <span className="text-sm">Ask a question about {companyName || 'the company'}</span>
            <p className="text-xs mt-1 text-slate-500">e.g. "What is RELIANCE's latest price?"</p>
          </div>
        )}
        {messages.map((m, i) => (
          <Message key={i} role={m.role} content={m.content} verdict={m.verdict} timestamp={m.time} />
        ))}
        {loading && (
          <Message role="assistant" content="Thinking…" timestamp={new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} />
        )}
      </div>

      {/* Evidence panel (shown for the last assistant answer) */}
      {lastResponse && showEvidence && (
        <div className="p-4 border-t border-slate-100 bg-slate-50">
          <div className="flex items-center gap-2 mb-2 text-xs text-slate-500 uppercase tracking-wider font-semibold cursor-pointer select-none" onClick={() => setShowEvidence(false)}>
            <span className="mr-1">▲</span> Hide evidence
          </div>
          <EvidencePanel
            citations={lastResponse.citations}
            evidence={lastResponse.evidence}
          />
        </div>
      )}
      {lastResponse && !showEvidence && (
        <div className="px-4 border-t border-slate-100 bg-slate-50">
          <button
            onClick={() => setShowEvidence(true)}
            className="w-full text-xs text-india-saffron font-medium hover:text-india-saffron/80 py-1.5"
          >
            Show evidence ({lastResponse.citations.length} cited)
          </button>
        </div>
      )}

      {/* Input */}
      <div className="p-3 border-t border-slate-100 bg-slate-50">
        <form onSubmit={handleAsk} className="flex gap-2">
          <input
            ref={inputRef}
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={`Ask about ${companyName || 'company'}…`}
            disabled={loading || !cin}
            className="flex-1 px-4 py-2.5 rounded-lg border border-slate-200 bg-white text-slate-900 focus:ring-2 focus:ring-india-saffron/50 focus:border-transparent outline-none disabled:opacity-50 transition-all"
          />
          <button
            type="submit"
            disabled={loading || !cin || !input.trim()}
            className="px-4 py-2.5 rounded-lg bg-india-saffron text-white font-medium disabled:opacity-50 disabled:cursor-not-allowed transition-colors hover:bg-india-saffron/90"
          >
            Ask
          </button>
        </form>
      </div>
    </div>
  )
}
