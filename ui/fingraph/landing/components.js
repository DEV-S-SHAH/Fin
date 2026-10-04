/* FinGraph landing — reusable UI components (ES modules, no build step). */

/* Tiny DOM helper: h("div", {class:"x"}, child...) -> Node */
export function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2), value);
    } else if (key === "aria-label") node.setAttribute("aria-label", value);
    else if (key === "style" && typeof value === "object") {
      Object.assign(node.style, value);
    } else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child == null) continue;
    node.append(child.nodeType ? child : String(child));
  }
  return node;
}

/* Element builder for the clickable actions. */
export function PrimaryButton({ label, href, icon }) {
  return h("a", { class: "btn btn--primary", href },
    icon,
    label && span(label)
  );
}

function span(text) {
  const el = document.createElement("span");
  el.className = "btn__label";
  el.textContent = text;
  return el;
}

export function SecondaryButton({ label, href, icon }) {
  return h("a", { class: "btn btn--ghost", href },
    icon,
    span(label)
  );
}

/* SectionHeading -> { title, sub } rendered centred. */
export function SectionHeading({ kicker, title, sub }) {
  return h("div", { class: "section-head" },
    kicker && h("p", { class: "section-head__kicker" }, kicker),
    h("h2", { class: "section-head__title" }, title),
    sub && h("p", { class: "section-head__sub" }, sub)
  );
}

/* FinGraphLogo — abstract node-graph mark. Returns an SVG string so it can be
   stamped into any element (nav, footer). */
export function FinGraphLogo() {
  const size = 32;
  const nodes = [
    [16, 20, 7.5, "main"],      // primary node
    [7, 9, 3.6, "sat"],
    [26, 8, 3.2, "sat"],
    [25.5, 22, 2.6, "sat"],
  ];
  const links = [
    [16, 20, 7, 9],
    [16, 20, 26, 8],
    [16, 20, 25.5, 22],
  ];
  const linkLines = links
    .map(([x1, y1, x2, y2]) =>
      `<line class="logo__link" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}" vector-effect="non-scaling-stroke"/>`
    )
    .join("");
  const nodeEls = nodes
    .map(([x, y, r, kind]) =>
      `<circle class="logo__node--${kind}" cx="${x}" cy="${y}" r="${r}"/>`
    )
    .join("");
  const dots = [
    [11.5, 23.5, 1.7],
    [21, 12.5, 1.5],
  ].map(([x, y, r]) => `<circle class="logo__node--dot" cx="${x}" cy="${y}" r="${r}"/>`).join("");

  return `<svg class="logo__svg" viewBox="0 0 ${size} ${size}" fill="none" aria-hidden="true" focusable="false">${linkLines}${nodeEls}${dots}</svg>`;
}

export function stampLogos(root = document) {
  root.querySelectorAll("[data-logo]").forEach((el) => { el.innerHTML = FinGraphLogo(); });
}

/* FloatingDataCard — decorated stat chip for the hero corners. */
export function FloatingDataCard({ label, value, id }) {
  return h("div", { class: "float u-float--" + (id || "tl"), "data-float": "" },
    h("span", { class: "float__label" }, label),
    h("span", { class: "float__value" }, value)
  );
}

