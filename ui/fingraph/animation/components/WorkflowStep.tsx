/**
 * WorkflowStep Component
 * 
 * An expandable execution step with nested sub-steps and details.
 * Supports pending, running, completed, and failed states.
 */

import { useState, useEffect } from 'preact/hooks';
import { h, type ComponentChildren } from 'preact';
import type { ExecutionStep } from '../types/execution.js';
import { formatDuration, getStepIcon, getStepStatusClass } from '../utils/executionState.js';
import './WorkflowStep.css';

interface WorkflowStepProps {
  step: ExecutionStep;
  level?: number;
  isLast?: boolean;
  autoExpand?: boolean;
  onToggle?: (stepId: string, expanded: boolean) => void;
}

export function WorkflowStep({
  step,
  level = 0,
  isLast = false,
  autoExpand = false,
  onToggle
}: WorkflowStepProps) {
  const [expanded, setExpanded] = useState(autoExpand || step.status === 'running');
  const [showChildren, setShowChildren] = useState(false);

  // Animate children visibility
  useEffect(() => {
    if (expanded && step.children && step.children.length > 0) {
      const timer = setTimeout(() => setShowChildren(true), 50);
      return () => clearTimeout(timer);
    } else {
      setShowChildren(false);
    }
  }, [expanded, step.children]);

  const handleToggle = (e: React.MouseEvent) => {
    e.stopPropagation();
    const nextExpanded = !expanded;
    setExpanded(nextExpanded);
    onToggle?.(step.id, nextExpanded);
  };

  const hasChildren = step.children && step.children.length > 0;
  const statusClass = getStepStatusClass(step.status);
  const icon = getStepIcon(step.status);
  const duration = formatDuration(step.durationMs);
  const indent = level * 24;

  return (
    <div className={`workflow-step ${statusClass} ${expanded ? 'expanded' : ''} ${level > 0 ? 'nested' : ''}`} style={{ '--indent': `${indent}px` } as any}>
      {/* Vertical connector line */}
      <div className="workflow-step__connector" style={{ '--is-last': isLast ? '1' : '0' } as any}>
        {level > 0 && <div className="workflow-step__vertical-line" />}
      </div>

      {/* Step content */}
      <div className="workflow-step__content" onClick={handleToggle}>
        {/* Step indicator */}
        <div className="workflow-step__indicator">
          <span className={`workflow-step__dot ${statusClass}`}>{icon}</span>
          {hasChildren && (
            <button 
              className="workflow-step__expand-toggle" 
              onClick={handleToggle}
              aria-expanded={expanded}
              aria-label={expanded ? 'Collapse' : 'Expand'}
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
                <path d="M6 9l6 6 6-6" stroke-linecap="round" stroke-linejoin="round"/>
              </svg>
            </button>
          )}
        </div>

        {/* Step label and message */}
        <div className="workflow-step__label-area">
          <div className="workflow-step__header">
            <span className="workflow-step__name">{step.name}</span>
            {step.message && <span className="workflow-step__message">{step.message}</span>}
          </div>
          
          {/* Step details (shown when expanded) */}
          {expanded && step.details && Object.keys(step.details).length > 0 && (
            <div className="workflow-step__details">
              {Object.entries(step.details).map(([key, value]) => (
                <div key={key} className="workflow-step__detail-row">
                  <span className="workflow-step__detail-key">{key}</span>
                  <span className="workflow-step__detail-value">{String(value)}</span>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Duration */}
        <div className="workflow-step__duration" aria-label={`Duration: ${duration}`}>
          {duration}
        </div>
      </div>

      {/* Nested children */}
      {showChildren && hasChildren && (
        <div className="workflow-step__children" role="group" aria-label={`${step.name} details`}>
          {step.children!.map((child, index) => (
            <WorkflowStep
              key={child.id}
              step={child}
              level={level + 1}
              isLast={index === step.children!.length - 1}
              autoExpand={false}
              onToggle={onToggle}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * WorkflowStepDetails - Renders detailed sub-step information
 * Used for the nested evidence/retrieval details
 */
interface WorkflowStepDetailsProps {
  title: string;
  items: Array<{
    label: string;
    value: string | number;
    type?: 'metric' | 'count' | 'duration' | 'text';
  }>;
  className?: string;
}

export function WorkflowStepDetails({ title, items, className = '' }: WorkflowStepDetailsProps) {
  return (
    <div className={`workflow-step-details ${className}`}>
      <h4 className="workflow-step-details__title">{title}</h4>
      <dl className="workflow-step-details__list">
        {items.map((item, index) => (
          <div key={index} className="workflow-step-details__item">
            <dt className="workflow-step-details__label">{item.label}</dt>
            <dd className={`workflow-step-details__value workflow-step-details__value--${item.type || 'text'}`}>
              {typeof item.value === 'number' ? item.value.toLocaleString() : item.value}
            </dd>
          </div>
        ))}
      </dl>
    </div>
  );
}