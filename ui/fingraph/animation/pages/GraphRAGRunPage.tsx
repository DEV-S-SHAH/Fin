/**
 * GraphRAGRunPage - Main Animation Page
 * 
 * Standalone page for testing and viewing GraphRAG execution animations.
 * Accessible at /animation route.
 */

import { useState, useCallback, useEffect } from 'preact/hooks';
import { h } from 'preact';
import { useGraphRAGRun } from '../hooks/useGraphRAGRun.js';
import { RunDetails, RunDetailsTrigger } from '../components/RunDetails.js';
import './GraphRAGRunPage.css';

export function GraphRAGRunPage() {
  const [question, setQuestion] = useState('');
  const [route, setRoute] = useState<'KNOWN' | 'COLD_START'>('KNOWN');
  const [ticker, setTicker] = useState('');
  const [showDetails, setShowDetails] = useState(false);
  const [minimized, setMinimized] = useState(false);
  const [lastTrace, setLastTrace] = useState<ExecutionTrace | null>(null);
  
  const { trace, isRunning, isConnected, error, startRun, stopRun, clearRun } = useGraphRAGRun({
    onComplete: (completedTrace) => {
      setLastTrace(completedTrace);
      setShowDetails(true);
    },
    onError: (err) => {
      console.error('Run error:', err);
    }
  });

  // Show details when run starts
  useEffect(() => {
    if (isRunning) {
      setShowDetails(true);
      setMinimized(false);
    }
  }, [isRunning]);

  const handleSubmit = useCallback(async (e: Event) => {
    e.preventDefault();
    if (!question.trim()) return;
    
    clearRun();
    setShowDetails(true);
    setMinimized(false);
    await startRun(question.trim(), route, ticker.trim() || undefined);
  }, [question, route, ticker, startRun, clearRun]);

  const handleStop = useCallback(() => {
    stopRun();
  }, [stopRun]);

  const handleClose = useCallback(() => {
    setShowDetails(false);
  }, []);

  const handleMinimize = useCallback(() => {
    setMinimized(true);
  }, []);

  const currentTrace = trace || lastTrace;

  return (
    <div className="graphrag-run-page">
      <header className="graphrag-run-page__header">
        <div className="graphrag-run-page__brand">
          <span className="graphrag-run-page__logo" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
              <circle cx="12" cy="5.5" r="2.6" fill="var(--primary)" stroke="none"/>
              <circle cx="5.5" cy="17.5" r="2.6" fill="var(--primary-2)" stroke="none"/>
              <circle cx="18.5" cy="17.5" r="2.6" fill="var(--primary-3)" stroke="none"/>
              <path d="M12 8.1 5.5 15M12 8.1l6.5 6.9M8.1 17.5h7.8" opacity=".75" stroke="var(--primary)"/>
            </svg>
          </span>
          <span className="graphrag-run-page__title">GraphRAG Execution</span>
        </div>
        <div className="graphrag-run-page__status">
          {isRunning && (
            <span className="graphrag-run-page__status-badge running">
              <span className="graphrag-run-page__status-dot" />
              Running
            </span>
          )}
          {currentTrace?.status === 'success' && !isRunning && (
            <span className="graphrag-run-page__status-badge success">Success</span>
          )}
          {currentTrace?.status === 'failed' && !isRunning && (
            <span className="graphrag-run-page__status-badge failed">Failed</span>
          )}
          {!isRunning && !currentTrace && (
            <span className="graphrag-run-page__status-badge idle">Ready</span>
          )}
        </div>
      </header>

      <main className="graphrag-run-page__main">
        {/* Input Form */}
        <section className="graphrag-run-page__input-section" aria-labelledby="input-heading">
          <h2 id="input-heading" className="graphrag-run-page__section-title">Submit Question</h2>
          
          <form onSubmit={handleSubmit} className="graphrag-run-page__form">
            <div className="graphrag-run-page__field">
              <label htmlFor="question" className="graphrag-run-page__label">Question</label>
              <textarea
                id="question"
                className="graphrag-run-page__textarea"
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                placeholder="e.g. What was Apple's total net sales in fiscal 2025, and how much came from the Americas segment?"
                rows={3}
                disabled={isRunning}
                required
              />
            </div>

            <div className="graphrag-run-page__options">
              <div className="graphrag-run-page__field">
                <label htmlFor="route" className="graphrag-run-page__label">Route</label>
                <select
                  id="route"
                  className="graphrag-run-page__select"
                  value={route}
                  onChange={(e) => setRoute(e.target.value as 'KNOWN' | 'COLD_START')}
                  disabled={isRunning}
                >
                  <option value="KNOWN">Known Entity (routing → traversal → synthesis)</option>
                  <option value="COLD_START">Cold Start (full pipeline with SEC fetch)</option>
                </select>
              </div>

              <div className="graphrag-run-page__field">
                <label htmlFor="ticker" className="graphrag-run-page__label">Ticker (optional)</label>
                <input
                  id="ticker"
                  type="text"
                  className="graphrag-run-page__input"
                  value={ticker}
                  onChange={(e) => setTicker(e.target.value.toUpperCase())}
                  placeholder="AAPL, TSLA, MSFT…"
                  maxLength={5}
                  disabled={isRunning}
                />
              </div>
            </div>

            <div className="graphrag-run-page__actions">
              <button
                type="submit"
                className="graphrag-run-page__btn graphrag-run-page__btn--primary"
                disabled={isRunning || !question.trim()}
              >
                {isRunning ? 'Running…' : 'Run GraphRAG'}
              </button>
              
              {isRunning && (
                <button
                  type="button"
                  className="graphrag-run-page__btn graphrag-run-page__btn--ghost"
                  onClick={handleStop}
                >
                  Stop
                </button>
              )}

              {currentTrace && !isRunning && (
                <button
                  type="button"
                  className="graphrag-run-page__btn graphrag-run-page__btn--ghost"
                  onClick={() => {
                    setShowDetails(true);
                    setMinimized(false);
                  }}
                >
                  View Details
                </button>
              )}
            </div>
          </form>
        </section>

        {/* Run Details Panel (minimized) */}
        {minimized && currentTrace && (
          <div className="graphrag-run-page__minimized" onClick={() => setMinimized(false)}>
            <RunDetailsTrigger
              isRunning={isRunning}
              trace={currentTrace}
              onOpen={() => { setShowDetails(true); setMinimized(false); }}
            />
          </div>
        )}

        {/* Live Status Bar */}
        {isRunning && (
          <div className="graphrag-run-page__live-status" role="status" aria-live="polite">
            <div className="graphrag-run-page__live-indicator">
              <span className="graphrag-run-page__live-dot" />
              <span>Executing…</span>
            </div>
            {trace && (
              <div className="graphrag-run-page__live-steps">
                {trace.steps
                  .filter(s => s.status !== 'pending')
                  .slice(-3)
                  .map(step => (
                    <span key={step.id} className={`graphrag-run-page__live-step ${step.status}`}>
                      {step.name} {step.durationMs ? `(${step.durationMs}ms)` : ''}
                    </span>
                  ))}
              </div>
            )}
          </div>
        )}

        {/* Error Display */}
        {error && !isRunning && (
          <div className="graphrag-run-page__error" role="alert">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
              <circle cx="12" cy="12" r="10"/>
              <path d="M12 8v4M12 16h.01" stroke-linecap="round"/>
            </svg>
            <span>{error}</span>
          </div>
        )}
      </main>

      {/* Run Details Modal */}
      <RunDetails
        trace={currentTrace}
        isOpen={showDetails}
        onClose={handleClose}
        onMinimize={handleMinimize}
      />
    </div>
  );
}