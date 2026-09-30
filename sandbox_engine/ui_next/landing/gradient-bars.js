/* FinGraph landing — gradient-bar background floor.
   Port of 21st.dev/waleedkibhen/gradient-bars-background, re-tuned to the
   terminal brief: the columns are electric orange-red (#FF3C00) fading to transparent, the
   pulse is deliberately slow, and the whole wall sits dim enough to be a
   textured floor rather than a spectacle — text stays the loudest thing on
   the page. Honours prefers-reduced-motion by rendering a single static
   frame. */

const DEFAULTS = {
  numBars: 13,
  gradientFrom: "rgba(255, 60, 0, 0.32)",    /* #FF3C00 at floor strength    */
  gradientTo: "transparent",
  animationDuration: 2.8,
  backgroundColor: "#030303",                /* near-black base              */
  stagger: 0.085,                            /* seconds between column pulses */
  peak: 100,                                 /* tallest column, %             */
  valley: 30,                                /* shortest                      */
  curve: 1.2,                                /* how fast the wall falls off    */
};

export function gradientBars(root, options = {}) {
  const o = Object.assign({}, DEFAULTS, options);
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* The same height profile as the original: tallest at the centre, tapering
     outward along a smooth curve — read naturally as a signal waveform. */
  const heightAt = (index, total) => {
    const progress = index / (total - 1);
    const drift = Math.abs(progress - 0.5);
    return o.valley + (o.peak - o.valley) * Math.pow(drift * 2, o.curve);
  };

  const row = document.createElement("div");
  row.setAttribute("aria-hidden", "true");
  row.style.setProperty("display", "flex");
  row.style.setProperty("height", "100%");
  row.style.setProperty("width", "100%");
  row.style.setProperty("transform", "translateZ(0)");
  row.style.setProperty("backface-visibility", "hidden");
  row.style.setProperty("-webkit-font-smoothing", "antialiased");

  for (let i = 0; i < o.numBars; i++) {
    const h = heightAt(i, o.numBars);
    const bar = document.createElement("div");
    bar.style.setProperty("flex", `1 0 calc(100% / ${o.numBars})`);
    bar.style.setProperty("max-width", `calc(100% / ${o.numBars})`);
    bar.style.setProperty("height", "100%");
    bar.style.setProperty("background", `linear-gradient(to top, ${o.gradientFrom}, ${o.gradientTo})`);
    bar.style.setProperty("transform", `scaleY(${h / 100})`);
    bar.style.setProperty("transform-origin", "bottom");
    bar.style.setProperty("transition", "transform 0.5s ease-in-out");
    bar.style.setProperty("outline", "1px solid rgba(0, 0, 0, 0)");
    bar.style.setProperty("box-sizing", "border-box");
    bar.style.setProperty("--initial-scale", String(h / 100));
    if (!reduced) {
      bar.style.setProperty("animation", `pulseBar ${o.animationDuration}s ease-in-out infinite alternate`);
      bar.style.setProperty("animation-delay", `${i * o.stagger}s`);
    }
    row.append(bar);
  }

  root.style.setProperty("background-color", o.backgroundColor);
  root.style.setProperty("overflow", "hidden");
  root.append(row);

  if (!reduced) {
    const style = document.createElement("style");
    style.textContent = `@keyframes pulseBar {
  0% { transform: scaleY(var(--initial-scale)); }
  100% { transform: scaleY(calc(var(--initial-scale) * 0.7)); }
}`;
    document.head.append(style);
  }
}