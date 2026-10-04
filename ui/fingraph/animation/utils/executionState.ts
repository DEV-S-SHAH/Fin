/**
 * Execution State Utilities
 * 
 * Helpers for managing execution step states, durations, and transitions.
 */

import type { ExecutionStep, ExecutionTrace, RunStatus, SSEEvent, SSEStatusData, SSEResponseData } from '../types/execution.js';

export const STEP_KEYS = [
  'routing',
  'fetching',
  'extraction',
  'stitching',
  'traversal',
  'synthesis'
] as const;

export type StepKey = typeof STEP_KEYS[number];

export const STEP_LABELS: Record<StepKey, string> = {
  routing: 'Entity Resolution',
  fetching: 'Filing Acquisition',
  extraction: 'Fact Extraction',
  stitching: 'Graph Stitching',
  traversal: 'Graph Retrieval',
  synthesis: 'Answer Synthesis'
};

export const STEP_DESCRIPTIONS: Record<StepKey, string> = {
  routing: 'Matching the question to a known entity in the graph',
  fetching: 'Fetching the latest SEC filing from EDGAR',
  extraction: 'Extracting financial facts and relationships from the filing',
  stitching: 'Stitching extracted facts onto the knowledge graph',
  traversal: 'Traversing the graph to retrieve relevant context',
  synthesis: 'Composing the final answer from retrieved evidence'
};

export const COLD_START_STEPS: StepKey[] = ['routing', 'fetching', 'extraction', 'stitching', 'traversal', 'synthesis'];
export const KNOWN_STEPS: StepKey[] = ['routing', 'traversal', 'synthesis'];

export function createExecutionTrace(question: string, route: 'KNOWN' | 'COLD_START', ticker?: string): ExecutionTrace {
  const steps = (route === 'COLD_START' ? COLD_START_STEPS : KNOWN_STEPS).map((key, index) => ({
    id: key,
    name: STEP_LABELS[key],
    status: 'pending' as const,
    startedAt: null,
    completedAt: null,
    durationMs: null,
    message: STEP_DESCRIPTIONS[key],
    details: {},
    children: []
  }));

  return {
    runId: `run-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`,
    question,
    ticker,
    company: ticker,
    status: 'running',
    startedAt: Date.now(),
    completedAt: null,
    durationMs: null,
    steps,
    stageLatencies: {}
  };
}

export function updateStepStatus(
  trace: ExecutionTrace,
  stepKey: string,
  status: 'pending' | 'running' | 'completed' | 'failed',
  message?: string,
  details?: Record<string, unknown>
): ExecutionTrace {
  const stepIndex = trace.steps.findIndex(s => s.id === stepKey);
  if (stepIndex === -1) return trace;

  const updatedSteps = [...trace.steps];
  const step = { ...updatedSteps[stepIndex] };
  const now = Date.now();

  switch (status) {
    case 'running':
      step.status = 'running';
      step.startedAt = now;
      if (message) step.message = message;
      break;
    case 'completed':
      step.status = 'completed';
      step.completedAt = now;
      if (step.startedAt) step.durationMs = now - step.startedAt;
      break;
    case 'failed':
      step.status = 'failed';
      step.completedAt = now;
      if (step.startedAt) step.durationMs = now - step.startedAt;
      break;
  }

  if (details) {
    step.details = { ...step.details, ...details };
    // If backend provided duration, use it
    if (details.backendDurationMs !== undefined) {
      step.durationMs = details.backendDurationMs;
    }
  }

  updatedSteps[stepIndex] = step;

  // Update overall status
  let runStatus: RunStatus = 'running';
  if (trace.steps.every(s => s.status === 'completed')) {
    runStatus = 'success';
  } else if (trace.steps.some(s => s.status === 'failed')) {
    runStatus = 'failed';
  }

  return {
    ...trace,
    steps: updatedSteps,
    status: runStatus,
    completedAt: runStatus !== 'running' ? now : null,
    durationMs: runStatus !== 'running' ? now - trace.startedAt : null
  };
}

