export type Verdict = 'SUPPORTED' | 'QUALIFIED' | 'REFUSED'

export interface Citation {
  tag: string
  text: string
  source: string
}

export interface EvidenceItem {
  tag: string
  text: string
  provenance: string
  source: string
  kind: string
}

export interface AskResponse {
  answer: string
  verdict: Verdict
  citations: Citation[]
  evidence: EvidenceItem[]
}

export interface GraphNode {
  id: string
  type: string
  name?: string
  label?: string
  cin?: string
  ticker?: string
  metric_name?: string
  value?: string | number
  form_type?: string
  filing_date?: string
  fiscal_year?: string | number
  source?: string
  [key: string]: unknown
}

export interface GraphEdge {
  source: string
  target: string
  rel: string
  description?: string
  value?: string | number
  scale?: string
  period?: string
}

export interface GraphData {
  cin: string
  company_name: string
  nodes: GraphNode[]
  edges: GraphEdge[]
}

export interface Company {
  ticker: string
  name: string
}

export interface Session {
  session_id: string
  cin: string | null
  company_name: string | null
}
