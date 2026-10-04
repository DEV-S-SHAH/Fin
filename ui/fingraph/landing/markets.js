/* FinGraph landing — live markets: ticker tape + featured cards.

   Data comes from the server's /api/markets (yfinance behind a one-minute
   cache), so the page polls on the same cadence the cache expires on: fresh
   quotes every minute, one upstream call per minute regardless of how many
   visitors are watching. */

import { h } from "./components.js";

const REFRESH_MS = 60_000;

/* The featured cards: the three issuers in the graph first. */
const FEATURED = ["AAPL", "MSFT", "NVDA"];

const ARROW_UP = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 5l7 9H5z"/></svg>';
const ARROW_DOWN = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M12 19l-7-9h14z"/></svg>';
const ARROW_FLAT = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M5 11h14v2H5z"/></svg>';

function direction(row) {
  if (row.pct == null || row.pct === 0) return "flat";
  return row.pct > 0 ? "up" : "down";
}

function arrow(dir) {
  return dir === "up" ? ARROW_UP : dir === "down" ? ARROW_DOWN : ARROW_FLAT;
}

/* The h() helper appends strings as text nodes, so the arrow markup has to be
   parsed into a real element before it can go in the tree. */
function arrowNode(dir) {
  const tpl = document.createElement("template");
  tpl.innerHTML = arrow(dir);
  return tpl.content.firstElementChild;
}

function fmt(value, digits = 2) {
  return value == null ? "—" : Number(value).toFixed(digits);
}

function signed(value, digits = 2) {
  if (value == null) return "—";
  return (value > 0 ? "+" : "") + Number(value).toFixed(digits);
}

/* One tape item: SYM  price  ▲ +1.23 (+0.45%) */
function tickItem(row) {
  const dir = direction(row);
  return h("span", { class: "tick" },
    h("span", { class: "tick__sym" }, row.ticker),
    h("span", { class: "tick__price" }, fmt(row.price)),
    h("span", { class: `tick__chg is-${dir}` }, arrowNode(dir), signed(row.pct) + "%"),
  );
}

/* The track holds the list twice so the -50% translate loops seamlessly. */
function renderTicker(track, rows) {
  track.replaceChildren();
  for (const pass of [0, 1]) {
    for (const row of rows) track.append(tickItem(row));
  }
}

function card(row) {
  const dir = direction(row);
  return h("article", { class: "mkt-card" },
    h("div", { class: "mkt-card__head" },
      h("span", { class: "mkt-card__sym" }, row.ticker),
      h("span", { class: "mkt-card__price" }, fmt(row.price)),
    ),
    h("span", { class: `mkt-card__chg is-${dir}` },
      arrowNode(dir),
      h("span", {}, `${signed(row.change)} (${signed(row.pct)}%)`),
      h("small", {}, "today"),
    ),
  );
}

function renderGrid(grid, rows) {
  grid.replaceChildren();
  const byTicker = new Map(rows.map((r) => [r.ticker, r]));
  const featured = FEATURED.map((t) => byTicker.get(t)).filter(Boolean);
  const rest = rows.filter((r) => !FEATURED.includes(r.ticker)).slice(0, 3);
  for (const row of [...featured, ...rest]) grid.append(card(row));
}

function setStatus(text, isError = false) {
  const el = document.getElementById("markets-status");
  if (!el) return;
  el.textContent = text;
  el.classList.toggle("is-error", isError);
}

async function fetchMarkets() {
  const res = await fetch("/api/markets", { headers: { accept: "application/json" } });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  return data.markets || [];
}

async function refresh() {
  const track = document.getElementById("ticker-track");
  const grid = document.getElementById("markets-grid");
  if (!track || !grid) return;

  try {
    const rows = await fetchMarkets();
    if (!rows.length) throw new Error("empty");
    renderTicker(track, rows);
    renderGrid(grid, rows);
    setStatus("");
  } catch (err) {
    // Keep whatever is on screen; say why it is stale rather than blanking it.
    setStatus(`Live quotes unavailable — retrying (${err.message || "error"})`, true);
  }
}

export function initMarkets() {
  const ticker = document.getElementById("ticker");
  if (!ticker) return;

  setStatus("Loading live quotes…");
  refresh();
  setInterval(refresh, REFRESH_MS);

  /* Pause-on-hover is CSS; this is the keyboard equivalent — focus inside the
     tape stops it too, so the pause is not a pointer-only privilege. */
  ticker.addEventListener("focusin", () => ticker.classList.add("is-paused"));
  ticker.addEventListener("focusout", () => ticker.classList.remove("is-paused"));
}
