export type EntityType =
  | "Company"
  | "Filing"
  | "FinancialMetric"
  | "Segment"
  | "DisclosureEvent"
  | "Unspecified";

export interface Entity {
  id: string;
  name: string;
  entity_type: EntityType;
  label_hint?: string;
  description?: string;
  fiscal_year?: number | null;
  fiscal_period?: string | null;
  form_type?: string | null;
  period_end_date?: string | null;
}

export interface GraphNode extends Entity {
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
  r?: number;
  degree?: number;
}

export interface GraphEdge {
  source: string | GraphNode;
  target: string | GraphNode;
  relation: string;
  description?: string;
  curve?: number;
}

export interface GraphPayload {
  nodes: GraphNode[];
  edges: GraphEdge[];
  seeds?: string[];
}

export type RouteType = "KNOWN" | "COLD_START" | "AMBIGUOUS";

export interface RouteResponse {
  route: RouteType;
  ticker: string | null;
}

export type ProvenanceTag =
  | "STATED"
  | "DERIVED"
  | "INFERRED"
  | "EXTERNAL"
  | "GAP";

export interface ProvenanceGrade {
  tag: ProvenanceTag;
  confidence: number;
  explanation?: string;
}

export interface Citation {
  id: string;
  entity_id: string;
  entity_name: string;
  entity_type: EntityType;
  text: string;
  provenance: ProvenanceGrade;
  filing_id?: string;
  filing_form?: string;
  fiscal_year?: number;
  fiscal_period?: string;
}

export interface AnswerChunk {
  type: "text" | "citation" | "metric" | "table";
  content: string;
  citations?: Citation[];
  metadata?: Record<string, unknown>;
}

export interface RAGResponse {
  answer: string;
  chunks: AnswerChunk[];
  citations: Citation[];
  verdict: "SUFFICIENT" | "PARTIAL" | "INSUFFICIENT" | "UNCERTAIN";
  route: RouteType;
  ticker: string | null;
  trace?: TraceStep[];
  timings?: Record<string, number>;
}

export interface TraceStep {
  stage: string;
  name: string;
  status: "pending" | "active" | "completed" | "error";
  description: string;
  details?: string;
  data?: unknown;
  duration_ms?: number;
  started_at?: number;
  completed_at?: number;
}

export interface AskRequest {
  question: string;
  stream?: boolean;
  seed?: string;
  hops?: number;
  limit?: number;
}

export interface EntitySearchParams {
  q?: string;
  limit?: number;
}

export interface GraphParams {
  seed?: string;
  hops?: number;
  limit?: number;
}

export interface CompanySummary {
  ticker: string;
  name: string;
  filings: number;
  forms: string[];
  periods: Array<{
    form: string;
    fiscal_year: number | null;
    period: string | null;
    period_end: string | null;
  }>;
  latest: {
    form: string;
    fiscal_year: number | null;
    period: string | null;
    period_end: string | null;
  } | null;
}

export interface Session {
  provider: string | null;
  authenticated: boolean;
}

export interface MarketQuote {
  ticker: string;
  price: number;
  change: number | null;
  pct: number | null;
}