/**
 * GraphRAG Execution Types
 * 
 * Type definitions for the execution animation system.
 * These mirror the backend SSE event structure and execution trace.
 */

export type ExecutionStatus = 'pending' | 'running' | 'completed' | 'failed';

export type RunStatus = 'running' | 'success' | 'failed';

export interface ExecutionStep {
  id: string;
  name: string;
  status: ExecutionStatus;
  startedAt: number | null;
  completedAt: number | null;
  durationMs: number | null;
  message?: string;
  details?: Record<string, unknown>;
  children?: ExecutionStep[];
}

export interface ExecutionTrace {
  runId: string;
  question: string;
  company?: string;
  ticker?: string;
  status: RunStatus;
  startedAt: number;
  completedAt: number | null;
  durationMs: number | null;
  steps: ExecutionStep[];
  answer?: string;
  sources?: Source[];
  provenance?: ProvenanceEntry[];
  trace?: TraceEntry[];
  error?: string;
  stageLatencies?: Record<string, number>;
  graphMetrics?: GraphMetrics;
}

export interface Source {
  id: string;
  type: 'filing' | 'chunk' | 'entity' | 'relationship';
  title: string;
  excerpt?: string;
  metadata?: Record<string, unknown>;
}

export interface ProvenanceEntry {
  type: 'graph' | 'vector' | 'hybrid';
  query: string;
  results: number;
  latencyMs: number;
}

export interface TraceEntry {
  stage: string;
  message: string;
  timestamp: number;
  durationMs?: number;
  data?: Record<string, unknown>;
}

export interface GraphMetrics {
  nodesRetrieved: number;
  edgesTraversed: number;
  maxHopDepth: number;
  retrievalTimeMs: number;
}

export interface SSEEvent {
  type: 'status' | 'token' | 'done' | 'error';
  data: SSEStatusData | SSEResponseData | SSEErrorData;
}

export interface SSEStatusData {
  step: 'start' | 'routing' | 'fetching' | 'extracting' | 'stitching' | 'traversing' | 'traversal' | 'synthesis' | 'fallback' | 'ambiguous' | string;
  message?: string;
  ticker?: string;
  stages?: string[];
  elapsed_ms?: number;
  stage_duration_ms?: number;
  stage_complete?: boolean;
}

export interface SSEResponseData {
  status: 'complete' | 'error';
  answer?: string;
  provenance?: ProvenanceEntry[];
  graph?: {
    nodes: unknown[];
    edges: unknown[];
  };
  route?: 'KNOWN' | 'COLD_START';
  ticker?: string;
  stage_latencies_ms?: Record<string, number>;
  graph_metrics?: GraphMetrics;
  degraded?: boolean;
  latency_ms?: number;
  background_task_scheduled?: boolean;
  used_tags?: string[];
  tag_map?: Record<string, string>;
  verdict?: string;
  provenance_mix?: Record<string, number>;
  violations?: string[];
  error?: string;
}

export interface SSEErrorData {
  error: string;
  step?: string;
}

export interface RunDetailsState {
  currentRun: ExecutionTrace | null;
  isConnected: boolean;
  error: string | null;
}

export interface AnimationConfig {
  reducedMotion: boolean;
  autoExpandActiveStep: boolean;
  showDurations: boolean;
  maxVisibleSteps: number;
}