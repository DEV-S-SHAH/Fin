/* FinGraph landing — rotating hero word.
   Cycles the word after "See the" through a fixed list with a slide+fade,
   the way a text-loop component would. The container is a fixed-width,
   one-line, overflow-hidden box so the swap never reflows the headline.
   Static under reduced motion. */

const WORDS = ["connections", "insights", "relationships", "signals", "patterns"];
const INTERVAL = 2600;
const TRANSITION = 560;

export function initTextLoop() {
  const root = document.querySelector("[data-text-loop]");
  if (!root) return;
  const inner = root.querySelector(".text-loop__inner");
  const word = root.querySelector(".text-loop__word");
  const title = document.getElementById("hero-title");
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const spacer = root.querySelector(".text-loop__spacer");
  if (spacer) {
    const longest = WORDS.reduce((a, b) => (b.length > a.length ? b : a), "");
    spacer.textContent = longest;
  }

  let index = 0;

  const announce = (text) => {
    if (title) title.setAttribute("aria-label", `See the ${text} behind the numbers.`);
  };

  function rotate() {
    index = (index + 1) % WORDS.length;
    const text = WORDS[index];

    /* slide the old word up and out, then drop the new one in from below */
    inner.style.transform = "translateY(-100%)";
    inner.style.opacity = "0";

    setTimeout(() => {
      word.textContent = text;
      announce(text);
      inner.style.transition = "none";
      inner.style.transform = "translateY(100%)";
      inner.style.opacity = "0";
      void inner.offsetWidth; /* commit the reset before re-enabling the transition */
      inner.style.transition = "";
      inner.style.transform = "translateY(0)";
      inner.style.opacity = "1";
    }, TRANSITION);
  }

  announce(WORDS[0]);
  if (reduced) return;
  setInterval(rotate, INTERVAL);
}
