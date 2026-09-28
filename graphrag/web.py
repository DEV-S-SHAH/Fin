"""A small local web UI for exploring the graph and asking questions.

Deliberately stdlib-only: the project has no web framework dependency, and a
graph viewer plus a query box does not justify one. The server binds to the
loopback interface by default because ``/api/ask`` spends provider quota and
there is no authentication -- exposing that on a network is the operator's
decision, made explicit with ``--allow-remote``.

Routes
------
``GET  /``                  the viewer page
``GET  /api/stats``         node/edge counts and the entity-type breakdown
``GET  /api/entities``      entity search (``?q=`` optional, ``?limit=``)
``GET  /api/graph``         a hop-limited subgraph (``?seed=``, ``?hops=``)
``POST /api/ask``           ``{"question": "..."}`` -> answer plus its subgraph

Requests are serialised behind a lock. The underlying store and the provider
client are not documented as thread-safe, and this is a single-user local tool,
so correctness beats concurrency here.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import GraphRAGConfig
from .llm import LLMClient, resolve_client
from .qa import ask
from .store import GraphStore, Subgraph

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY_BYTES = 64 * 1024


def _graph_payload(graph: Subgraph) -> dict[str, Any]:
    """Serialise a subgraph for the browser.

    Node ids are the graph's own slugs, so the client can correlate a node with
    an answer's citation tags without a second lookup.
    """
    return {
        "seeds": [e.id for e in graph.seeds],
        "nodes": [
            {
                "id": n.id,
                "name": n.name,
                "type": n.entity_type or "unspecified",
                "description": n.description or "",
            }
            for n in graph.node_list()
        ],
        "edges": [
            {
                "source": e.from_id,
                "target": e.to_id,
                "relation": e.rel_type,
                "description": e.description or "",
            }
            for e in graph.edges
        ],
    }


class _Handler(BaseHTTPRequestHandler):
    """One request handler; the owning server supplies ``graph_store`` etc."""

    server_version = "graphrag-ui"
    protocol_version = "HTTP/1.1"

    # Injected by make_server.
    graph_store: GraphStore
    llm_factory: Any
    config: GraphRAGConfig
    lock: threading.Lock

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        # Route through logging instead of stderr, so --quiet works and the
        # access log does not interleave with progress output.
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status=status)

    # -- routes -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        route = parsed.path
        params = parse_qs(parsed.query)

        if route in ("/", "/index.html"):
            return self._serve_index()
        if route == "/api/stats":
            return self._api_stats()
        if route == "/api/entities":
            return self._api_entities(params)
        if route == "/api/graph":
            return self._api_graph(params)
        return self._error(404, f"no such route: {route}")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/api/ask":
            return self._error(404, f"no such route: {parsed.path}")
        return self._api_ask()

    # -- handlers ---------------------------------------------------------

    def _serve_index(self) -> None:
        index = STATIC_DIR / "index.html"
        try:
            body = index.read_bytes()
        except OSError:
            return self._error(
                500, f"UI asset missing at {index}; reinstall the package"
            )
        self._send(200, body, "text/html; charset=utf-8")

    def _api_stats(self) -> None:
        with self.lock:
            stats = self.graph_store.stats()
        self._json(stats)

    def _api_entities(self, params: dict[str, list[str]]) -> None:
        query = (params.get("q") or [""])[0].strip()
        limit = _int_param(params, "limit", 200, maximum=1000)
        with self.lock:
            if query:
                entities = self.graph_store.search_entities(query, limit=limit)
            else:
                entities = self.graph_store.all_entities(limit=limit)
        self._json({"entities": [e.as_dict() for e in entities]})

    def _api_graph(self, params: dict[str, list[str]]) -> None:
        raw_seed = (params.get("seed") or [""])[0].strip()
        hops = _int_param(params, "hops", 2, minimum=1, maximum=3)
        limit = _int_param(params, "limit", 200, minimum=1, maximum=1000)

        if raw_seed:
            seeds = [s for s in (x.strip() for x in raw_seed.split(",")) if s]
        else:
            # No seed: show the whole graph, capped, so the page is useful
            # before the operator has picked anything.
            with self.lock:
                entities = self.graph_store.all_entities(limit=limit)
            seeds = [e.id for e in entities]
            if not seeds:
                return self._json({"seeds": [], "nodes": [], "edges": []})
            hops = 1

        with self.lock:
            graph = self.graph_store.neighborhood(seeds, hops=hops)
        self._json(_graph_payload(graph))

    def _api_ask(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._error(400, "invalid Content-Length")
        if length <= 0:
            return self._error(400, "empty request body")
        if length > MAX_BODY_BYTES:
            return self._error(413, "request body too large")

        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return self._error(400, f"invalid JSON body: {exc}")
        if not isinstance(payload, dict):
            return self._error(400, "body must be a JSON object")

        question = str(payload.get("question") or "").strip()
        if not question:
            return self._error(400, "question is required")

        # Building the client here rather than up front keeps a missing key from
        # breaking the read-only endpoints, and picks up key changes.
        try:
            client: LLMClient = self.llm_factory()
        except Exception as exc:  # noqa: BLE001 - surfaced to the browser
            return self._error(503, f"LLM provider unavailable: {exc}")

        try:
            with self.lock:
                answer = ask(question, self.graph_store, client, self.config)
        except Exception as exc:  # noqa: BLE001 - surfaced to the browser
            # Without this the handler raises into BaseHTTPRequestHandler, which
            # drops the connection and leaves the page with no explanation.
            return self._error(500, f"query failed: {exc}")

        self._json(
            {
                "question": answer.question,
                "text": answer.text,
                "grounded": answer.grounded,
                "note": answer.note,
                "linked": answer.linked,
                "used_tags": answer.used_tags,
                "hallucinated_tags": sorted(
                    set(answer.cited) - set(answer.used_tags)
                ),
                "tag_map": answer.tag_map,
                "context_entities": answer.context_entities,
                "context_edges": answer.context_edges,
                # The same trace the CLI prints, so the page can show *why* the
                # answer says what it says and not just which nodes are lit up.
                "flow": answer.render_flow(),
                "graph": _graph_payload(answer.graph) if answer.graph else None,
            }
        )


def _int_param(
    params: dict[str, list[str]],
    name: str,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Read a bounded integer query parameter, ignoring nonsense."""
    raw = (params.get(name) or [""])[0].strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def make_server(
    store: GraphStore,
    config: GraphRAGConfig | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    provider: str | None = None,
    model: str | None = None,
) -> ThreadingHTTPServer:
    """Build (but do not start) the UI server.

    *host* defaults to loopback. Passing anything else is an explicit choice by
    the caller and is logged as a warning by :func:`serve`.
    """
    config = config or store.config

    def llm_factory() -> LLMClient:
        return resolve_client(provider, model)

    handler = type(
        "_BoundHandler",
        (_Handler,),
        {
            "graph_store": store,
            "config": config,
            "lock": threading.Lock(),
            "llm_factory": staticmethod(llm_factory),
        },
    )
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def serve(
    store: GraphStore,
    config: GraphRAGConfig | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    provider: str | None = None,
    model: str | None = None,
    open_browser: bool = True,
) -> int:
    """Run the UI until interrupted. Returns a process exit code."""
    import webbrowser

    config = config or store.config

    if host not in ("127.0.0.1", "localhost", "::1"):
        # No auth, and /api/ask spends provider quota on whoever can reach it.
        log.warning(
            "serving on %s: /api/ask is unauthenticated and spends provider quota",
            host,
        )

    server = make_server(store, config, host, port, provider, model)
    url = f"http://{host}:{server.server_address[1]}/"
    stats = store.stats()
    print(f"graphrag UI   : {url}")
    print(f"database      : {store.path}")
    print(
        f"graph         : {stats.get('nodes', 0)} entities, "
        f"{stats.get('edges', 0)} relationships"
    )
    print("press Ctrl-C to stop")

    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.shutdown()
        server.server_close()
    return 0
