/**
 * RunDetailsPanel - Studio Integration
 * 
 * A drawer panel for viewing GraphRAG execution details within the Studio.
 * Follows the same pattern as ReportsPanel.
 */

import { $, clear, el } from "./util.js";

export class RunDetailsPanel {
  constructor() {
    this.drawer = null;
    this.scrim = null;
    this.content = null;
    this.lastFocus = null;
    this.currentTrace = null;
    
    this.#createElements();
    this.#wire();
  }

  #createElements() {
    // Create drawer elements dynamically
    this.scrim = el("div", { class: "scrim", id: "run-details-scrim", hidden: true });
    this.drawer = el("aside", { 
      class: "drawer", 
      id: "run-details-drawer", 
      role: "dialog", 
      "aria-modal": "true", 
      "aria-labelledby": "run-details-title",
      hidden: true 
    }, [
      el("div", { class: "drawer__head" }, [
        el("h2", { id: "run-details-title", class: "drawer__title" }, "Execution Details"),
        el("div", { class: "topbar__spacer" }),
        el("button", { 
          type: "button", 
          id: "run-details-close", 
          class: "btn btn--ghost btn--icon", 
          title: "Close (Esc)" 
        }, [
          el("svg", { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: "2", "aria-hidden": "true" }, [
            el("path", { d: "M6 6l12 12M18 6l-12 12", strokeLinecap: "round" })
          ]),
          el("span", { class: "sr-only" }, "Close execution details")
        ])
      ]),
      el("div", { class: "drawer__body", id: "run-details-body" })
    ]);

