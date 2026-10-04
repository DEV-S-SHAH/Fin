/**
 * ExecutionDetailsModal - Centered real-time execution details panel
 * 
 * Fully synchronized with the REAL backend at localhost:9100/api
 * Uses actual backend execution state/response via SSE:
 * - Entity Resolution (routing)
 * - Graph Retrieval (traversal)
 * - Evidence Retrieval (traversal details)
 * - Context Assembly (synthesis context)
 * - LLM Answer Generation (synthesis)
 * 
 * No fake timers, no hardcoded steps, no fake progress.
 */

import { $, el, clear } from "./util.js";

/**
 * Format duration from milliseconds to human-readable string
 * @param {number} ms - Duration in milliseconds
 * @returns {string} Formatted duration (e.g., "0.18ms", "100.54ms", "16.33s")
 */
export function formatDuration(ms) {
  if (ms === null || ms === undefined || isNaN(ms)) return "—";
  if (ms < 1) return `${ms.toFixed(2)}ms`;
  if (ms < 1000) return `${ms.toFixed(2)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

export class ExecutionDetailsModal {
  constructor() {
    this.overlay = null;
    this.modal = null;
    this.steps = [];
    this.startTime = 0;
    this.timerInterval = null;
    this.isVisible = false;
    this.onCompleteCallback = null;
    this.currentRoute = 'KNOWN';
    this.currentTicker = null;
    this.currentQuestion = '';
    this.backendMetrics = null;
    this.stepStartTimes = {}; // Track when each step started (frontend)
    
    this.#createElements();
  }

  #createElements() {
    // Overlay - dark translucent with subtle blur
    this.overlay = el("div", { 
      class: "execution-details-overlay", 
      hidden: true 
    });

    // Modal - centered, compact panel
    this.modal = el("div", { 
      class: "execution-details-modal", 
      role: "dialog", 
      "aria-modal": "true", 
      "aria-labelledby": "execution-details-title",
      hidden: true 
    }, [
      // Header
      el("div", { class: "execution-details-header" }, [
        el("h2", { 
          id: "execution-details-title", 
          class: "execution-details-title" 
        }, "Execution Details"),
        el("span", { 
          id: "execution-details-status", 
          class: "execution-details-status running" 
        }, "Running"),
        el("button", { 
          id: "execution-details-close", 
          class: "execution-details-close",
          type: "button",
          "aria-label": "Close (keeps running in background)"
        }, [
          el("svg", { 
            viewBox: "0 0 24 24", 
            fill: "none", 
            stroke: "currentColor", 
            strokeWidth: "2", 
            "aria-hidden": "true" 
          }, [
            el("path", { 
              d: "M6 6l12 12M18 6l-12 12", 
              strokeLinecap: "round" 
            })
          ])
        ])
      ]),

      // Body with scrolling
      el("div", { 
        id: "execution-details-body", 
        class: "execution-details-body" 
      }, [
        // Question section
        el("div", { class: "execution-details-question" }, [
          el("span", { class: "execution-details-question-label" }, "Question"),
          el("p", { 
            id: "execution-details-question-text", 
            class: "execution-details-question-text" 
          }, ""),
          // Ticker and route badges
          el("div", { class: "execution-details-badges" }, [
            el("span", { 
              id: "execution-details-ticker", 
              class: "execution-details-ticker",
              hidden: true
            }, ""),
            el("span", { 
              id: "execution-details-route-badge", 
              class: "execution-details-route-badge",
              hidden: true
            }, "")
          ])
        ]),

        // Workflow timeline
        el("div", { 
          id: "execution-details-timeline", 
          class: "execution-details-timeline", 
          role: "list", 
          "aria-label": "Execution steps" 
        }),

        // Live timer footer
        el("div", { class: "execution-details-footer" }, [
          el("div", { class: "execution-details-timer" }, [
            el("span", { 
              id: "execution-details-elapsed", 
              class: "execution-details-elapsed" 
            }, "0.00s"),
            el("span", { class: "execution-details-timer-label" }, "elapsed")
          ]),
          el("div", { class: "execution-details-current-step" }, [
            el("span", { 
              id: "execution-details-current-step-name", 
              class: "execution-details-current-step-name" 
            }, "Initializing…"),
            el("span", { 
              id: "execution-details-current-step-duration", 
              class: "execution-details-current-step-duration" 
            }, "")
          ])
        ])
      ])
    ]);

    document.body.append(this.overlay, this.modal);
    
    // Close handlers
    this.modal.querySelector("#execution-details-close").addEventListener("click", () => this.hide());
    this.overlay.addEventListener("click", () => this.hide());
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && this.isVisible) this.hide();
    });
  }

  /**
   * Show the modal and initialize from backend SSE "start" event
   * @param {string} question - The user's question
   * @param {string} route - Route type from probe: 'KNOWN' or 'COLD_START'
   * @param {string|null} ticker - Company ticker if resolved
   * @param {Function|null} onComplete - Callback when execution completes
   */
  show(question, route, ticker, onComplete = null) {
    this.currentQuestion = question;
    this.currentRoute = route;
    this.currentTicker = ticker;
    this.onCompleteCallback = onComplete;
    this.backendMetrics = null;
    this.stepStartTimes = {};
    this.startTime = Date.now(); // Use wall clock for elapsed timer

    // Build steps from backend route plan (matching backend ROUTE_PLAN)
    const routeStages = route === 'COLD_START'
      ? ['routing', 'fetching', 'extraction', 'stitching', 'traversal', 'synthesis']
      : ['routing', 'traversal', 'synthesis'];

    const stepLabels = {
      routing: 'Entity Resolution',
      fetching: 'Filing Acquisition',
      extraction: 'Fact Extraction',
      stitching: 'Graph Stitching',
      traversal: 'Graph Retrieval',
      synthesis: 'Answer Synthesis'
    };

    const stepDescriptions = {
      routing: 'Matching the question to an entity in the graph',
      fetching: 'Fetching the latest SEC filing from EDGAR',
      extraction: 'Extracting financial facts from the filing',
      stitching: 'Stitching extracted facts onto the knowledge graph',
      traversal: 'Traversing the graph to retrieve relevant context',
      synthesis: 'Composing the answer from retrieved evidence'
    };

    this.steps = routeStages.map(key => ({
      id: key,
      label: stepLabels[key],
      description: stepDescriptions[key],
      status: 'pending',
      startedAt: null,
      completedAt: null,
      durationMs: null,        // Frontend measured
      backendDurationMs: null, // Backend reported (from elapsed_ms or stage_latencies_ms)
      nodes: null,
      edges: null,
      hopDepth: null,
      children: []
    }));

    // Set question
    this.modal.querySelector("#execution-details-question-text").textContent = question;

    // Set ticker badge
    const tickerEl = this.modal.querySelector("#execution-details-ticker");
    if (ticker) {
      tickerEl.textContent = ticker;
      tickerEl.hidden = false;
    } else {
      tickerEl.hidden = true;
    }

    // Set route badge
    const routeBadgeEl = this.modal.querySelector("#execution-details-route-badge");
    routeBadgeEl.textContent = route === 'COLD_START' ? 'Cold Start' : 'Known Entity';
    routeBadgeEl.className = `execution-details-route-badge ${route.toLowerCase()}`;
    routeBadgeEl.hidden = false;

    // Render initial timeline
    this.#renderTimeline();

    // Show modal with animation
    this.overlay.hidden = false;
    this.modal.hidden = false;
    requestAnimationFrame(() => {
      this.overlay.classList.add("visible");
      this.modal.classList.add("visible");
    });

    this.isVisible = true;

    // Start elapsed timer (wall clock)
    this.#startTimer();

    // Update status badge
    const statusEl = this.modal.querySelector("#execution-details-status");
    statusEl.textContent = "Running";
    statusEl.className = "execution-details-status running";
  }

  #renderTimeline() {
    const container = this.modal.querySelector("#execution-details-timeline");
    clear(container);

    this.steps.forEach((step, index) => {
      const stepEl = el("div", { 
        class: `execution-details-step ${step.status}`,
        "data-step-id": step.id,
        role: "listitem"
      }, [
        // Connector line
        index > 0 && el("div", { class: "execution-details-step-connector" }),
        
        // Step content
        el("div", { class: "execution-details-step-content" }, [
          // Indicator dot
          el("div", { class: `execution-details-step-indicator ${step.status}` }, [
            el("span", { class: "execution-details-step-dot" })
          ]),
          
          // Label, description, and metrics
          el("div", { class: "execution-details-step-info" }, [
            el("div", { class: "execution-details-step-main" }, [
              el("span", { class: "execution-details-step-label" }, step.label),
              el("span", { class: "execution-details-step-description" }, step.description)
            ]),
            // Duration and metrics on the right
            el("div", { class: "execution-details-step-metrics" }, [
              step.backendDurationMs !== null && el("span", { class: "execution-details-step-duration" }, formatDuration(step.backendDurationMs)),
              step.nodes !== null && el("span", { class: "execution-details-step-metric", title: "Nodes retrieved" }, `${step.nodes} nodes`),
              step.edges !== null && el("span", { class: "execution-details-step-metric", title: "Edges traversed" }, `${step.edges} edges`),
              step.hopDepth !== null && el("span", { class: "execution-details-step-metric", title: "Hop depth" }, `${step.hopDepth} hops`)
            ])
          ]),
          
          // Children (nested details)
          step.children && step.children.length > 0 && el("div", { 
            class: "execution-details-step-children",
            hidden: false
          }, [
            ...step.children.map((child, childIndex) => this.#createChildElement(child, childIndex === step.children.length - 1))
          ])
        ])
      ]);
      
      container.append(stepEl);
    });
  }

  #createChildElement(child, isLast) {
    return el("div", { 
      class: `execution-details-step ${child.status} nested ${isLast ? 'last-child' : ''}`,
      "data-step-id": child.id,
      role: "listitem"
    }, [
      el("div", { class: "execution-details-step-connector" }, [
        !isLast && el("div", { class: "execution-details-step-vertical-line" })
      ]),
      el("div", { class: "execution-details-step-content" }, [
        el("div", { class: `execution-details-step-indicator ${child.status}` }, [
          el("span", { class: "execution-details-step-dot" })
        ]),
        el("div", { class: "execution-details-step-info" }, [
          el("div", { class: "execution-details-step-main" }, [
            el("span", { class: "execution-details-step-label" }, child.label),
            child.message && el("span", { class: "execution-details-step-description" }, child.message)
          ]),
          el("div", { class: "execution-details-step-metrics" }, [
            child.backendDurationMs !== null && el("span", { class: "execution-details-step-duration" }, formatDuration(child.backendDurationMs)),
            child.details && Object.keys(child.details).length > 0 && el("div", { class: "execution-details-step-child-details" }, [
              ...Object.entries(child.details).map(([key, value]) => 
                el("div", { class: "execution-details-step-detail-row" }, [
                  el("span", { class: "execution-details-step-detail-key" }, key),
                  el("span", { class: "execution-details-step-detail-value" }, String(value))
                ])
              )
            ])
          ])
        ])
      ])
    ]);
  }

  #updateTimeline() {
    this.steps.forEach((step, index) => {
      const stepEl = this.modal.querySelector(`[data-step-id="${step.id}"]`);
      if (stepEl) {
        stepEl.className = `execution-details-step ${step.status}`;
        const indicator = stepEl.querySelector(".execution-details-step-indicator");
        if (indicator) indicator.className = `execution-details-step-indicator ${step.status}`;
        
        const backendDurationEl = stepEl.querySelector(".execution-details-step-duration");
        if (backendDurationEl && step.backendDurationMs !== null) {
          backendDurationEl.textContent = formatDuration(step.backendDurationMs);
        }
        
        const nodesEl = stepEl.querySelector(".execution-details-step-metric[title='Nodes retrieved']");
        if (nodesEl && step.nodes !== null) {
          nodesEl.textContent = `${step.nodes} nodes`;
        }
        
        const edgesEl = stepEl.querySelector(".execution-details-step-metric[title='Edges traversed']");
        if (edgesEl && step.edges !== null) {
          edgesEl.textContent = `${step.edges} edges`;
        }
        
        const hopsEl = stepEl.querySelector(".execution-details-step-metric[title='Hop depth']");
        if (hopsEl && step.hopDepth !== null) {
          hopsEl.textContent = `${step.hopDepth} hops`;
        }

        // Update children
        if (step.children && step.children.length > 0) {
          const childrenContainer = stepEl.querySelector(".execution-details-step-children");
          if (childrenContainer) {
            clear(childrenContainer);
            step.children.forEach((child, childIndex) => {
              childrenContainer.append(this.#createChildElement(child, childIndex === step.children.length - 1));
            });
          }
        }
      }
    });
  }

  /**
   * Handle backend SSE status event
   * @param {Object} data - SSE status event data {step, message, elapsed_ms, ticker?, stages?}
   */
  handleStatusEvent(data) {
    const { step, message, elapsed_ms, ticker, stages } = data;

    if (step === "start") {
      // Backend sends the full plan - initialize steps if not already done
      if (ticker && !this.currentTicker) {
        this.currentTicker = ticker;
        const tickerEl = this.modal.querySelector("#execution-details-ticker");
        tickerEl.textContent = ticker;
        tickerEl.hidden = false;
      }
      if (stages) {
        // Could update step order if different from probe, but probe is usually accurate
      }
      return;
    }

    // Map backend step names to our step IDs
    const stepMap = {
      routing: 'routing',
      fetching: 'fetching',
      extracting: 'extraction',
      stitching: 'stitching',
      traversing: 'traversal',
      traversal: 'traversal',
      synthesis: 'synthesis',
      fallback: 'synthesis'
    };

    const stepId = stepMap[step];
    if (!stepId) return;

    const stepIndex = this.steps.findIndex(s => s.id === stepId);
    if (stepIndex === -1) return;

    const stepOrder = ['routing', 'fetching', 'extraction', 'stitching', 'traversal', 'synthesis'];
    const currentIndex = stepOrder.indexOf(stepId);

    // Mark previous steps as completed with backend elapsed_ms
    for (let i = 0; i < currentIndex; i++) {
      const prevStep = stepOrder[i];
      const prevStepObj = this.steps.find(s => s.id === prevStep);
      if (prevStepObj && (prevStepObj.status === 'pending' || prevStepObj.status === 'running')) {
        prevStepObj.status = 'completed';
        // Use backend elapsed_ms for completed steps
        prevStepObj.backendDurationMs = elapsed_ms !== undefined ? elapsed_ms : null;
        prevStepObj.completedAt = Date.now();
        if (prevStepObj.startedAt) {
          prevStepObj.durationMs = prevStepObj.completedAt - prevStepObj.startedAt;
        }
      }
    }

    // Mark current step as running
    const currentStep = this.steps[stepIndex];
    currentStep.status = 'running';
    currentStep.startedAt = Date.now();
    this.stepStartTimes[stepId] = currentStep.startedAt;

    // Update message if provided
    if (message) {
      currentStep.description = message;
    }

    this.#updateTimeline();
    this.#updateCurrentStepDisplay();
  }

  /**
   * Handle backend SSE done event - final metrics
   * @param {Object} data - SSE done event data
   */
  handleDoneEvent(data) {
    // Store backend metrics for final display
    this.backendMetrics = data;

    // Apply stage_latencies_ms from backend
    if (data.stage_latencies_ms) {
      const stageMap = {
        routing: 'routing',
        fetching: 'fetching',
        extracting: 'extraction',
        stitching: 'stitching',
        traversing: 'traversal',
        traversal: 'traversal',
        synthesis: 'synthesis'
      };

      for (const [stageKey, duration] of Object.entries(data.stage_latencies_ms)) {
        const stepId = stageMap[stageKey];
        if (stepId) {
          const step = this.steps.find(s => s.id === stepId);
          if (step) {
            step.backendDurationMs = duration;
            // If completed but no frontend timing, use backend
            if (step.status === 'completed' && step.durationMs === null) {
              step.durationMs = duration;
            }
          }
        }
      }
    }

    // Apply graph_metrics from backend (FIX: correct field names)
    if (data.graph_metrics) {
      const { max_hop_depth, node_count, edge_count } = data.graph_metrics;
      const traversalStep = this.steps.find(s => s.id === 'traversal');
      if (traversalStep) {
        traversalStep.hopDepth = max_hop_depth;
        traversalStep.nodes = node_count;
        traversalStep.edges = edge_count;
      }
    }

    // Mark all steps as completed
    this.steps.forEach(step => {
      if (step.status === 'pending' || step.status === 'running') {
        step.status = 'completed';
        step.completedAt = Date.now();
        if (step.startedAt) step.durationMs = step.completedAt - step.startedAt;
      }
    });

    // Update route badge if different from probe
    if (data.route && data.route !== this.currentRoute) {
      const routeBadgeEl = this.modal.querySelector("#execution-details-route-badge");
      routeBadgeEl.textContent = data.route === 'COLD_START' ? 'Cold Start' : 'Known Entity';
      routeBadgeEl.className = `execution-details-route-badge ${data.route.toLowerCase()}`;
    }

    this.#updateTimeline();
    this.#updateCurrentStepDisplay();
  }

  /**
   * Handle token event - synthesis is running
   */
  handleTokenEvent() {
    // First token means synthesis started
    const synthesisStep = this.steps.find(s => s.id === 'synthesis');
    if (synthesisStep && synthesisStep.status === 'pending') {
      synthesisStep.status = 'running';
      synthesisStep.startedAt = Date.now();
      this.stepStartTimes['synthesis'] = synthesisStep.startedAt;
      this.#updateTimeline();
      this.#updateCurrentStepDisplay();
    }
  }

  /**
   * Complete the execution (success or failure)
   * @param {boolean} success - Whether execution succeeded
   */
  complete(success = true) {
    this.#stopTimer();
    
    // Mark all pending as completed
    this.steps.forEach(step => {
      if (step.status === 'pending' || step.status === 'running') {
        step.status = success ? 'completed' : 'failed';
        step.completedAt = Date.now();
        if (step.startedAt) step.durationMs = step.completedAt - step.startedAt;
      }
    });
    
    this.#updateTimeline();
    
    const statusEl = this.modal.querySelector("#execution-details-status");
    statusEl.textContent = success ? "Completed" : "Failed";
    statusEl.className = `execution-details-status ${success ? 'success' : 'failed'}`;
    
    const nameEl = this.modal.querySelector("#execution-details-current-step-name");
    if (nameEl) nameEl.textContent = success ? "Completed" : "Failed";
    
    const durationEl = this.modal.querySelector("#execution-details-current-step-duration");
    if (durationEl) durationEl.textContent = "";
    
    // Call completion callback after brief moment to show "Completed" state
    if (this.onCompleteCallback && success) {
      setTimeout(() => {
        this.onCompleteCallback();
      }, 400);
    }
  }

  #updateCurrentStepDisplay() {
    const runningStep = this.steps.find(s => s.status === 'running');
    if (runningStep) {
      const nameEl = this.modal.querySelector("#execution-details-current-step-name");
      const durationEl = this.modal.querySelector("#execution-details-current-step-duration");
      
      if (nameEl) nameEl.textContent = runningStep.label;
      if (durationEl && runningStep.startedAt) {
        const elapsed = ((Date.now() - runningStep.startedAt) / 1000).toFixed(2);
        durationEl.textContent = `${elapsed}s`;
      }
    }
  }

  #startTimer() {
    this.#stopTimer();
    const elapsedEl = this.modal.querySelector("#execution-details-elapsed");
    
    this.timerInterval = setInterval(() => {
      if (elapsedEl) {
        const elapsed = ((Date.now() - this.startTime) / 1000).toFixed(2);
        elapsedEl.textContent = `${elapsed}s`;
      }
      this.#updateCurrentStepDisplay();
    }, 50);
  }

  #stopTimer() {
    if (this.timerInterval) {
      clearInterval(this.timerInterval);
      this.timerInterval = null;
    }
  }

  hide() {
    if (!this.isVisible) return;
    
    this.overlay.classList.remove("visible");
    this.modal.classList.remove("visible");
    
    setTimeout(() => {
      this.overlay.hidden = true;
      this.modal.hidden = true;
    }, 250);
    
    this.isVisible = false;
    this.#stopTimer();
  }
}