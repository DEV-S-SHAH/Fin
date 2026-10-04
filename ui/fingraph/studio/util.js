/* Shared helpers: DOM building, escaping, markdown, colours, formatting.
 *
 * Two rules hold everywhere below, and they are the reason this file exists
 * rather than a `$` helper inline in each module:
 *
 *   1. Model output is escaped exactly once, at the edge. `md()` escapes the
 *      whole string before it looks for any markup, so a sentence can never
 *      bring its own tag along. Nothing downstream concatenates prose into HTML.
 *   2. Values that are not markup are set with `textContent`. The one function
 *      that returns markup (`md`) is the only one whose result goes to innerHTML.
 */

export const $ = (id) => document.getElementById(id);

export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "html") node.innerHTML = value;
    else if (key === "style") node.setAttribute("style", value);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export const clear = (node) => { while (node.firstChild) node.removeChild(node.firstChild); return node; };

export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

const SVG_NS = "http://www.w3.org/2000/svg";

export function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined) continue;
    node.setAttribute(key, value);
  }
  return node;
}

/* ── type colours ─────────────────────────────────────────────────────────── */

const KNOWN_COLORS = {
  company: "#FF3C00",
  filing: "#FF551C",
  financialmetric: "#F59E0B",
  segment: "#FF6B35",
  disclosureevent: "#FF7A45",
  documentchunk: "#FF8A50",
  executive: "#FBBF24",
  supplier: "#FB923C",
  riskfactor: "#EF4444",
  productfamily: "#FCD34D",
  geographicmarket: "#F97316",
  competitor: "#EA580C",
  customer: "#FDBA74",
  section: "#FFA07A",
  causalrelation: "#E63600",
  regulatorybody: "#D97706",
  macrovariable: "#F59E0B",
  footnote: "#FED7AA",
  fiscalperiod: "#FB923C",
  rawfact: "#FF6B35",
  standardizedconcept: "#FF551C",
};

/* One hue per entity type, stable across reloads: a type that hashes to a
 * different colour on each load makes the legend a lie. */
export function typeColor(type) {
  const key = String(type || "unspecified").toLowerCase();
  if (KNOWN_COLORS[key]) return KNOWN_COLORS[key];
  let hash = 0;
  for (const ch of key) hash = (hash * 31 + ch.codePointAt(0)) >>> 0;
  const hue = 10 + (hash % 45);
  return `hsl(${hue} 85% 60%)`;
}

export function prettyType(type) {
  const key = String(type || "");
  return key ? key.replace(/([a-z])([A-Z])/g, "$1 $2") : "Unspecified";
}

/** The two node shapes the server sends: `/api/graph` says `entity_type`, the
 *  payload from `/api/ask` says `type`. Every reader normalises through this. */
export function typeOf(node) {
  if (!node) return "Unspecified";
  return node.type || node.entity_type || "Unspecified";
}

/* ── markdown ─────────────────────────────────────────────────────────────── */

function inline(text) {
  return escapeHtml(text)
    /* A citation is only a citation if the tag was actually issued; the caller
     * checks that against tag_map, and a tag with no node is left as plain text
     * rather than rendered as a button that goes nowhere. */
    .replace(/\[(E\d+)\]/g, '<button class="cite" data-tag="$1" title="Focus $1 in the graph">$1</button>')
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])_([^_]+)_(?=[\s.,;:)]|$)/g, "$1<em>$2</em>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/(^|\s)(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
}

/** Minimal, safe markdown → HTML. Headings, bold, italic, code, quotes, rules,
 *  bullet and ordered lists, and `[E1]` citations. Everything is escaped first,
 *  so no branch below can emit a tag the model wrote. */
export function md(source) {
  const out = [];
  let list = null;

  const closeList = () => {
    if (list) { out.push(`</${list}>`); list = null; }
  };

  for (const raw of String(source ?? "").split(/\r?\n/)) {
    const line = raw.trimEnd();

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      closeList();
      const level = Math.min(6, Math.max(3, heading[1].length + 2));
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }

    if (/^\s*(?:[-*_]\s*){3,}$/.test(line) && line.trim()) {
      closeList();
      out.push("<hr>");
      continue;
    }

    const quote = line.match(/^&gt;\s?(.*)$/);
    if (quote) {
      closeList();
      out.push(`<blockquote>${inline(quote[1])}</blockquote>`);
      continue;
    }

    const bullet = line.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)$/);
    if (bullet) {
      const kind = /^\s*\d/.test(line) ? "ol" : "ul";
      if (list !== kind) { closeList(); out.push(`<${kind}>`); list = kind; }
      out.push(`<li>${inline(bullet[1])}</li>`);
      continue;
    }

    closeList();
    if (!line.trim()) continue;
    out.push(`<p>${inline(line)}</p>`);
  }
  closeList();
  return out.join("\n");
}

/** Highlight every occurrence of `term` inside already-escaped text. */
export function highlight(text, term) {
  const safe = escapeHtml(text ?? "");
  if (!term) return safe;
  const needle = escapeHtml(term).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return safe.replace(new RegExp(needle, "gi"), (hit) => `<mark class="hit">${hit}</mark>`);
}

/* ── formatting ───────────────────────────────────────────────────────────── */

const NUM = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 });

export function fmtNumber(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "number") return NUM.format(value);
  return String(value);
}

const SCALE_WORDS = { 0: "units", 3: "thousands", 6: "millions", 9: "billions" };

export function fmtScale(value) {
  if (value === null || value === undefined || value === "") return "";
  return SCALE_WORDS[value] ?? String(value);
}

export function fmtDuration(ms) {
  if (!Number.isFinite(ms)) return "";
  return ms < 1000 ? `${Math.round(ms)}ms` : `${(ms / 1000).toFixed(1)}s`;
}

export function plural(n, one, many = `${one}s`) {
  return `${NUM.format(n)} ${n === 1 ? one : many}`;
}

export function debounce(fn, ms) {
  let timer;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
}

/* ── persisted preferences ────────────────────────────────────────────────── */

const PREFIX = "ui2.";

export function loadPref(key, fallback) {
  try {
    const raw = localStorage.getItem(PREFIX + key);
    return raw === null ? fallback : JSON.parse(raw);
  } catch {
    return fallback;
  }
}

export function savePref(key, value) {
  try {
    localStorage.setItem(PREFIX + key, JSON.stringify(value));
  } catch {
    /* private mode: preferences are a convenience, not a requirement */
  }
}

/* ── feedback ─────────────────────────────────────────────────────────────── */

export function toast(message, kind = "") {
  const host = $("toasts");
  if (!host) return;
  const node = el("div", { class: `toast${kind ? ` toast--${kind}` : ""}`, text: message });
  host.append(node);
  setTimeout(() => {
    node.classList.add("is-leaving");
    setTimeout(() => node.remove(), 220);
  }, kind === "bad" ? 7000 : 3600);
}

export function announce(text) {
  const box = $("announcer");
  if (box) box.textContent = text;
}

/** Screen-reader text for a decorative icon button that already has a title. */
export function titleOf(node) {
  return node?.getAttribute("title") || node?.textContent?.trim() || "";
}

/* ── FinGraph logo (shared with landing page) ──────────────────────────────── */

export function FinGraphLogo() {
  const size = 32;
  const nodes = [
    [16, 20, 7.5, "main"],
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
