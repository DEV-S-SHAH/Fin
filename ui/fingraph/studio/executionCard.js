/**
 * ExecutionCard — Premium centered execution card for GraphRAG
 * 
 * Visual direction: FinGraph landing page aesthetic
 * - Dark/black translucent glass surface
 * - Subtle black → deep red/orange gradient
 * - Thin translucent border
 * - Soft glassmorphism / backdrop blur
 * - Minimal, premium, no visual noise
 * 
 * Fully synchronized with REAL backend at localhost:9100/api
 * No fake timers, no hardcoded execution states.
 */

import { $, el, clear } from "./util.js";

/**
 * Format duration from milliseconds to human-readable string
 */
export function formatDuration(ms) {
  if (ms === null || ms === undefined || isNaN(ms)) return "—";
  if (ms < 1) return `${ms.toFixed(2)}ms`;
  if (ms < 1000) return `${ms.toFixed(2)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

const STEP_LABELS = {
  routing: "Entity Resolution",
  fetching: "Filing Acquisition",
  extraction: "Fact Extraction",
  stitching: "Graph Stitching",
  traversal: "Graph Retrieval",
  synthesis: "Answer Synthesis"
};

const STEP_DESCRIPTIONS = {
  routing: "Matching the question to an entity in the graph",
  fetching: "Fetching the latest SEC filing from EDGAR",
  extraction: "Extracting financial facts from the filing",
  stitching: "Stitching extracted facts onto the knowledge graph",
  traversal: "Traversing the graph to retrieve relevant context",
  synthesis: "Composing the answer from retrieved evidence"
};

const ROUTE_STAGES = {
  KNOWN: ["routing", "traversal", "synthesis"],
  COLD_START: ["routing", "fetching", "extraction", "stitching", "traversal", "synthesis"]
};

const STEP_MAP = {
  routing: "routing",
  fetching: "fetching",
  extracting: "extraction",
  stitching: "stitching",
  traversing: "traversal",
  traversal: "traversal",
  synthesis: "synthesis",
  fallback: "synthesis"
};

export class ExecutionCard {
  constructor() {
    this.overlay = null;
    this.card = null;
    this.steps = [];
    this.startTime = 0;
    this.timerInterval = null;
    this.isVisible = false;
    this.currentRoute = "KNOWN";
    this.currentTicker = null;
    this.currentQuestion = "";
    this.backendMetrics = null;
    this.stepStartTimes = {};
    this.onCompleteCallback = null;
    this.isComplete = false;
    this.currentStepIndex = -1;
    
    this.#createElements();
  }

  #createElements() {
    // Card - compact, centered, premium glassmorphism
    // Uses flexbox centering: position: fixed; inset: 0; display: flex; align-items: center; justify-content: center;
    this.card = el("div", { 
      class: "execution-card", 
      role: "dialog", 
      "aria-modal": "true", 
      "aria-labelledby": "execution-card-title",
      hidden: true 
    }, [
      // Panel - the actual card content
      el("div", { class: "execution-card__panel" }, [
        // Header
        el("div", { class: "execution-card__header" }, [
          el("h2", { 
            id: "execution-card-title", 
            class: "execution-card__title" 
          }, "GraphRAG Execution"),
          el("span", { 
            id: "execution-card-status", 
            class: "execution-card__status running" 
          }, "Running"),
        ]),

        // Body
        el("div", { 
          id: "execution-card-body", 
          class: "execution-card__body" 
        }, [
          // Question
          el("div", { class: "execution-card__question" }, [
            el("p", { 
              id: "execution-card-question", 
              class: "execution-card__question-text" 
            }, ""),
          ]),

          // Meta badges (ticker + route)
          el("div", { 
            id: "execution-card-meta", 
            class: "execution-card__meta",
            hidden: true
          }, [
            el("span", { 
              id: "execution-card-ticker", 
              class: "execution-card__badge" 
            }, ""),
            el("span", { 
              id: "execution-card-route", 
              class: "execution-card__badge" 
            }, "")
          ]),

          // Timeline - clean vertical workflow
          el("ol", { 
            id: "execution-card-timeline", 
            class: "execution-card__timeline", 
            role: "list",
            "aria-label": "Execution steps"
          }),

          // Footer - elapsed timer
          el("div", { class: "execution-card__footer" }, [
            el("div", { class: "execution-card__elapsed-wrap" }, [
              el("span", { 
                id: "execution-card-elapsed", 
                class: "execution-card__elapsed" 
              }, "0.00s"),
              el("span", { class: "execution-card__elapsed-label" }, "elapsed")
            ]),
            el("div", { class: "execution-card__current" }, [
              el("span", { 
                id: "execution-card-current-step", 
                class: "execution-card__current-name" 
              }, "Initializing…"),
              el("span", { 
                id: "execution-card-current-duration", 
                class: "execution-card__current-duration" 
              }, "")
            ])
          ])
        ])
      ])
    ]);

    document.body.append(this.card);

    // Close handlers - click on overlay (the flex container) closes the card
    this.card.addEventListener("click", (e) => {
      if (e.target === this.card) this.hide();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && this.isVisible) this.hide();
    });
  }

  /**
   * Show the card and initialize from route probe
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
    this.startTime = Date.now();
    this.isComplete = false;
    this.currentStepIndex = -1;

    // Initialize steps based on route
    const routeStages = ROUTE_STAGES[route] || ROUTE_STAGES.KNOWN;
    
    this.steps = routeStages.map((key, index) => ({
      id: key,
      label: STEP_LABELS[key] || key,
      description: STEP_DESCRIPTIONS[key] || "",
      status: "pending",
      startedAt: null,
      completedAt: null,
      durationMs: null,
      backendDurationMs: null,
      nodes: null,
      edges: null,
      hopDepth: null
    }));

    // Set question
    this.card.querySelector("#execution-card-question").textContent = question;

    // Set meta badges
    const metaEl = this.card.querySelector("#execution-card-meta");
    const tickerEl = this.card.querySelector("#execution-card-ticker");
    const routeEl = this.card.querySelector("#execution-card-route");

    if (ticker) {
      tickerEl.textContent = ticker;
      tickerEl.hidden = false;
    } else {
      tickerEl.hidden = true;
    }

    routeEl.textContent = route === "COLD_START" ? "Cold Start" : "Known Entity";
    routeEl.className = `execution-card__badge ${route.toLowerCase()}`;
    routeEl.hidden = false;

    metaEl.hidden = false;

    // Update status
    const statusEl = this.card.querySelector("#execution-card-status");
    statusEl.textContent = "Running";
    statusEl.className = "execution-card__status running";

    // Render initial timeline
    this.#renderTimeline();

    // Show with animation
    this.card.hidden = false;
    requestAnimationFrame(() => {
      this.card.classList.add("visible");
    });

    this.isVisible = true;

    // Start elapsed timer
    this.#startTimer();
  }

  #renderTimeline() {
    const container = this.card.querySelector("#execution-card-timeline");
    clear(container);

    this.steps.forEach((step, index) => {
      const stepEl = this.#createStepElement(step, index);
      container.append(stepEl);
    });
  }

  #createStepElement(step, index) {
    const isLast = index === this.steps.length - 1;
    
    return el("li", { 
      class: `execution-card__step ${step.status}`,
      "data-step-id": step.id,
      role: "listitem"
    }, [
      // Connector line (except last)
      !isLast && el("span", { class: "execution-card__connector" }),

      // Step content
      el("div", { class: "execution-card__step-content" }, [
        // Status indicator
        el("span", { 
          class: `execution-card__indicator ${step.status}` 
        }, [
          el("span", { class: "execution-card__dot" })
        ]),

        // Label + description
        el("div", { class: "execution-card__step-info" }, [
          el("span", { class: "execution-card__step-label" }, step.label),
          el("span", { class: "execution-card__step-description" }, step.description)
        ]),

        // Duration (right side)
        el("span", { 
          class: `execution-card__step-duration ${step.status}` 
        }, step.backendDurationMs !== null ? formatDuration(step.backendDurationMs) : "—")
      ])
    ]);
  }

  #updateTimeline() {
    this.steps.forEach((step, index) => {
      const stepEl = this.card.querySelector(`[data-step-id="${step.id}"]`);
      if (!stepEl) return;

      stepEl.className = `execution-card__step ${step.status}`;
      
      const indicator = stepEl.querySelector(".execution-card__indicator");
      if (indicator) indicator.className = `execution-card__indicator ${step.status}`;

      const durationEl = stepEl.querySelector(".execution-card__step-duration");
      if (durationEl && step.backendDurationMs !== null) {
        durationEl.textContent = formatDuration(step.backendDurationMs);
        durationEl.className = `execution-card__step-duration ${step.status}`;
      }
    });
  }

  /**
   * Handle backend SSE status event
   * @param {Object} data - SSE status event data {step, message, elapsed_ms, stage_duration_ms, stage_complete, ticker?, stages?}
   */
  handleStatusEvent(data) {
    if (!this.isVisible || this.isComplete) return;

    const { step, message, elapsed_ms, stage_duration_ms, stage_complete, ticker } = data;

    if (step === "start") {
      if (ticker && !this.currentTicker) {
        this.currentTicker = ticker;
        const tickerEl = this.card.querySelector("#execution-card-ticker");
        tickerEl.textContent = ticker;
        tickerEl.hidden = false;
        this.card.querySelector("#execution-card-meta").hidden = false;
      }
      return;
    }

    const stepId = STEP_MAP[step];
    if (!stepId) return;

    const stepIndex = this.steps.findIndex(s => s.id === stepId);
    if (stepIndex === -1) return;

    const stepOrder = ["routing", "fetching", "extraction", "stitching", "traversal", "synthesis"];
    const currentIndex = stepOrder.indexOf(stepId);

    // Handle stage completion with actual backend duration
    if (stage_complete && stage_duration_ms !== undefined) {
      const completedStep = this.steps.find(s => s.id === stepId);
      if (completedStep && (completedStep.status === "pending" || completedStep.status === "running")) {
        completedStep.status = "completed";
        completedStep.backendDurationMs = stage_duration_ms;
        completedStep.completedAt = Date.now();
        if (completedStep.startedAt) {
          completedStep.durationMs = completedStep.completedAt - completedStep.startedAt;
        }
      }
      this.#updateTimeline();
      this.#updateCurrentStepDisplay();
      return;
    }

    // Mark previous steps as completed (without backend duration - will be set on stage_complete)
    for (let i = 0; i < currentIndex; i++) {
      const prevStepId = stepOrder[i];
      const prevStep = this.steps.find(s => s.id === prevStepId);
      if (prevStep && (prevStep.status === "pending" || prevStep.status === "running")) {
        prevStep.status = "completed";
        // Don't set backendDurationMs here - wait for stage_complete event
        prevStep.completedAt = Date.now();
        if (prevStep.startedAt) {
          prevStep.durationMs = prevStep.completedAt - prevStep.startedAt;
        }
      }
    }

    // Mark current step as running
    const currentStep = this.steps[stepIndex];
    currentStep.status = "running";
    currentStep.startedAt = Date.now();
    this.stepStartTimes[stepId] = currentStep.startedAt;
    this.currentStepIndex = stepIndex;

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
    this.backendMetrics = data;
    this.isComplete = true;
    this.#stopTimer();

    // Apply stage_latencies_ms from backend
    if (data.stage_latencies_ms) {
      const stageMap = {
        routing: "routing",
        fetching: "fetching",
        extracting: "extraction",
        stitching: "stitching",
        traversing: "traversal",
        traversal: "traversal",
        synthesis: "synthesis"
      };

      for (const [stageKey, duration] of Object.entries(data.stage_latencies_ms)) {
        const stepId = stageMap[stageKey];
        if (stepId) {
          const step = this.steps.find(s => s.id === stepId);
          if (step) {
            step.backendDurationMs = duration;
            if (step.status === "completed" && step.durationMs === null) {
              step.durationMs = duration;
            }
          }
        }
      }
    }

    // Apply graph_metrics from backend
    if (data.graph_metrics) {
      const { max_hop_depth, node_count, edge_count } = data.graph_metrics;
      const traversalStep = this.steps.find(s => s.id === "traversal");
      if (traversalStep) {
        traversalStep.hopDepth = max_hop_depth;
        traversalStep.nodes = node_count;
        traversalStep.edges = edge_count;
      }
    }

    // Mark all steps as completed
    this.steps.forEach(step => {
      if (step.status === "pending" || step.status === "running") {
        step.status = "completed";
        step.completedAt = Date.now();
        if (step.startedAt) step.durationMs = step.completedAt - step.startedAt;
      }
    });

    // Update route badge if different from probe
    if (data.route && data.route !== this.currentRoute) {
      const routeEl = this.card.querySelector("#execution-card-route");
      routeEl.textContent = data.route === "COLD_START" ? "Cold Start" : "Known Entity";
      routeEl.className = `execution-card__badge ${data.route.toLowerCase()}`;
    }

    this.#updateTimeline();
    this.#updateCurrentStepDisplay();
  }

  /**
   * Handle token event - synthesis is running
   */
  handleTokenEvent() {
    const synthesisStep = this.steps.find(s => s.id === "synthesis");
    if (synthesisStep && synthesisStep.status === "pending") {
      synthesisStep.status = "running";
      synthesisStep.startedAt = Date.now();
      this.stepStartTimes["synthesis"] = synthesisStep.startedAt;
      this.currentStepIndex = this.steps.findIndex(s => s.id === "synthesis");
      this.#updateTimeline();
      this.#updateCurrentStepDisplay();
    }
  }

  /**
   * Handle error
   */
  handleError() {
    if (!this.isVisible) return;
    this.isComplete = true;
    this.#stopTimer();
    this.steps.forEach(step => {
      if (step.status === "running") step.status = "failed";
    });
    const statusEl = this.card.querySelector("#execution-card-status");
    statusEl.textContent = "Failed";
    statusEl.className = "execution-card__status failed";
    this.#updateTimeline();
  }

  #updateCurrentStepDisplay() {
    const runningStep = this.steps.find(s => s.status === "running");
    const nameEl = this.card.querySelector("#execution-card-current-step");
    const durationEl = this.card.querySelector("#execution-card-current-duration");

    if (runningStep) {
      nameEl.textContent = runningStep.label;
      if (runningStep.startedAt) {
        const elapsed = ((Date.now() - runningStep.startedAt) / 1000).toFixed(2);
        durationEl.textContent = `${elapsed}s`;
      }
    } else if (this.isComplete) {
      nameEl.textContent = "Completed";
      durationEl.textContent = "";
    }
  }

  #startTimer() {
    this.#stopTimer();
    const elapsedEl = this.card.querySelector("#execution-card-elapsed");

    this.timerInterval = setInterval(() => {
      if (elapsedEl && this.isVisible) {
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

    this.card.classList.remove("visible");

    setTimeout(() => {
      this.card.hidden = true;
      this.isVisible = false;
      this.#stopTimer();
      if (this.onCompleteCallback) {
        this.onCompleteCallback();
        this.onCompleteCallback = null;
      }
    }, 300);
  }

  setOnComplete(callback) {
    this.onCompleteCallback = callback;
  }
}