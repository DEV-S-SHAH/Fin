/* FinGraph landing — entry point. */

import { h, stampLogos } from "./components.js";
import { initNavPanel, initNavAnchors } from "./nav.js";
import { gradientBars } from "./gradient-bars.js";
import { initMarkets } from "./markets.js";
import { initTextLoop } from "./text-loop.js";

const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ── ambient gradient-bar floor ───────────────────────────────────────────── */
function initGradientBars() {
  const root = document.getElementById("gradient-bars");
  if (!root) return;
  gradientBars(root);
}

/* ── billing toggle ───────────────────────────────────────────────────────── */
const BILLING_NOTES = {
  free: { monthly: "Free forever", yearly: "Free forever" },
  pro: { monthly: "Billed monthly", yearly: "Billed yearly — 2 months free" },
  enterprise: { monthly: "Tailored to your stack", yearly: "Tailored to your stack" },
};

function initBillingToggle() {
  const sw = document.getElementById("billing-switch");
  if (!sw) return;

  const labelMonthly = document.getElementById("label-monthly");
  const labelYearly = document.getElementById("label-yearly");
  const amounts = Array.from(document.querySelectorAll(".plan__amount[data-monthly]"));
  const notes = Array.from(document.querySelectorAll("[data-billing-note]"));

  let yearly = false;

  function apply(animate) {
    sw.setAttribute("aria-checked", String(yearly));
    labelMonthly.classList.toggle("is-on", !yearly);
    labelYearly.classList.toggle("is-on", yearly);

    for (const el of amounts) {
      const value = yearly ? el.dataset.yearly : el.dataset.monthly;
      if (!animate || reduced) {
        el.textContent = "$" + value;
        continue;
      }
      /* cross-fade: out, swap, in — 160ms each way */
      el.classList.add("is-swapping");
      setTimeout(() => {
        el.textContent = "$" + value;
        el.classList.remove("is-swapping");
      }, 160);
    }

    for (const el of notes) {
      const card = el.closest("[data-plan]");
      const plan = card ? card.dataset.plan : "free";
      el.textContent = BILLING_NOTES[plan][yearly ? "yearly" : "monthly"];
    }
  }

  sw.addEventListener("click", () => {
    yearly = !yearly;
    apply(true);
  });

  apply(false);
}

/* ── navbar scroll effect ───────────────────────────────────────────────────── */
function initNavScroll() {
  const navWrap = document.querySelector(".nav-wrap");
  if (!navWrap) return;

  let ticking = false;
  const threshold = 40;

  function onScroll() {
    const scrolled = window.scrollY > threshold;
    navWrap.classList.toggle("nav-wrap--scrolled", scrolled);
  }

  function requestTick() {
    if (!ticking) {
      requestAnimationFrame(() => {
        onScroll();
        ticking = false;
      });
      ticking = true;
    }
  }

  window.addEventListener("scroll", requestTick, { passive: true });
  onScroll(); // initial check
}

/* ── first-paint polish: only under reduced-motion nothing animates ────────── */
function start() {
  stampLogos();
  initNavPanel();
  initNavAnchors();
  if (!reduced) {
    const hero = document.getElementById("hero");
    if (hero) hero.classList.add("hero--ready");
  }
  initNavScroll();
  initGradientBars();
  initMarkets();
  initBillingToggle();
  initTextLoop();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", start);
} else {
  start();
}