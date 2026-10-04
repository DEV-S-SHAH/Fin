/**
 * RunDetails Component
 * 
 * Main container for the Run Details panel with General/Workflow tabs.
 */

import { useState, useEffect, useCallback } from 'preact/hooks';
import { h } from 'preact';
import type { ExecutionTrace } from '../types/execution.js';
import { RunHeader, RunHeaderMeta } from './RunHeader.js';
import { GeneralTab } from './GeneralTab.js';
import { WorkflowTimeline } from './WorkflowTimeline.js';
import './RunDetails.css';

interface RunDetailsProps {
  trace: ExecutionTrace | null;
  isOpen: boolean;
  onClose: () => void;
  onMinimize?: () => void;
}

export function RunDetails({ trace, isOpen, onClose, onMinimize }: RunDetailsProps) {
  const [activeTab, setActiveTab] = useState<'general' | 'workflow'>('workflow');
  const [expandedSteps, setExpandedSteps] = useState<Set<string>>(new Set());

  // Auto-switch to workflow tab when running
  useEffect(() => {
    if (trace && trace.status === 'running') {
      setActiveTab('workflow');
    }
  }, [trace?.status]);

  const handleStepToggle = useCallback((stepId: string, expanded: boolean) => {
    setExpandedSteps(prev => {
      const next = new Set(prev);
      if (expanded) next.add(stepId);
      else next.delete(stepId);
      return next;
    });
  }, []);

  if (!isOpen || !trace) return null;

  return (
    <div className="run-details" role="dialog" aria-modal="true" aria-labelledby="run-details-title">
      <div className="run-details__overlay" onClick={onClose} aria-hidden="true" />
      
      <div className="run-details__panel">
        <RunHeader
          trace={trace}
          activeTab={activeTab}
          onTabChange={setActiveTab}
          onClose={onClose}
        />
        
        <RunHeaderMeta trace={trace} />
        
        <div className="run-details__content">
          {activeTab === 'general' && (
            <GeneralTab trace={trace} />
          )}
          
          {activeTab === 'workflow' && (
            <div className="run-details__workflow" role="tabpanel" id="tabpanel-workflow" aria-labelledby="tab-workflow">
              <WorkflowTimeline
                trace={trace}
                autoExpandActive={true}
                onStepToggle={handleStepToggle}
              />
            </div>
          )}
        </div>

        {/* Footer with summary */}
        {trace.completedAt && (
          <footer className="run-details__footer">
            <div className="run-details__summary">
              <span className="run-details__summary-item">
                <strong>{trace.steps.filter(s => s.status === 'completed').length}</strong> completed
              </span>
              <span className="run-details__summary-item">
                <strong>{trace.steps.filter(s => s.status === 'failed').length}</strong> failed
              </span>
              <span className="run-details__summary-item">
                <strong>{formatTotalDuration(trace)}</strong> total
              </span>
            </div>
            {onMinimize && (
              <button className="run-details__minimize" onClick={onMinimize}>
                Minimize
              </button>
            )}
          </footer>
        )}
      </div>
    </div>
  );
}

function formatTotalDuration(trace: ExecutionTrace): string {
  if (!trace.durationMs) return '—';
  const ms = trace.durationMs;
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

/**
 * RunDetailsTrigger - Button to open run details from Ask the Graph
 */
interface RunDetailsTriggerProps {
  isRunning: boolean;
  trace: ExecutionTrace | null;
  onOpen: () => void;
}

export function RunDetailsTrigger({ isRunning, trace, onOpen }: RunDetailsTriggerProps) {
  if (!isRunning && !trace) return null;

  return (
    <button
      className={`run-details-trigger ${isRunning ? 'running' : ''} ${trace?.status === 'failed' ? 'failed' : ''}`}
      onClick={onOpen}
      aria-label={isRunning ? 'View execution details' : 'View last run details'}
    >
      <span className="run-details-trigger__icon">
        {isRunning ? (
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <circle cx="12" cy="12" r="10" stroke-dasharray="31.4 31.4" stroke-dashoffset="31.4" className="spin" />
          </svg>
        ) : trace?.status === 'failed' ? (
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path d="M6 6l12 12M18 6l-12 12" stroke-linecap="round"/>
          </svg>
        ) : (
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path d="M9 18l6-6-6-6" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        )}
      </span>
      <span className="run-details-trigger__text">
        {isRunning ? 'Running…' : trace?.status === 'success' ? 'Completed' : trace?.status === 'failed' ? 'Failed' : 'Details'}
      </span>
      <span className="run-details-trigger__duration">
        {trace?.durationMs ? `${(trace.durationMs / 1000).toFixed(1)}s` : ''}
      </span>
    </button>
  );
}