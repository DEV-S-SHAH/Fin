/**
 * RunHeader Component
 * 
 * Compact header showing run title, status badge, and tabs.
 */

import { h } from 'preact';
import type { ExecutionTrace, RunStatus } from '../types/execution.js';
import { formatDate, formatDuration } from '../utils/executionState.js';
import './RunHeader.css';

interface RunHeaderProps {
  trace: ExecutionTrace | null;
  activeTab: 'general' | 'workflow';
  onTabChange: (tab: 'general' | 'workflow') => void;
  onClose: () => void;
}

export function RunHeader({ trace, activeTab, onTabChange, onClose }: RunHeaderProps) {
  if (!trace) return null;

  const status = trace.status;
  const statusLabels: Record<RunStatus, string> = {
    running: 'Running',
    success: 'Success',
    failed: 'Failed'
  };

  return (
    <header className="run-header">
      <div className="run-header__left">
        <h1 className="run-header__title">Run Details</h1>
        <span className={`run-header__status run-header__status--${status}`}>
          {statusLabels[status]}
        </span>
      </div>

      <div className="run-header__center">
        <div className="run-header__tabs" role="tablist" aria-label="Run details view">
          <button
            role="tab"
            aria-selected={activeTab === 'general'}
            aria-controls="tabpanel-general"
            className={`run-header__tab ${activeTab === 'general' ? 'active' : ''}`}
            onClick={() => onTabChange('general')}
          >
            General
          </button>
          <button
            role="tab"
            aria-selected={activeTab === 'workflow'}
            aria-controls="tabpanel-workflow"
            className={`run-header__tab ${activeTab === 'workflow' ? 'active' : ''}`}
            onClick={() => onTabChange('workflow')}
          >
            Workflow
          </button>
        </div>
      </div>

      <div className="run-header__right">
        <button
          className="run-header__close"
          onClick={onClose}
          aria-label="Close run details"
          title="Close"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path d="M6 6l12 12M18 6l-12 12" stroke-linecap="round"/>
          </svg>
        </button>
      </div>
    </header>
  );
}

/**
 * RunHeaderMeta - Additional metadata shown below header when run is complete
 */
interface RunHeaderMetaProps {
  trace: ExecutionTrace;
}

export function RunHeaderMeta({ trace }: RunHeaderMetaProps) {
  if (!trace.completedAt) return null;

  return (
    <div className="run-header__meta">
      <dl className="run-header__meta-grid">
        <div className="run-header__meta-item">
          <dt>Run ID</dt>
          <dd><code>{trace.runId}</code></dd>
        </div>
        <div className="run-header__meta-item">
          <dt>Started</dt>
          <dd>{formatDate(trace.startedAt)}</dd>
        </div>
        <div className="run-header__meta-item">
          <dt>Completed</dt>
          <dd>{formatDate(trace.completedAt)}</dd>
        </div>
        <div className="run-header__meta-item">
          <dt>Duration</dt>
          <dd>{formatDuration(trace.durationMs)}</dd>
        </div>
        {trace.ticker && (
          <div className="run-header__meta-item">
            <dt>Company</dt>
            <dd>{trace.company || trace.ticker} ({trace.ticker})</dd>
          </div>
        )}
        {trace.stageLatencies && (
          <div className="run-header__meta-item">
            <dt>Route</dt>
            <dd>
              {Object.keys(trace.stageLatencies).includes('fetching') ? 'Cold Start' : 'Known Entity'}
            </dd>
          </div>
        )}
      </dl>
    </div>
  );
}