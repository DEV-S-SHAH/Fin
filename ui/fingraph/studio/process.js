/* The process strip: what the pipeline is actually doing while a question runs.
 *
 * The stages are not a scripted animation. The server reports each boundary it
 * crosses as it crosses it, and this module draws exactly those -- so a stage
 * that never fires is never shown, and a slow retrieval looks slow instead of
 * being hidden behind a bar that finishes on a timer.
 *
 * GSAP drives the motion. It is loaded as a plain global, and it is optional:
 * if the vendor file is missing the strip falls back to a two-line shim so the
 * process is still legible, because a missing animation must never cost the
 * user the only account of what the server is doing. */

import { el, clear } from "./util.js";

/* Stage key -> the short label on the rail. The long form is the server's
 * `message`, which is written for someone waiting and is the strip's heading. */
const STAGE_LABELS = {
  routing: "Routing",
  fetching: "Fetch",
  extraction: "Extract",
  stitching: "Stitch",
  traversal: "Retrieval",
  synthesis: "Synthesis",
};

const STAGE_FALLBACK = ["routing", "traversal", "synthesis"];

const labelFor = (key) => STAGE_LABELS[key] || key.replace(/_/g, " ");

const noopTween = { kill() {}, progress: () => {} };

/** Applies the end state immediately and animates nothing. */
const SHIM = {
  set(_t, _vars) {},
  to(_t, vars) { vars?.onComplete?.(); return noopTween; },
  fromTo(_t, _from, toVars) { toVars?.onComplete?.(); return noopTween; },
  from(_t, _vars) { return noopTween; },
  timeline() {
    return { to() { return this; }, fromTo() { return this; }, from() { return this; }, add() { return this; }, kill() {} };
  },
  killTweensOf() {},
};

/** The motion layer, or the shim when the motion is not wanted.
 *
 *  `prefers-reduced-motion` takes the shim for the same reason a missing vendor
 *  file does. A stylesheet cannot switch this off, because GSAP writes inline
 *  transforms and no `!important` rule reaches them; the choice has to be made
 *  here, at the one point every tween passes through. */