export function processSSEEvent(trace: ExecutionTrace | null, event: SSEEvent): ExecutionTrace {
  if (!trace) return trace as ExecutionTrace;

  const { type, data } = event;

  if (type === 'status') {
    const statusData = data as SSEStatusData;
    
    // Handle plan initialization
    if (statusData.step === 'start' && statusData.stages) {
      // Plan already set during trace creation, just update ticker
      return {
        ...trace,
        ticker: statusData.ticker,
        company: statusData.ticker
      };
    }

    // Map backend step names to our step keys
    const stepMap: Record<string, StepKey> = {
      routing: 'routing',
      fetching: 'fetching',
      extracting: 'extraction',
      stitching: 'stitching',
      traversing: 'traversal',
      synthesis: 'synthesis',
      fallback: 'synthesis' // fallback goes to synthesis
    };

    const stepKey = stepMap[statusData.step];
    if (stepKey) {
      // Handle stage completion with actual backend duration
      if (statusData.stage_complete && statusData.stage_duration_ms !== undefined) {
        return updateStepStatus(trace, stepKey, 'completed', statusData.message, {
          backendDurationMs: statusData.stage_duration_ms
        });
      }
      
      // Stage started
      return updateStepStatus(trace, stepKey, 'running', statusData.message);
    }

    // Handle ambiguous
    if (statusData.step === 'ambiguous') {
      return {
        ...trace,
        status: 'failed',
        error: 'Question is ambiguous - cannot resolve to a specific entity',
        completedAt: Date.now(),
        durationMs: Date.now() - trace.startedAt
      };
    }
  }

  if (type === 'done') {
    const responseData = data as SSEResponseData;
    const completedTrace = { ...trace };
    
    // Mark all pending/running steps as completed
    let updatedSteps = completedTrace.steps.map(step => {
      if (step.status === 'pending' || step.status === 'running') {
        return {
          ...step,
          status: 'completed' as const,
          completedAt: Date.now(),
          durationMs: step.startedAt ? Date.now() - step.startedAt : 0
        };
      }
      return step;
    });

    // Add stage latencies if available
    if (responseData.stage_latencies_ms) {
      updatedSteps = updatedSteps.map(step => ({
        ...step,
        durationMs: responseData.stage_latencies_ms?.[step.id] ?? step.durationMs
      }));
    }

    return {
      ...completedTrace,
      steps: updatedSteps,
      status: responseData.status === 'error' ? 'failed' : 'success',
      completedAt: Date.now(),
      durationMs: responseData.latency_ms ?? (Date.now() - trace.startedAt),
      answer: responseData.answer,
      provenance: responseData.provenance,
      trace: responseData.graph ? [
        { stage: 'graph', message: 'Graph retrieved', timestamp: Date.now(), data: responseData.graph }
      ] : undefined,
      stageLatencies: responseData.stage_latencies_ms,
      graphMetrics: responseData.graph_metrics,
      error: responseData.error
    };
  }

  if (type === 'error') {
    const errorData = data as SSEErrorData;
    return {
      ...trace,
      status: 'failed',
      error: errorData.error,
      completedAt: Date.now(),
      durationMs: Date.now() - trace.startedAt
    };
  }

  return trace;
}

export function formatDuration(ms: number | null): string {
  if (ms === null || ms === undefined) return '—';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

export function formatTimestamp(ts: number): string {
  return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

export function formatDate(ts: number): string {
  return new Date(ts).toLocaleString([], { 
    year: 'numeric', 
    month: 'short', 
    day: 'numeric',
    hour: '2-digit', 
    minute: '2-digit', 
    second: '2-digit' 
  });
}

export function getStepIcon(status: ExecutionStatus): string {
  switch (status) {
    case 'completed': return '✓';
    case 'running': return '●';
    case 'failed': return '✕';
    case 'pending': return '○';
  }
}

export function getStepStatusClass(status: ExecutionStatus): string {
  switch (status) {
    case 'completed': return 'is-done';
    case 'running': return 'is-active';
    case 'failed': return 'is-failed';
    case 'pending': return 'is-pending';
  }
}