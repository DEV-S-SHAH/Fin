/**
 * WorkflowTimeline Component
 * 
 * Vertical execution timeline showing all workflow steps with expandable details.
 */

import { h } from 'preact';
import type { ExecutionTrace, ExecutionStep } from '../types/execution.js';
import { WorkflowStep } from './WorkflowStep.js';
import './WorkflowTimeline.css';

interface WorkflowTimelineProps {
  trace: ExecutionTrace;
  autoExpandActive?: boolean;
  onStepToggle?: (stepId: string, expanded: boolean) => void;
}

export function WorkflowTimeline({ trace, autoExpandActive = true, onStepToggle }: WorkflowTimelineProps) {
  const steps = trace.steps;
  
  if (!steps.length) {
    return (
      <div className="workflow-timeline__empty">
        <p>No execution steps recorded</p>
      </div>
    );
  }

  // Build enhanced steps with children from trace data
  const enhancedSteps = steps.map(step => enhanceStep(step, trace));

  return (
    <div className="workflow-timeline" role="list" aria-label="Execution workflow">
      {enhancedSteps.map((step, index) => (
        <WorkflowStep
          key={step.id}
          step={step}
          level={0}
          isLast={index === enhancedSteps.length - 1}
          autoExpand={autoExpandActive && step.status === 'running'}
          onToggle={onStepToggle}
        />
      ))}
      
      {/* Overall progress indicator */}
      <div className="workflow-timeline__progress" aria-hidden="true">
        <div 
          className="workflow-timeline__progress-fill" 
          style={{ width: `${getOverallProgress(trace)}%` } as any}
        />
      </div>
    </div>
  );
}