function motion() {
  if (globalThis.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return SHIM;
  return globalThis.gsap || SHIM;
}

export class Process {
  #host;
  #gsap;
  #plan = STAGE_FALLBACK;
  #nodes = [];
  #active = -1;
  #seen = new Set();
  #started = 0;
  #pulse = null;
  #frame = 0;
  #labels = [];

  constructor(host) {
    this.#host = host;
    this.#gsap = motion();
  }

  /** The stages a run is expected to cross, in order. Sent by the server before
   *  any work starts so the strip can be built once rather than redrawn on every
   *  event, and so the shape of the run is known while the first stage runs. */
  #build(plan, ticker) {
    const stages = Array.isArray(plan) && plan.length ? plan : STAGE_FALLBACK;
    this.#plan = stages;
    this.#active = -1;
    this.#seen = new Set();
    this.#started = performance.now();
    this.#labels = stages.map(labelFor);
    const host = clear(this.#host);
    host.hidden = false;
    host.setAttribute("role", "status");
    host.setAttribute("aria-live", "polite");

    const head = el("div", { class: "proc__head" }, [
      el("span", { class: "proc__spinner" }),
      el("span", { class: "proc__headline", text: ticker ? `Working on ${ticker}` : "Working" }),
    ]);

    const rail = el("ol", { class: "proc__rail" });
    this.#nodes = stages.map((key, i) => {
      // The connector sits in the step rather than between two of them, so the
      // rail stays a flat list: an `li` nested in an `li` is not a list, and the
      // offsetParent a connector would need to position is exactly what a
      // transformed parent breaks.
      const dot = el("span", { class: "proc__dot" });
      const time = el("span", { class: "proc__time" });
      return el("li", {
        class: "proc__step",
        style: `--i:${i}`,
      }, [
        i > 0 ? el("span", { class: "proc__link" }) : null,
        dot,
        el("span", { class: "proc__meta" }, [
          el("span", { class: "proc__label", text: this.#labels[i] }),
          time,
        ]),
      ]);
    });
    for (const node of this.#nodes) rail.append(node);

    const total = el("span", { class: "proc__total", text: "0.0s" });
    const track = el("div", { class: "proc__track" }, [
      el("span", { class: "proc__fill" }),
    ]);
    host.append(head, rail, el("div", { class: "proc__foot" }, [track, total]));

    // The rail arrives rather than appearing, so the strip reads as part of the
    // answer it sits above instead of a panel that was always there.
    this.#gsap.from(host, { opacity: 0, y: 8, duration: 0.32, ease: "power2.out" });
    this.#gsap.from(".proc__step", {
      opacity: 0, y: 6, duration: 0.3, stagger: 0.045, ease: "power2.out",
    });
    return rail;
  }

  /** A stage has begun. `message` is the server's own description of it. */
  enter(stage, message) {
    const index = this.#plan.indexOf(stage);
    if (index === -1) {
      // A stage the plan did not predict -- the fallback path, say. Added rather
      // than ignored, because it is real work the user is waiting on.
      this.#plan.push(stage);
      this.#labels.push(labelFor(stage));
      const node = el("li", { class: "proc__step", style: `--i:${this.#plan.length - 1}` }, [
        el("span", { class: "proc__link" }),
        el("span", { class: "proc__dot" }),
        el("span", { class: "proc__meta" }, [
          el("span", { class: "proc__label", text: labelFor(stage) }),
          el("span", { class: "proc__time" }),
        ]),
      ]);
      this.#host.querySelector(".proc__rail")?.append(node);
      this.#nodes.push(node);
    }

    if (this.#active === index) return;

    // Close the previous stage's clock before opening the next.
    if (this.#active !== -1) this.#close(this.#active);
    this.#active = index;
    this.#seen.add(index);

    const cell = this.#nodes[index];
    if (!cell) return;
    cell.classList.add("is-active");
    this.#headline(message || labelFor(stage));
    this.#pulse?.kill();
    this.#pulse = this.#gsap.to(cell.querySelector(".proc__dot"), {
      scale: 1.55, duration: 0.62, repeat: -1, yoyo: true, ease: "sine.inOut",
    });
    this.#gsap.fromTo(cell, { opacity: 0.55 }, { opacity: 1, duration: 0.25 });
    this.#paintRail();
    this.#startClock();
  }

  /** A free-text line under the heading: counts retrieved, a degraded run, the
   *  ticker that was resolved. Kept short and factual. */
  detail(message) {
    if (!message) return;
    this.#headline(message);
  }

  #headline(message) {
    const node = this.#host.querySelector(".proc__headline");
    if (!node || node.textContent === message) return;
    node.textContent = message;
    this.#gsap.fromTo(node, { opacity: 0.2, y: 4 }, { opacity: 1, y: 0, duration: 0.24 });
  }

  #paintRail() {
    const cells = this.#nodes;
    for (let i = 0; i < cells.length; i += 1) {
      cells[i]?.classList.toggle("is-done", i < this.#active);
      cells[i]?.classList.toggle("is-active", i === this.#active);
    }
    const done = Math.max(0, this.#active);
    const ratio = cells.length > 1 ? done / (cells.length - 1) : 0;
    this.#gsap.to(".proc__fill", {
      scaleX: ratio, duration: 0.45, ease: "power2.out",
    });
  }

  /** Mark the whole rail complete and run the bar to the end.
   *
   *  Deliberately not `#paintRail()`: that method reads `#active` as a cursor
   *  into the rail, and by the time a run ends the cursor has already been reset
   *  to -1. Repainting from -1 un-marked every stage that had finished and put
   *  the bar back to zero, so a completed run was drawn as "nothing done except
   *  the last step" -- the strip contradicted the answer sitting under it. */
  #completeRail() {
    for (const cell of this.#nodes) {
      if (!cell) continue;
      cell.classList.remove("is-active");
      cell.classList.add("is-done");
      const time = cell.querySelector(".proc__time");
      if (time && !time.textContent) {
        time.textContent = `${((performance.now() - this.#started) / 1000).toFixed(1)}s`;
      }
    }
    this.#gsap.to(".proc__fill", { scaleX: 1, duration: 0.45, ease: "power2.out" });
  }

  /** Stop a stage's clock and write how long it took. */
  #close(index) {
    const cell = this.#nodes[index];
    if (!cell) return;
    cell.classList.remove("is-active");
    cell.classList.add("is-done");
    const time = cell.querySelector(".proc__time");
    if (time && !time.textContent) {
      time.textContent = this.#elapsed(index);
    }
  }

  /** Per-stage wall clock, from when this stage opened rather than from the
   *  start of the run -- otherwise every stage reports the same growing number
   *  and the strip tells you nothing about which part was slow. */
  #opened = null;
  #openedIndex = -1;
  #elapsed(index) {
    if (index === this.#openedIndex) return `${((performance.now() - this.#opened) / 1000).toFixed(1)}s`;
    return "";
  }

  /* The clock is a readout, not decoration, so it is driven by rAF and not by
   * the motion layer. Routing it through gsap.ticker looked tidier, but the
   * shim's ticker never fires, so a page with no GSAP -- or one that asked for
   * reduced motion -- would have shown a process with no timings on it, which is
   * the one thing this strip exists to provide. */
  #startClock() {
    this.#opened = performance.now();
    this.#openedIndex = this.#active;
    this.#stopClock();
    const tick = () => {
      const time = this.#nodes[this.#active]?.querySelector(".proc__time");
      if (time) {
        time.textContent = `${((performance.now() - this.#opened) / 1000).toFixed(1)}s`;
      }
      const total = this.#host.querySelector(".proc__total");
      if (total) {
        total.textContent = `${((performance.now() - this.#started) / 1000).toFixed(1)}s`;
      }
      this.#frame = requestAnimationFrame(tick);
    };
    this.#frame = requestAnimationFrame(tick);
  }

  #stopClock() {
    if (this.#frame) {
      cancelAnimationFrame(this.#frame);
      this.#frame = 0;
    }
  }

  /** The run is over. Everything that was open is closed, the strip is marked
   *  done rather than torn down instantly, and it removes itself a beat later so
   *  the answer does not appear to have replaced a row of live work. */
  end({ ok = true, note = "" } = {}) {
    if (this.#active !== -1) this.#close(this.#active);
    this.#active = -1;
    this.#stopClock();
    this.#pulse?.kill();
    this.#pulse = null;
    const host = this.#host;
    if (host.hidden) return;
    host.classList.toggle("is-failed", !ok);
    this.#gsap.killTweensOf?.(".proc__fill");
    if (ok) this.#completeRail();
    const last = this.#nodes[this.#nodes.length - 1];
    last?.classList.add("is-done");
    // Null once the strip has been torn down, and `end` can still be reached
    // that way: a cancelled run ends twice, once from the abort and once from
    // the request finishing.
    const total = this.#host.querySelector(".proc__total");
    if (total) {
      total.textContent = note || `${((performance.now() - this.#started) / 1000).toFixed(1)}s`;
    }
    this.#gsap.to(host, {
      opacity: 0, y: -6, duration: 0.3, delay: ok ? 0.9 : 1.6, ease: "power2.in",
      onComplete: () => { host.hidden = true; },
    });
  }

  /** Build the strip for a run. `plan` is the server's list of expected stages. */
  begin({ plan, ticker } = {}) {
    this.#stopClock();
    this.#pulse?.kill();
    this.#pulse = null;
    this.#build(plan, ticker);
  }
}
