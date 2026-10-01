const API_BASE = "";

async function fetchJson<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      ...options?.headers,
    },
    credentials: "include",
  });

  if (!res.ok) {
    let error: string;
    try {
      error = (await res.json()).error || `HTTP ${res.status}`;
    } catch {
      error = `HTTP ${res.status}`;
    }
    throw new Error(error);
  }

  return res.json();
}

async function fetchStream(
  path: string,
  body: unknown,
  onEvent: (event: { type: string; data: unknown }) => void,
  signal?: AbortSignal
): Promise<void> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: {
      Accept: "text/event-stream",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
    credentials: "include",
    signal,
  });

  if (!res.ok) {
    let error: string;
    try {
      error = (await res.json()).error || `HTTP ${res.status}`;
    } catch {
      error = `HTTP ${res.status}`;
    }
    throw new Error(error);
  }

  if (!res.body) throw new Error("No response body");

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let split;
    while ((split = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);
      const parsed = parseFrame(frame);
      if (parsed) onEvent(parsed);
    }
  }
  const tail = parseFrame(buffer);
  if (tail) onEvent(tail);
}

function parseFrame(frame: string): { type: string; data: unknown } | null {
  let type = "message";
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) type = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
  }
  if (!dataLines.length) return null;
  try {
    return { type, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    return null;
  }
}

export const api = {
  auth: {
    config: () =>
      fetchJson<{ google: boolean; apple: boolean; tradingview: boolean }>(
        "/api/auth/config"
      ),
    session: () =>
      fetchJson<{ provider: string | null; authenticated: boolean }>(
        "/api/auth/session"
      ),
    login: (provider: string, token: string) =>
      fetchJson<void>("/api/auth/session", {
        method: "POST",
        body: JSON.stringify({ provider, token }),
      }),
    logout: () =>
      fetchJson<void>("/api/auth/logout", {
        method: "POST",
      }),
  },

  markets: () => fetchJson<{ markets: MarketQuote[] }>("/api/markets"),

  companies: () => fetchJson<{ companies: Company[] }>("/api/companies"),

  route: (question: string) =>
    fetchJson<{ route: RouteType; ticker: string | null }>(
      `/api/route?q=${encodeURIComponent(question)}`
    ),

  entities: (params: EntitySearchParams = {}) => {
    const searchParams = new URLSearchParams();
    if (params.q) searchParams.set("q", params.q);
    if (params.limit) searchParams.set("limit", String(params.limit));
    return fetchJson<{ entities: Entity[] }>(
      `/api/entities?${searchParams.toString()}`
    );
  },

  graph: (params: GraphParams = {}) => {
    const searchParams = new URLSearchParams();
    if (params.seed) searchParams.set("seed", params.seed);
    if (params.hops) searchParams.set("hops", String(params.hops));
    if (params.limit) searchParams.set("limit", String(params.limit));
    return fetchJson<GraphPayload>(`/api/graph?${searchParams.toString()}`);
  },

  rag: {
    ask: (question: string, options?: { stream?: boolean }) =>
      fetchJson<RAGResponse>("/api/ask", {
        method: "POST",
        body: JSON.stringify({ question, stream: options?.stream ?? false }),
      }),
    askStream: (
      question: string,
      onEvent: (event: { type: string; data: unknown }) => void,
      signal?: AbortSignal
    ) => fetchStream("/api/ask?stream=true", { question, stream: true }, onEvent, signal),
    state: () => fetchJson<{ model: string; backend: string }>("/api/rag"),
    saveState: (body: { model?: string; backend?: string; key?: string }) =>
      fetchJson<void>("/api/rag", {
        method: "POST",
        body: JSON.stringify(body),
      }),
  },

  reports: () => fetchJson<{ reports: Report[] }>("/api/reports"),
  report: (id: string) => fetchJson<Report>(`/api/reports/${encodeURIComponent(id)}`),

  stats: () => fetchJson<{ schema: string; nodes: number; edges: number; rag_model: string; rag_backend: string }>("/api/stats"),
};

export type MarketQuote = {
  ticker: string;
  price: number;
  change: number | null;
  pct: number | null;
};

export type Company = {
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
};

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

export interface Report {
  id: string;
  name: string;
  description: string;
  category: string;
  query_template: string;
  parameters: Record<string, unknown>;
}