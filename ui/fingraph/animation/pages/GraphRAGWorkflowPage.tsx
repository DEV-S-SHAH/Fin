/**
 * GraphRAGWorkflowPage - Embeddable Workflow View
 * 
 * A standalone workflow timeline component that can be embedded
 * in the Ask the Graph UI or used independently.
 */

import { h } from 'preact';
import type { ExecutionTrace } from '../types/execution.js';
import { WorkflowTimeline } from '../components/WorkflowTimeline.js';
import './GraphRAGWorkflowPage.css';

interface GraphRAGWorkflowPageProps {
  trace: ExecutionTrace | null;
  compact?: boolean;
  onStepToggle?: (stepId: string, expanded: boolean) => void;
}

export function GraphRAGWorkflowPage({ trace, compact = false, onStepToggle }: GraphRAGWorkflowPageProps) {
  if (!trace) {
    return (
      <div className={`graphrag-workflow-page ${compact ? 'compact' : ''}`}>
        <div className="graphrag-workflow-page__empty">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">
            <path d="M9 12l2 2 4-4M21 12c0 4.97-4.03 9-9 9S3 16.97 3 12 7.03 3 12 3s9 4.03 9 9" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
          <p>No execution trace available</p>
          <span className="graphrag-workflow-page__hint">Run a question to see the workflow</span>
        </div>
      </div>
    );
  }

  return (
    <div className={`graphrag-workflow-page ${compact ? 'compact' : ''}`}>
      {!compact && (
        <header className="graphrag-workflow-page__header">
          <h3 className="graphrag-workflow-page__title">Workflow</h3>
          <div className="graphrag-workflow-page__status">
            <span className={`graphrag-workflow-page__badge ${trace.status}`}>
              {trace.status.charAt(0).toUpperCase() + trace.status.slice(1)}
            </span>
          </div>
        </header>
      )}
      
      <div className="graphrag-workflow-page__timeline">
        <WorkflowTimeline
          trace={trace}
          autoExpandActive={!compact}
          onStepToggle={onStepToggle}
        />
      </div>
      
      {!compact && trace.completedAt && (
        <footer className="graphrag-workflow-page__footer">
          <div className="graphrag-workflow-page__summary">
            <span><strong>{trace.steps.filter(s => s.status === 'completed').length}</strong> completed</span>
            <span><strong>{trace.steps.filter(s => s.status === 'failed').length}</strong> failed</span>
            <span><strong>{formatTotalDuration(trace)}</strong> total</span>
          </div>
        </footer>
      )}
    </div>
  );
}

function formatTotalDuration(trace: ExecutionTrace): string {
  if (!trace.durationMs) return '—';
  const ms = trace.durationMs;
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}