/**
 * GeneralTab Component
 * 
 * Shows high-level run information in the General tab.
 */

import { h } from 'preact';
import type { ExecutionTrace } from '../types/execution.js';
import { formatDate, formatDuration } from '../utils/executionState.js';
import './GeneralTab.css';

interface GeneralTabProps {
  trace: ExecutionTrace;
}

export function GeneralTab({ trace }: GeneralTabProps) {
  if (!trace) return null;

  return (
    <div className="general-tab" role="tabpanel" id="tabpanel-general" aria-labelledby="tab-general">
      <dl className="general-tab__grid">
        {/* Run Identity */}
        <div className="general-tab__section">
          <dt className="general-tab__section-title">Run Identity</dt>
          <div className="general-tab__fields">
            <div className="general-tab__field">
              <span className="general-tab__label">Run ID</span>
              <code className="general-tab__value">{trace.runId}</code>
            </div>
            <div className="general-tab__field">
              <span className="general-tab__label">Status</span>
              <span className={`general-tab__value general-tab__value--status general-tab__value--${trace.status}`}>
                {trace.status.charAt(0).toUpperCase() + trace.status.slice(1)}
              </span>
            </div>
          </div>
        </div>

        {/* Question */}
        <div className="general-tab__section">
          <dt className="general-tab__section-title">Question</dt>
          <dd className="general-tab__question">{trace.question}</dd>
        </div>

        {/* Company */}
        {trace.ticker && (
          <div className="general-tab__section">
            <dt className="general-tab__section-title">Company</dt>
            <div className="general-tab__fields">
              <div className="general-tab__field">
                <span className="general-tab__label">Ticker</span>
                <span className="general-tab__value">{trace.ticker}</span>
              </div>
              {trace.company && (
                <div className="general-tab__field">
                  <span className="general-tab__label">Name</span>
                  <span className="general-tab__value">{trace.company}</span>
                </div>
              )}
            </div>
          </div>
        )}

        {/* Timing */}
        <div className="general-tab__section">
          <dt className="general-tab__section-title">Timing</dt>
          <div className="general-tab__fields">
            <div className="general-tab__field">
              <span className="general-tab__label">Started</span>
              <span className="general-tab__value">{formatDate(trace.startedAt)}</span>
            </div>
            {trace.completedAt && (
              <div className="general-tab__field">
                <span className="general-tab__label">Completed</span>
                <span className="general-tab__value">{formatDate(trace.completedAt)}</span>
              </div>
            )}
            <div className="general-tab__field">
              <span className="general-tab__label">Total Duration</span>
              <span className="general-tab__value general-tab__value--duration">{formatDuration(trace.durationMs)}</span>
            </div>
          </div>
        </div>

        {/* Route & Model */}
        <div className="general-tab__section">
          <dt className="general-tab__section-title">Execution</dt>
          <div className="general-tab__fields">
            <div className="general-tab__field">
              <span className="general-tab__label">Route</span>
              <span className="general-tab__value">
                {trace.stageLatencies && 'fetching' in trace.stageLatencies ? 'Cold Start' : 'Known Entity'}
              </span>
            </div>
            {trace.stageLatencies && (
              <div className="general-tab__field">
                <span className="general-tab__label">Stages Executed</span>
                <span className="general-tab__value">
                  {Object.keys(trace.stageLatencies).filter(k => trace.stageLatencies![k] > 0).length} / {Object.keys(trace.stageLatencies).length}
                </span>
              </div>
            )}
            {trace.graphMetrics && (
              <div className="general-tab__field">
                <span className="general-tab__label">Graph Nodes</span>
                <span className="general-tab__value">{trace.graphMetrics.nodesRetrieved.toLocaleString()}</span>
              </div>
            )}
            {trace.graphMetrics && (
              <div className="general-tab__field">
                <span className="general-tab__label">Graph Edges</span>
                <span className="general-tab__value">{trace.graphMetrics.edgesTraversed.toLocaleString()}</span>
              </div>
            )}
            {trace.graphMetrics && (
              <div className="general-tab__field">
                <span className="general-tab__label">Max Hop Depth</span>
                <span className="general-tab__value">{trace.graphMetrics.maxHopDepth}</span>
              </div>
            )}
          </div>
        </div>

        {/* Evidence & Sources */}
        {(trace.provenance && trace.provenance.length > 0) && (
          <div className="general-tab__section">
            <dt className="general-tab__section-title">Evidence & Retrieval</dt>
            <div className="general-tab__fields">
              <div className="general-tab__field">
                <span className="general-tab__label">Retrieval Operations</span>
                <span className="general-tab__value">{trace.provenance.length}</span>
              </div>
              <div className="general-tab__field">
                <span className="general-tab__label">Total Results</span>
                <span className="general-tab__value">
                  {trace.provenance.reduce((sum, p) => sum + p.results, 0).toLocaleString()}
                </span>
              </div>
              <div className="general-tab__field">
                <span className="general-tab__label">Total Latency</span>
                <span className="general-tab__value">
                  {trace.provenance.reduce((sum, p) => sum + p.latencyMs, 0)}ms
                </span>
              </div>
            </div>
            
            {trace.provenance.map((p, i) => (
              <div key={i} className="general-tab__provenance-item">
                <span className="general-tab__provenance-type">{p.type}</span>
                <span className="general-tab__provenance-query">{p.query}</span>
                <span className="general-tab__provenance-stats">
                  {p.results} results · {p.latencyMs}ms
                </span>
              </div>
            ))}
          </div>
        )}

        {/* Stage Latencies */}
        {trace.stageLatencies && (
          <div className="general-tab__section">
            <dt className="general-tab__section-title">Stage Latencies</dt>
            <div className="general-tab__fields general-tab__fields--stages">
              {Object.entries(trace.stageLatencies).map(([stage, ms]) => (
                <div key={stage} className={`general-tab__field general-tab__field--stage ${ms > 0 ? 'completed' : 'skipped'}`}>
                  <span className="general-tab__label">{stage.charAt(0).toUpperCase() + stage.slice(1)}</span>
                  <span className="general-tab__value">
                    {ms > 0 ? `${ms}ms` : 'skipped'}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Error */}
        {trace.error && (
          <div className="general-tab__section general-tab__section--error">
            <dt className="general-tab__section-title">Error</dt>
            <dd className="general-tab__error">{trace.error}</dd>
          </div>
        )}
      </dl>
    </div>
  );
}