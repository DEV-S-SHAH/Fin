/* FinGraph sign-in — entry point.

   The buttons link to the providers' official OAuth authorize endpoints.
   Client IDs are server-side configuration (env vars); the page asks the
   server which providers are enabled and only shows those. The provider
   redirects back to /auth/callback, which trades the returned token for a
   local session cookie and lands on the app. */

import { stampLogos } from "../landing/components.js";

const REDIRECT_URI = `${location.origin}/auth/callback`;
const STATE_KEY = "fin.auth.state";

/* Official authorize endpoints. response_type=token (implicit) for Google
   and TradingView so the access token comes back in the redirect fragment;
   Apple returns code+id_token the same way with response_mode=fragment. */
const PROVIDERS = {
  google: {
    label: "Google",
    url: (cid, state) =>
      "https://accounts.google.com/o/oauth2/v2/auth"
      + `?client_id=${encodeURIComponent(cid)}`
      + `&redirect_uri=${encodeURIComponent(REDIRECT_URI)}`
      + "&response_type=token"
      + "&scope=" + encodeURIComponent("openid email profile")
      + `&state=${encodeURIComponent(state)}`,
  },
  apple: {
    label: "Apple",
    url: (cid, state) =>
      "https://appleid.apple.com/auth/authorize"
      + `?client_id=${encodeURIComponent(cid)}`
      + `&redirect_uri=${encodeURIComponent(REDIRECT_URI)}`
      + "&response_type=" + encodeURIComponent("code id_token")
      + "&scope=" + encodeURIComponent("name email")
      + "&response_mode=fragment"
      + `&state=${encodeURIComponent(state)}`,
  },
  tradingview: {
    label: "TradingView",
    url: (cid, state) =>
      "https://www.tradingview.com/authorize/"
      + `?client_id=${encodeURIComponent(cid)}`
      + `&redirect_uri=${encodeURIComponent(REDIRECT_URI)}`
      + "&response_type=token"
      + "&scope=read"
      + `&state=${encodeURIComponent(state)}`,
  },
};

const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/* ------------------------------- starfield ---------------------------------- */
/* A quiet field of stars: slow drift, gentle twinkle. Purely decorative —
   pointer-events are off and it sits behind everything. */
function initStars() {
  const canvas = document.getElementById("stars");
  if (!canvas || reduced) return;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  let stars = [];
  let raf = 0;
  let w = 0;
  let h = 0;

  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    w = canvas.clientWidth;
    h = canvas.clientHeight;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const count = Math.round((w * h) / 9000);
    stars = Array.from({ length: count }, () => ({
      x: Math.random() * w,
      y: Math.random() * h,
      r: Math.random() * 1.1 + 0.3,
      base: Math.random() * 0.5 + 0.15,
      amp: Math.random() * 0.3,
      speed: Math.random() * 0.9 + 0.25,
      phase: Math.random() * Math.PI * 2,
      drift: Math.random() * 0.008 + 0.002,
    }));
  }

  function frame(t) {
    ctx.clearRect(0, 0, w, h);
    for (const s of stars) {
      const twinkle = s.base + s.amp * Math.sin(s.phase + t * 0.001 * s.speed);
      ctx.globalAlpha = Math.max(0.05, Math.min(1, twinkle));
      ctx.fillStyle = "#e8e8ec";
      ctx.beginPath();
      ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
      ctx.fill();
      s.y -= s.drift;
      if (s.y < -2) s.y = h + 2;
    }
    ctx.globalAlpha = 1;
    raf = requestAnimationFrame(frame);
  }

  resize();
  window.addEventListener("resize", resize, { passive: true });
  raf = requestAnimationFrame(frame);
}

/* --------------------------------- notices ---------------------------------- */
function showNotice(message) {
  const el = document.getElementById("auth-notice");
  if (!el) return;
  el.textContent = message;
  el.hidden = false;
}

/* ------------------------------ provider config ----------------------------- */
async function loadConfig() {
  try {
    const res = await fetch("/api/auth/config", { headers: { accept: "application/json" } });
    if (!res.ok) return {};
    return await res.json();
  } catch {
    return {};
  }
}

/* --------------------------------- signing in ------------------------------- */
function beginOAuth(provider, clientId) {
  const state = crypto.randomUUID();
  try { sessionStorage.setItem(STATE_KEY, state); } catch { /* private mode */ }
  location.assign(PROVIDERS[provider].url(clientId, state));
}

/* Local development sign-in: no provider round-trip, the server issues the
   same session cookie the OAuth callback would. */
async function signInLocally() {
  const btn = document.getElementById("btn-dev");
  if (btn) {
    btn.disabled = true;
    btn.style.opacity = "0.6";
  }
  try {
    const res = await fetch("/api/auth/session", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ provider: "dev", token: "local" }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    location.assign("/app");
  } catch (err) {
    showNotice(`Could not start a local session: ${err.message || "unknown error"}`);
    if (btn) {
      btn.disabled = false;
      btn.style.opacity = "";
    }
  }
}

/* ----------------------------------- start ----------------------------------- */
async function start() {
  stampLogos();
  initStars();

  const config = await loadConfig();
  const wrap = document.getElementById("providers");
  const devSection = document.getElementById("auth-dev");

  let enabled = 0;
  for (const provider of Object.keys(PROVIDERS)) {
    const clientId = config[provider];
    const btn = wrap && wrap.querySelector(`[data-provider="${provider}"]`);
    if (!clientId) {
      if (btn) btn.hidden = true;
      continue;
    }
    enabled += 1;
    if (btn) btn.addEventListener("click", () => beginOAuth(provider, clientId));
  }

  if (enabled === 0 && devSection) {
    devSection.hidden = false;
    const devBtn = document.getElementById("btn-dev");
    if (devBtn) devBtn.addEventListener("click", signInLocally);
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", start);
} else {
  start();
}
