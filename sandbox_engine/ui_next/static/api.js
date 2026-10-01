/* HTTP + SSE client.
 *
 * The two request shapes the UI needs are here and nowhere else: plain JSON,
 * and a `text/event-stream` read off a POST body. `EventSource` cannot be used
 * for the second — it is GET-only, and a question has to be a POST so the
 * question text never lands in a URL, a history entry, or a proxy log — so the
 * SSE framing is parsed off the fetch body by hand. It is a dozen lines and it
 * buys token streaming, which is the difference between waiting eight seconds
 * in silence and watching an answer arrive.
 */

export async function get(path, opts = {}) {
  return unwrap(path, { method: "GET", credentials: "include", ...opts });
}

export async function post(path, body) {
  return unwrap(path, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
}

async function unwrap(path, init) {
  let response;
  try {
    response = await fetch(path, { cache: "no-store", credentials: "include", ...init });
  } catch (cause) {
    /* An abort is a decision rather than a failure -- the reader cancelled, or
     * a caller set a deadline on purpose -- and there is nothing to diagnose.
     * Letting it fall through to the branch below would report a deliberate
     * cancellation as a dropped connection, spend three seconds probing the
     * server to find out which, and then hand back an `Error` whose name is no
     * longer `AbortError`, so the caller's cancel handling would miss it. */
    if (cause?.name === "AbortError") throw cause;
    /* A rejected fetch is the browser refusing to say anything: the request
     * never completed, so there is no status, no body and no usable message.
     * "Failed to fetch" is true and useless — it does not distinguish a server
     * that is gone from a request that blew up inside a server that is alive,
     * and those need different fixes. One probe on a cheap endpoint tells them
     * apart. */
    throw new Error(await describeTransportFailure(path));
  }

  let body = null;
  try { body = await response.json(); } catch { /* empty or not json */ }
  if (!response.ok) throw new Error(body?.error || `HTTP ${response.status}`);
  return body;
}

async function describeTransportFailure(path) {
  const where = location.origin;
  let alive = false;
  try {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 3000);
    const probe = await fetch("/api/stats", { signal: ctl.signal, cache: "no-store", credentials: "include" });
    clearTimeout(timer);
    alive = probe.ok;
  } catch { alive = false; }

  if (!alive) {
    return (
      `Cannot reach the server at ${where} — ${path} got no response. It is not ` +
      `running, or it stopped partway. If you started it in a terminal, check ` +
      `that terminal for a traceback and restart it with ` +
      `python -m sandbox_engine.ui_next`
    );
  }
  return (
    `${where} is running, but the connection dropped during ${path} without a ` +
    `response, which means the request failed inside the server. The traceback ` +
    `was printed to the terminal running it.`
  );
}

/* ── reads ────────────────────────────────────────────────────────────────── */

export const fetchStats = () => get("/api/stats");
export const fetchCompanies = () => get("/api/companies");
export const fetchRagState = () => get("/api/rag");
export const fetchReports = () => get("/api/reports");
export const fetchReport = (id) => get(`/api/reports/${encodeURIComponent(id)}`);

/** Which retrieval route a question would take. Cheap: one graph query, no model
 *  call. The UI asks this before it commits, so it can stream only where the
 *  server really streams. `opts` carries a `signal` so a caller that is already
 *  showing "working" cannot be left waiting on this forever. */
export const fetchRoute = (question, opts = {}) =>
  get(`/api/route?q=${encodeURIComponent(question)}`, opts);

export function fetchEntities(query = "", limit = 500) {
  const params = new URLSearchParams({ limit: String(limit) });
  if (query) params.set("q", query);
  return get(`/api/entities?${params}`);
}

export function fetchGraph({ seed = "", hops = 2, limit = 250 } = {}) {
  const params = new URLSearchParams({ hops: String(hops), limit: String(limit) });
  if (seed) params.set("seed", seed);
  return get(`/api/graph?${params}`);
}

/* ── writes ───────────────────────────────────────────────────────────────── */

export const saveRagState = (body) => post("/api/rag", body);
export const askJson = (question) => post("/api/ask", { question });

/**
 * Ask with the answer streamed back token by token.
 *
 * @param {string} question
 * @param {(evt: {type: string, data: any}) => void} onEvent
 * @param {{signal?: AbortSignal}} [opts]
 */
export async function askStream(question, onEvent, opts = {}) {
  const response = await fetch("/api/ask?stream=true", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ question, stream: true }),
    signal: opts.signal,
  });

  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const body = await response.json();
      if (body?.error) detail = body.error;
    } catch { /* not json: the status line is all there is */ }
    throw new Error(detail);
  }
  if (!response.body) throw new Error("this browser cannot read a streaming response");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // Frames are separated by a blank line; anything after the last separator is
    // a partial frame and stays in the buffer until the next read completes it.
    let split;
    while ((split = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, split);
      buffer = buffer.slice(split + 2);
      const parsed = parseFrame(frame);
      if (parsed) onEvent(parsed);
    }
  }
  const tail = parseFrame(buffer);
  if (tail) onEvent(tail);
}

function parseFrame(frame) {
  let type = "message";
  const dataLines = [];
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) type = line.slice(6).trim();
    else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
  }
  if (!dataLines.length) return null;
  try {
    return { type, data: JSON.parse(dataLines.join("\n")) };
  } catch {
    return null;
  }
}
