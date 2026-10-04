/**
 * useGraphRAGRun Hook
 * 
 * Manages the GraphRAG execution lifecycle via SSE streaming.
 * Provides real-time execution trace updates.
 */

import { useState, useCallback, useRef, useEffect } from 'preact/hooks';
import type { ExecutionTrace, SSEEvent, RunStatus } from '../types/execution.js';
import { createExecutionTrace, processSSEEvent } from '../utils/executionState.js';

interface UseGraphRAGRunOptions {
  onComplete?: (trace: ExecutionTrace) => void;
  onError?: (error: string) => void;
}

interface UseGraphRAGRunReturn {
  trace: ExecutionTrace | null;
  isRunning: boolean;
  isConnected: boolean;
  error: string | null;
  startRun: (question: string, route?: 'KNOWN' | 'COLD_START', ticker?: string) => Promise<void>;
  stopRun: () => void;
  clearRun: () => void;
}

export function useGraphRAGRun(options: UseGraphRAGRunOptions = {}): UseGraphRAGRunReturn {
  const [trace, setTrace] = useState<ExecutionTrace | null>(null);
  const [isRunning, setIsRunning] = useState(false);
  const [isConnected, setIsConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  
  const abortControllerRef = useRef<AbortController | null>(null);
  const eventSourceRef = useRef<EventSource | null>(null);

  const stopRun = useCallback(() => {
    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
      abortControllerRef.current = null;
    }
    if (eventSourceRef.current) {
      eventSourceRef.current.close();
      eventSourceRef.current = null;
    }
    setIsConnected(false);
    setIsRunning(false);
  }, []);

  const clearRun = useCallback(() => {
    stopRun();
    setTrace(null);
    setError(null);
  }, [stopRun]);

  const startRun = useCallback(async (question: string, route: 'KNOWN' | 'COLD_START' = 'KNOWN', ticker?: string) => {
    if (isRunning) return;
    
    setError(null);
    setIsRunning(true);
    
    // Create initial trace
    const initialTrace = createExecutionTrace(question, route, ticker);
    setTrace(initialTrace);

    abortControllerRef.current = new AbortController();
    
    try {
      const response = await fetch('/api/ask?stream=true', {
        method: 'POST',
        credentials: 'include',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'text/event-stream'
        },
        body: JSON.stringify({ question, stream: true }),
        signal: abortControllerRef.current.signal
      });

      if (!response.ok) {
        let detail = `HTTP ${response.status}`;
        try {
          const body = await response.json();
          if (body?.error) detail = body.error;
        } catch { /* not json */ }
        throw new Error(detail);
      }

      if (!response.body) throw new Error('Streaming not supported');

      setIsConnected(true);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        
        buffer += decoder.decode(value, { stream: true });
        
        let split;
        while ((split = buffer.indexOf('\n\n')) !== -1) {
          const frame = buffer.slice(0, split);
          buffer = buffer.slice(split + 2);
          const parsed = parseSSEFrame(frame);
          if (parsed) {
            setTrace(prev => prev ? processSSEEvent(prev, parsed) : null);
          }
        }
      }
      
      // Process any remaining buffer
      const tail = parseSSEFrame(buffer);
      if (tail) {
        setTrace(prev => prev ? processSSEEvent(prev, tail) : null);
      }

      // Get final trace
      const finalTrace = trace; // Will be updated by the last setTrace
      if (finalTrace && options.onComplete) {
        options.onComplete(finalTrace);
      }
      
    } catch (err) {
      if (err instanceof Error && err.name === 'AbortError') {
        // Expected on cancel
        return;
      }
      const errorMessage = err instanceof Error ? err.message : 'Unknown error';
      setError(errorMessage);
      if (trace) {
        setTrace({
          ...trace,
          status: 'failed',
          error: errorMessage,
          completedAt: Date.now(),
          durationMs: Date.now() - trace.startedAt
        });
      }
      if (options.onError) options.onError(errorMessage);
    } finally {
      setIsConnected(false);
      setIsRunning(false);
      abortControllerRef.current = null;
    }
  }, [isRunning, options]);

  return {
    trace,
    isRunning,
    isConnected,
    error,
    startRun,
    stopRun,
    clearRun
  };
}

function parseSSEFrame(frame: string): SSEEvent | null {
  let type = 'message';
  const dataLines: string[] = [];
  
  for (const line of frame.split('\n')) {
    if (line.startsWith('event:')) {
      type = line.slice(6).trim();
    } else if (line.startsWith('data:')) {
      dataLines.push(line.slice(5).replace(/^ /, ''));
    }
  }
  
  if (!dataLines.length) return null;
  
  try {
    return { type, data: JSON.parse(dataLines.join('\n')) };
  } catch {
    return null;
  }
}

/**
 * Hook for polling execution status (fallback when SSE not available)
 */
export function useExecutionPolling(runId: string | null, intervalMs = 1000) {
  const [trace, setTrace] = useState<ExecutionTrace | null>(null);
  const [isPolling, setIsPolling] = useState(false);

  useEffect(() => {
    if (!runId) return;
    
    setIsPolling(true);
    const controller = new AbortController();
    
    const poll = async () => {
      try {
        const response = await fetch(`/api/runs/${runId}`, {
          signal: controller.signal,
          credentials: 'include'
        });
        if (response.ok) {
          const data = await response.json();
          setTrace(data);
          if (data.status !== 'running') {
            setIsPolling(false);
          }
        }
      } catch {
        // Ignore polling errors
      }
    };

    const interval = setInterval(poll, intervalMs);
    poll(); // Initial fetch
    
    return () => {
      clearInterval(interval);
      controller.abort();
      setIsPolling(false);
    };
  }, [runId, intervalMs]);

  return { trace, isPolling };
}