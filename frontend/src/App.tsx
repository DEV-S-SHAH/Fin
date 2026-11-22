import { useState, useEffect, useCallback } from 'react'
import { Menu, X, Phone, Watch, Globe, Monitor } from 'lucide-react'
import { createSession, bindCompany, listCompanies } from '@/services/api'
import type { Company, Session, AskResponse } from '@/types'
import { CompanySelector } from './components/CompanySelector'
import { Chat } from './components/Chat'
import { GraphView } from './components/GraphView'

function LogoIcon() {
  return (
    <svg
      width="28"
      height="28"
      viewBox="0 0 24 24"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className="text-india-saffron"
    >
      <path
        d="M12 2L3 10h5l2 4 2-4h5L12 2z"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M12 22l9-12h-5l-2 4-2-4h-5L12 22z"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

export default function App() {
  const [session, setSession] = useState<Session | null>(null)
  const [companies, setCompanies] = useState<Company[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [view, setView] = useState<'chat' | 'graph'>('chat')
  const [lastResponse, setLastResponse] = useState<AskResponse | null>(null)

  useEffect(() => {
    ;(async () => {
      setLoading(true)
      try {
        const s = await createSession()
        setSession({ session_id: s.session_id, cin: null, company_name: null })
        const c = await listCompanies()
        setCompanies(c)
      } catch (err) {
        setError(
          err instanceof Error
            ? `Backend not reachable: ${err.message}. Start the FastAPI server: uvicorn india_graph.api.main:app --reload --port 8000`
            : 'Unknown error',
        )
      } finally {
        setLoading(false)
      }
    })()
  }, [])

  const handleSelectCompany = useCallback(
    async (ticker: string, name: string) => {
      if (!session) return
      setLoading(true)
      setError(null)
      try {
        const res = await bindCompany(session.session_id, ticker, name)
        if (res.ok && res.cin) {
          setSession((prev) =>
            prev ? { ...prev, cin: res.cin ?? null, company_name: res.company_name ?? null } : prev,
          )
          setView('chat')
          setLastResponse(null) // Clear last response when switching companies
        } else {
          setError(res.error || `Could not bind ${ticker}`)
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to bind company')
      } finally {
        setLoading(false)
      }
    },
    [session],
  )

  if (loading) {
    return (
      <div className="flex h-screen items-center justify-center bg-slate-50">
        <div className="flex flex-col items-center gap-3">
          <div className="w-10 h-10 border-4 border-india-saffron border-t-transparent rounded-full animate-spin" />
          <p className="text-slate-600 font-medium">Initializing India Company Graph…</p>
        </div>
      </div>
    )
  }

  if (!session) return null

  return (
    <div className="flex h-screen bg-slate-50">
      {/* Sidebar */}
      <div
        className={`fixed inset-y-0 left-0 z-40 w-72 bg-white border-r border-slate-200 transform transition-transform duration-300 ${
          sidebarOpen ? 'translate-x-0' : '-translate-x-full'
        } lg:relative lg:translate-x-0 lg:static lg:inset-0`}
      >
        <div className="flex items-center justify-between px-6 py-4">
          <div className="flex items-center gap-2">
            <LogoIcon />
            <span className="font-bold text-slate-900 tracking-tight">India<br />Company<span className="text-india-saffron"> Graph</span></span>
          </div>
          <button
            onClick={() => setSidebarOpen(false)}
            className="lg:hidden p-1 text-slate-400 hover:text-slate-600"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        <div className="px-6 space-y-6">
          <div>
            <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-3">Session</h2>
            <div className="px-3 py-2 rounded-lg bg-slate-50 border border-slate-100">
              <div className="text-xs text-slate-500 font-mono mb-1">{session.session_id.slice(0, 8)}…</div>
              <div className="flex items-center gap-2 text-sm">
                {session.cin ? (
                  <>
                    <span className="w-2 h-2 rounded-full bg-green-500" />
                    <span className="text-slate-700 font-medium">Bound to {session.company_name}</span>
                  </>
                ) : (
                  <span className="text-slate-400">No company bound</span>
                )}
              </div>
            </div>
          </div>

          <CompanySelector
            companies={companies}
            onSelect={handleSelectCompany}
            loading={loading}
            error={error}
            boundTicker={session.cin ? companies.find((c) => c.ticker === session.company_name)?.ticker : null}
          />

          <div className="pt-4 border-t border-slate-100">
            <h2 className="text-xs font-semibold text-slate-400 uppercase tracking-wider mb-3">View</h2>
            <div className="flex gap-2">
              <button
                onClick={() => setView('chat')}
                className={`flex-1 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                  view === 'chat'
                    ? 'bg-india-saffron text-white'
                    : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
                }`}
              >
                Chat
              </button>
              <button
                onClick={() => setView('graph')}
                className={`flex-1 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                  view === 'graph'
                    ? 'bg-india-saffron text-white'
                    : 'bg-slate-100 text-slate-600 hover:bg-slate-200'
                }`}
              >
                Graph
              </button>
            </div>
          </div>
        </div>

        <div className="absolute bottom-0 left-0 right-0 p-4 border-t border-slate-100 bg-white">
          <button
            onClick={async () => {
              if (!session) return
              try {
                await fetch(`/api/session/${session.session_id}`, { method: 'DELETE' })
              } catch {}
              setSession(null)
            }}
            className="w-full px-4 py-2 text-left text-sm text-red-600 hover:bg-red-50 transition-colors"
          >
            <Phone className="w-4 h-4 mr-2" /> New session
          </button>
        </div>
      </div>

      {/* Overlay for mobile sidebar */}
      {sidebarOpen && !(
        <div className="fixed inset-0 z-30 bg-black/40 lg:hidden" onClick={() => setSidebarOpen(false)} />
      )}

      {/* Main content */}
      <div className="flex-1 flex flex-col lg:pl-72">
        <header className="px-6 py-4 bg-white border-b border-slate-200 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <button
              onClick={() => setSidebarOpen(!sidebarOpen)}
              className="lg:hidden p-2 text-slate-400 hover:text-slate-600"
            >
              <Menu className="w-5 h-5" />
            </button>
            <div className="flex items-center gap-2">
              <LogoIcon />
              <span className="font-bold text-slate-900 tracking-tight">India<br />Company<span className="text-india-saffron"> Graph</span></span>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <span className="hidden sm:block text-sm text-slate-500">Backend: localhost:8000</span>
            <button className="p-2 text-slate-400 hover:text-slate-600">
              <Watch className="w-4 h-4" />
            </button>
            <button className="p-2 text-slate-400 hover:text-slate-600">
              <Globe className="w-4 h-4" />
            </button>
            <button className="p-2 text-slate-400 hover:text-slate-600">
              <Monitor className="w-4 h-4" />
            </button>
          </div>
        </header>

        <main className="flex-1 overflow-y-auto p-6">
          {error && (
            <div className="mb-6 p-4 rounded-lg bg-red-50 border border-red-100 text-sm text-red-700">
              {error}
            </div>
          )}
          {view === 'chat' && (
            <Chat
              sessionId={session.session_id}
              cin={session.cin}
              companyName={session.company_name}
              onBindCompany={handleSelectCompany}
              onResponse={setLastResponse}
            />
          )}
          {view === 'graph' && (
            <div className="space-y-4">
              <h2 className="text-xl font-bold text-slate-900">Company Graph</h2>
              {session.cin ? (
                <GraphView sessionId={session.session_id} cin={session.cin} lastResponse={lastResponse} />
              ) : (
                <div className="p-6 rounded-lg bg-slate-50 border border-slate-100 text-center text-slate-400">
                  Select a company to view its graph.
                </div>
              )}
            </div>
          )}
        </main>
      </div>
    </div>
  )
}