    document.body.append(this.scrim, this.drawer);
    this.content = this.drawer.querySelector("#run-details-body");
  }

  #wire() {
    this.scrim?.addEventListener("click", () => this.close());
    this.drawer.querySelector("#run-details-close")?.addEventListener("click", () => this.close());
    
    this.drawer.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        this.close();
      }
      if (event.key !== "Tab") return;
      const focusable = this.drawer.querySelectorAll(
        'button:not([disabled]), input, [tabindex]:not([tabindex="-1"])'
      );
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });
  }

  open(trace, onCite = null) {
    this.currentTrace = trace;
    this.onCiteCallback = onCite;
    this.lastFocus = document.activeElement;
    
    this.drawer.hidden = false;
    this.scrim.hidden = false;
    requestAnimationFrame(() => {
      this.drawer.classList.add("is-open");
      this.scrim.classList.add("is-open");
    });
    
    this.#render(trace);
    this.drawer.querySelector("#run-details-close").focus();
  }

  close() {
    this.drawer.classList.remove("is-open");
    this.scrim.classList.remove("is-open");
    setTimeout(() => {
      this.drawer.hidden = true;
      this.scrim.hidden = true;
    }, 220);
    this.lastFocus?.focus?.();
  }

  get isOpen() {
    return this.drawer?.classList.contains("is-open") ?? false;
  }

  #render(trace) {
    if (!this.content || !trace) return;
    
    clear(this.content);
    this.content.append(this.#createTimelineHTML(trace));
  }

  #createTimelineHTML(trace) {
    const container = el("div", { class: "run-details-panel" });
    
    // Header with run info
    const header = el("div", { class: "run-details-panel__header" }, [
      el("div", { class: "run-details-panel__meta" }, [
        el("span", { class: "run-details-panel__question" }, `“${trace.question}”`),
        trace.ticker && el("span", { class: "run-details-panel__ticker" }, trace.ticker),
        el("span", { 
          class: `run-details-panel__status run-details-panel__status--${trace.status}` 
        }, trace.status.charAt(0).toUpperCase() + trace.status.slice(1))
      ])
    ]);
    container.append(header);

    // Timeline
    const timeline = el("div", { class: "run-details-panel__timeline" });
    trace.steps.forEach((step, index) => {
      timeline.append(this.#createStepElement(step, index === trace.steps.length - 1));
    });
    container.append(timeline);

    // Footer
    if (trace.completedAt) {
      const footer = el("div", { class: "run-details-panel__footer" }, [
        el("div", { class: "run-details-panel__summary" }, [
          el("span", {}, [
            el("strong", {}, `${trace.steps.filter(s => s.status === 'completed').length}`),
            " completed"
          ]),
          el("span", {}, [
            el("strong", {}, `${trace.steps.filter(s => s.status === 'failed').length}`),
            " failed"
          ]),
          el("span", {}, [
            el("strong", {}, this.#formatDuration(trace.durationMs)),
            " total"
          ])
        ])
      ]);
      container.append(footer);
    }

    return container;
  }

  #createStepElement(step, isLast) {
    const statusClass = this.#getStatusClass(step.status);
    const icon = this.#getStepIcon(step.status);
    const duration = this.#formatDuration(step.durationMs);
    
    const stepEl = el("div", { 
      class: `run-details-step ${statusClass} ${step.children && step.children.length > 0 ? 'has-children' : ''}` 
    }, [
      // Connector
      el("div", { class: "run-details-step__connector" }, [
        !isLast && el("div", { class: "run-details-step__vertical-line" })
      ]),
      
      // Content
      el("div", { class: "run-details-step__content" }, [
        // Indicator
        el("div", { class: "run-details-step__indicator" }, [
          el("span", { class: `run-details-step__dot ${statusClass}` }, icon),
          step.children && step.children.length > 0 && el("button", { 
            class: "run-details-step__expand",
            type: "button",
            "aria-expanded": "false",
            "aria-label": "Expand details"
          }, [
            el("svg", { viewBox: "0 0 24 24", fill: "none", stroke: "currentColor", strokeWidth: "2", "aria-hidden": "true" }, [
              el("path", { d: "M6 9l6 6 6-6", strokeLinecap: "round", strokeLinejoin: "round" })
            ])
          ])
        ]),
        
        // Label area
        el("div", { class: "run-details-step__label-area" }, [
          el("div", { class: "run-details-step__header" }, [
            el("span", { class: "run-details-step__name" }, step.name),
            step.message && el("span", { class: "run-details-step__message" }, step.message)
          ]),
          
          // Details
          step.details && Object.keys(step.details).length > 0 && el("div", { class: "run-details-step__details" }, [
            ...Object.entries(step.details).map(([key, value]) => 
              el("div", { class: "run-details-step__detail-row" }, [
                el("span", { class: "run-details-step__detail-key" }, key),
                el("span", { class: "run-details-step__detail-value" }, String(value))
              ])
            )
          ]),
          
          // Children
          step.children && step.children.length > 0 && el("div", { class: "run-details-step__children", hidden: true }, [
            ...step.children.map((child, childIndex) => 
              this.#createChildStepElement(child, childIndex === step.children.length - 1)
            )
          ])
        ]),
        
        // Duration
        el("div", { class: "run-details-step__duration" }, duration)
      ])
    ]);

    // Add expand handler
    const expandBtn = stepEl.querySelector(".run-details-step__expand");
    const childrenContainer = stepEl.querySelector(".run-details-step__children");
    if (expandBtn && childrenContainer) {
      expandBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        const expanded = expandBtn.getAttribute("aria-expanded") === "true";
        expandBtn.setAttribute("aria-expanded", String(!expanded));
        childrenContainer.hidden = expanded;
        expandBtn.querySelector("svg")?.style.setProperty("transform", expanded ? "rotate(0)" : "rotate(180deg)");
      });
    }

    return stepEl;
  }

  #createChildStepElement(step, isLast) {
    const statusClass = this.#getStatusClass(step.status);
    const icon = this.#getStepIcon(step.status);
    const duration = this.#formatDuration(step.durationMs);
    
    return el("div", { 
      class: `run-details-step ${statusClass} nested ${isLast ? 'last-child' : ''}` 
    }, [
      el("div", { class: "run-details-step__connector" }, [
        !isLast && el("div", { class: "run-details-step__vertical-line" })
      ]),
      el("div", { class: "run-details-step__content" }, [
        el("div", { class: "run-details-step__indicator" }, [
          el("span", { class: `run-details-step__dot ${statusClass}` }, icon)
        ]),
        el("div", { class: "run-details-step__label-area" }, [
          el("div", { class: "run-details-step__header" }, [
            el("span", { class: "run-details-step__name" }, step.name),
            step.message && el("span", { class: "run-details-step__message" }, step.message)
          ]),
          step.details && Object.keys(step.details).length > 0 && el("div", { class: "run-details-step__details" }, [
            ...Object.entries(step.details).map(([key, value]) => 
              el("div", { class: "run-details-step__detail-row" }, [
                el("span", { class: "run-details-step__detail-key" }, key),
                el("span", { class: "run-details-step__detail-value" }, String(value))
              ])
            )
          ])
        ]),
        el("div", { class: "run-details-step__duration" }, duration)
      ])
    ]);
  }

  #getStatusClass(status) {
    switch (status) {
      case 'completed': return 'is-done';
      case 'running': return 'is-active';
      case 'failed': return 'is-failed';
      default: return 'is-pending';
    }
  }

  #getStepIcon(status) {
    switch (status) {
      case 'completed': return '✓';
      case 'running': return '●';
      case 'failed': return '✕';
      default: return '○';
    }
  }

  #formatDuration(ms) {
    if (!ms && ms !== 0) return '—';
    if (ms < 1000) return `${ms}ms`;
    return `${(ms / 1000).toFixed(2)}s`;
  }
}