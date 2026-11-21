import { Search, Plus, AlertCircle } from 'lucide-react'
import type { Company } from '@/types'
import { useState } from 'react'

type CompanySelectorProps = {
  companies: Company[]
  onSelect: (ticker: string, name: string) => void
  loading?: boolean
  error?: string | null
  boundTicker?: string | null
}

export function CompanySelector({
  companies,
  onSelect,
  loading = false,
  error = null,
  boundTicker = null,
}: CompanySelectorProps) {
  const [query, setQuery] = useState('')
  const [inputTicker, setInputTicker] = useState('')

  const filtered = companies.filter(
    (c) =>
      c.ticker.toLowerCase().includes(query.toLowerCase()) ||
      c.name.toLowerCase().includes(query.toLowerCase()),
  )

  const handleSelect = (ticker: string, name: string) => {
    setInputTicker(ticker)
    setQuery('')
    onSelect(ticker, name)
  }

  return (
    <div className="bg-white rounded-xl border border-slate-200 p-5 shadow-sm">
      <div className="flex items-center gap-2 mb-4">
        <Search className="w-4 h-4 text-india-saffron" />
        <h2 className="text-lg font-semibold text-slate-900">Select Company</h2>
      </div>

      {boundTicker && (
        <div className="mb-4 flex items-center gap-2 px-2 py-2 rounded-lg bg-green-50 border border-green-100">
          <Plus className="w-3 h-3 text-green-600" />
          <span className="text-sm text-green-800 font-medium">
            Bound to <span className="font-mono">{boundTicker}</span>
          </span>
        </div>
      )}

      <div className="relative">
        <input
          type="text"
          placeholder="Enter NSE/BSE ticker (e.g. RELIANCE)"
          value={inputTicker}
          onChange={(e) => setInputTicker(e.target.value)}
          className="w-full pl-9 pr-4 py-2.5 rounded-lg border border-slate-200 bg-slate-50 text-slate-900 focus:ring-2 focus:ring-india-saffron/50 focus:border-transparent outline-none transition-all"
        />
        <button
          onClick={() => {
            const t = inputTicker.trim().toUpperCase()
            if (!t) return
            const match = companies.find((c) => c.ticker === t)
            handleSelect(t, match ? match.name : t)
          }}
          disabled={loading || !inputTicker.trim()}
          className="absolute right-2 top-2 h-8 w-8 rounded-lg bg-india-saffron text-white flex items-center justify-center disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          <Search className="w-4 h-4" />
        </button>
      </div>

      <div className="mt-3 max-h-60 overflow-y-auto border border-slate-100 rounded-lg bg-white">
        {filtered.length === 0 ? (
          <div className="px-4 py-3 text-sm text-slate-500">No matching companies</div>
        ) : (
          filtered.slice(0, 10).map((c) => (
            <button
              key={c.ticker}
              onClick={() => handleSelect(c.ticker, c.name)}
              className="w-full flex items-center justify-between px-4 py-2.5 text-left hover:bg-slate-50 transition-colors border-b border-slate-50/50 last:border-0"
            >
              <div>
                <div className="text-sm font-medium text-slate-900">{c.ticker}</div>
                <div className="text-xs text-slate-500">{c.name}</div>
              </div>
              <span className="text-xs font-mono text-india-saffron">NSE/BSE</span>
            </button>
          ))
        )}
      </div>

      {error && (
        <div className="mt-3 p-3 rounded-lg bg-red-50 border border-red-100 text-sm text-red-700">
          <AlertCircle className="w-4 h-4 inline mr-2" />
          {error}
        </div>
      )}
    </div>
  )
}
