/* FinGraph landing — floating navigation. */

/* Mobile hamburger + slide-down panel. */
export function initNavPanel() {
  const toggle = document.getElementById("nav-toggle");
  const panel = document.getElementById("nav-panel");
  const announcer = document.getElementById("announcer");
  if (!toggle || !panel) return;

  function setOpen(open) {
    toggle.setAttribute("aria-expanded", String(open));
    panel.hidden = !open;
    toggle.setAttribute("aria-label", open ? "Close the menu" : "Open the menu");
    if (announcer) announcer.textContent = open ? "Menu opened" : "Menu closed";
  }

  toggle.addEventListener("click", () => setOpen(toggle.getAttribute("aria-expanded") !== "true"));

  panel.querySelectorAll("a").forEach((link) => {
    link.addEventListener("click", () => setOpen(false));
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !panel.hidden) setOpen(false);
  });
}

/* Smooth-scroll for in-page anchors; falls back to the intro section when the
   target section does not exist yet (later builds land those sections). */
export function initNavAnchors() {
  const intro = document.getElementById("intro");
  document.querySelectorAll("[data-scroll]").forEach((link) => {
    link.addEventListener("click", (event) => {
      const hash = link.getAttribute("href");
      if (!hash || !hash.startsWith("#")) return;
      const target = document.querySelector(hash) || (hash !== "#top" ? intro : null);
      if (!target) return;
      event.preventDefault();
      target.scrollIntoView({ behavior: prefersReduced() ? "auto" : "smooth", block: "start" });
      history.replaceState(null, "", hash);
    });
  });

  /* Logo click → scroll to top */
  document.querySelectorAll(".nav__brand[href='#top']").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      window.scrollTo({ top: 0, behavior: prefersReduced() ? "auto" : "smooth" });
      history.replaceState(null, "", "/");
    });
  });
}

function prefersReduced() {
  return window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)").matches : false;
}