function enhanceStep(step: ExecutionStep, trace: ExecutionTrace): ExecutionStep {
  const children: ExecutionStep[] = [];
  
  // Add sub-steps based on step type and available data
  switch (step.id) {
    case 'routing':
      if (trace.ticker) {
        children.push({
          id: `${step.id}-company`,
          name: 'Company Resolved',
          status: 'completed',
          startedAt: step.startedAt,
          completedAt: step.completedAt,
          durationMs: 0,
          message: `Identified ${trace.company || trace.ticker} (${trace.ticker})`,
          details: { Ticker: trace.ticker, Company: trace.company || trace.ticker }
        });
      }
      break;
      
    case 'fetching':
      if (trace.stageLatencies?.fetching) {
        children.push({
          id: `${step.id}-sec`,
          name: 'SEC EDGAR Request',
          status: 'completed',
          startedAt: step.startedAt,
          completedAt: step.completedAt,
          durationMs: trace.stageLatencies.fetching,
          message: 'Fetched latest 10-K filing',
          details: { Source: 'EDGAR', Form: '10-K', Latency: `${trace.stageLatencies.fetching}ms` }
        });
      }
      break;
      
    case 'extraction':
      if (trace.stageLatencies?.extraction) {
        children.push({
          id: `${step.id}-llm`,
          name: 'LLM Triple Extraction',
          status: 'completed',
          startedAt: step.startedAt,
          completedAt: step.completedAt,
          durationMs: trace.stageLatencies.extraction,
          message: 'Extracted financial entities and relationships',
          details: { Model: 'ColdStartExtractor', Latency: `${trace.stageLatencies.extraction}ms` }
        });
      }
      break;
      
    case 'stitching':
      if (trace.stageLatencies?.stitching) {
        children.push({
          id: `${step.id}-overlay`,
          name: 'In-Memory Graph Stitching',
          status: 'completed',
          startedAt: step.startedAt,
          completedAt: step.completedAt,
          durationMs: trace.stageLatencies.stitching,
          message: 'Merged extracted facts into overlay graph',
          details: { Method: 'InMemoryOverlayGraph', Latency: `${trace.stageLatencies.stitching}ms` }
        });
      }
      break;
      
    case 'traversal':
      if (trace.graphMetrics) {
        children.push(
          {
            id: `${step.id}-traverse`,
            name: '2-Hop Graph Traversal',
            status: 'completed',
            startedAt: step.startedAt,
            completedAt: step.completedAt,
            durationMs: trace.stageLatencies?.traversal || step.durationMs,
            message: `Retrieved ${trace.graphMetrics.nodesRetrieved} nodes, ${trace.graphMetrics.edgesTraversed} edges`,
            details: { 
              'Max Hop Depth': trace.graphMetrics.maxHopDepth,
              'Nodes Retrieved': trace.graphMetrics.nodesRetrieved,
              'Edges Traversed': trace.graphMetrics.edgesTraversed,
              'Retrieval Time': `${trace.graphMetrics.retrievalTimeMs}ms`
            }
          },
          {
            id: `${step.id}-paths`,
            name: 'Graph Paths Found',
            status: 'completed',
            startedAt: step.startedAt,
            completedAt: step.completedAt,
            durationMs: 0,
            message: 'Identified relevant graph paths for context',
            details: { 
              'Path Types': 'Company → FinancialMetric → FiscalYear',
              'Filing Links': '10-K, 10-Q, 8-K'
            }
          }
        );
      }
      break;
      
    case 'synthesis':
      if (trace.answer) {
        const evidenceCount = trace.provenance?.length || 0;
        children.push(
          {
            id: `${step.id}-context`,
            name: 'Context Assembly',
            status: 'completed',
            startedAt: step.startedAt,
            completedAt: step.completedAt,
            durationMs: 0,
            message: `Assembled context from ${evidenceCount} evidence sources`,
            details: { 
              'Evidence Sources': evidenceCount,
              'Graph Facts': trace.graphMetrics?.nodesRetrieved || '—',
              'Context Tokens': '~8,000'
            }
          },
          {
            id: `${step.id}-llm`,
            name: 'LLM Answer Generation',
            status: 'completed',
            startedAt: step.startedAt,
            completedAt: step.completedAt,
            durationMs: trace.stageLatencies?.synthesis || step.durationMs,
            message: 'Generated answer with citations',
            details: { 
              'Model': 'Nemotron-3-Ultra',
              'Latency': trace.stageLatencies?.synthesis ? `${trace.stageLatencies.synthesis}ms` : '—',
              'Citations': trace.provenance?.length || 0
            }
          }
        );
      }
      break;
  }

  // Add provenance details if available
  if (trace.provenance && trace.provenance.length > 0 && step.id === 'synthesis') {
    children.push({
      id: `${step.id}-provenance`,
      name: 'Provenance Ledger',
      status: 'completed',
      startedAt: step.startedAt,
      completedAt: step.completedAt,
      durationMs: 0,
      message: `${trace.provenance.length} retrieval operations recorded`,
      details: trace.provenance.reduce((acc, p, i) => {
        acc[`Query ${i + 1}`] = `${p.type}: ${p.results} results (${p.latencyMs}ms)`;
        return acc;
      }, {} as Record<string, string>)
    });
  }

  return {
    ...step,
    children: children.length > 0 ? children : step.children
  };
}

function getOverallProgress(trace: ExecutionTrace): number {
  if (!trace.steps.length) return 0;
  const completed = trace.steps.filter(s => s.status === 'completed').length;
  const running = trace.steps.filter(s => s.status === 'running').length;
  // Count running as half-complete for visual progress
  return Math.round(((completed + running * 0.5) / trace.steps.length) * 100);
}

/**
 * Simple step list for compact views
 */
interface SimpleStepListProps {
  trace: ExecutionTrace;
}

export function SimpleStepList({ trace }: SimpleStepListProps) {
  return (
    <ol className="simple-step-list" role="list">
      {trace.steps.map((step, index) => (
        <li key={step.id} className={`simple-step-list__item ${getStepStatusClass(step.status)}`}>
          <span className="simple-step-list__icon">{getStepIcon(step.status)}</span>
          <span className="simple-step-list__name">{step.name}</span>
          <span className="simple-step-list__duration">{formatDuration(step.durationMs)}</span>
        </li>
      ))}
    </ol>
  );
}

// Re-export helpers
import { formatDuration, getStepIcon, getStepStatusClass } from '../utils/executionState.js';