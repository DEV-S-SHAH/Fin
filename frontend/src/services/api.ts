import type {
  AskResponse,
  Company,
  GraphData,
} from '@/types'

const BASE = '/api'

export async function createSession(): Promise<{ session_id: string }> {
  const res = await fetch(`${BASE}/session`, { method: 'POST' })
  if (!res.ok) throw new Error('Failed to create session')
  return res.json()
}

export async function bindCompany(
  sessionId: string,
  ticker: string,
  companyName?: string,
): Promise<{ ok: boolean; cin?: string; company_name?: string; error?: string }> {
  const res = await fetch(`${BASE}/session/${sessionId}/bind`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ticker, company_name: companyName }),
  })
  if (!res.ok) throw new Error('Failed to bind company')
  return res.json()
}

export async function askQuestion(
  sessionId: string,
  question: string,
): Promise<AskResponse> {
  const res = await fetch(`${BASE}/session/${sessionId}/ask`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question }),
  })
  if (!res.ok) throw new Error('Failed to ask question')
  return res.json()
}

export async function getGraph(sessionId: string): Promise<GraphData> {
  const res = await fetch(`${BASE}/session/${sessionId}/graph`)
  if (!res.ok) throw new Error('Failed to load graph')
  return res.json()
}

export async function listCompanies(): Promise<Company[]> {
  const res = await fetch(`${BASE}/companies`)
  if (!res.ok) throw new Error('Failed to load companies')
  return res.json()
}

export async function deleteSession(sessionId: string): Promise<{ ok: boolean }> {
  const res = await fetch(`${BASE}/session/${sessionId}`, { method: 'DELETE' })
  if (!res.ok) throw new Error('Failed to delete session')
  return res.json()
}
