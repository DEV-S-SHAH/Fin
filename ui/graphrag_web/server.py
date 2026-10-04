"""GraphRAG Web UI server — moved from graphrag.web.

This module provides the HTTP server for the GraphRAG Web UI (port 8765).

Run:
    python -m ui.graphrag_web
"""

from __future__ import annotations

import errno
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

# Import from graphrag package
from graphrag.config import DEFAULT_UI_PORT, UI_PORT_ENV, GraphRAGConfig, default_ui_port
from graphrag.llm import LLMClient, resolve_client
from graphrag.qa import ask
from graphrag.store import GraphStore, Subgraph
from sandbox_engine.http_limits import BoundedThreadingHTTPServer, Limits
from sandbox_engine.observability import (
    generate_request_id,
    get_request_id,
    set_request_id,
    clear_request_id,
    record_request,
)

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY_BYTES = 64 * 1024

#: Read once, at import, for the same reason as every other setting in this
#: repository: a running server's limits should not shift underneath it. Same
#: variable names as ``sandbox_engine.query_ui`` (see ``Limits.from_env``), but
#: resolved from ``os.environ`` alone rather than from ``.env``, matching how
#: ``graphrag.config`` reads its own settings. This server has no streaming
#: responses, so the two SSE ceilings do not apply and stay at their defaults.
LIMITS = Limits.from_env()


def bind_server(
    handler: type[BaseHTTPRequestHandler],
    host: str,
    port: int,
    limits: Limits | None = None,
) -> BoundedThreadingHTTPServer:
    """Bind *host*:*port*, turning a busy port into an explanation.

    The ceiling and the listen backlog come from ``BoundedThreadingHTTPServer``
    so this server has the same bounded cost per connection as the explorers.
    """
    try:
        return BoundedThreadingHTTPServer((host, port), handler, limits=limits or LIMITS)
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        raise OSError(
            errno.EADDRINUSE,
            f"port {port} on {host} is already in use.\n"
            f"  Another copy of this server is probably still running: "
            f"lsof -nP -iTCP:{port} -sTCP:LISTEN\n"
            f"  Or pick another port: --port <n>, or {UI_PORT_ENV}=<n>",
        ) from None


def _graph_payload(graph: Subgraph) -> dict[str, Any]:
    """Serialise a subgraph for the browser."""
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

    #: Without this, ``protocol_version = "HTTP/1.1"`` above means an idle
    #: keep-alive socket pins its handler thread for as long as the client
    #: cares to hold it open, with no ceiling on how many may do so. The class
    #: value is the process default; ``setup`` narrows it to the owning
    #: server's limits so one listener can be tuned without touching the rest.
    timeout: float = LIMITS.request_timeout

    # Injected by make_server.
    graph_store: GraphStore
    llm_factory: Any
    config: GraphRAGConfig
    lock: threading.Lock

    # -- plumbing ---------------------------------------------------------

    def setup(self) -> None:
        limits = getattr(self.server, "limits", None)
        if limits is not None:
            self.timeout = limits.request_timeout
        super().setup()

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        # Include request ID in response headers for client-side tracing
        request_id = getattr(self, "_request_id", None) or get_request_id()
        if request_id:
            self.send_header("X-Request-ID", request_id)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status=status)

    def _handle_request(self, fn: Callable[[], None]) -> None:
        """Handle a request with request ID generation and propagation."""
        request_id = generate_request_id()
        token = set_request_id(request_id)
        self._request_id = request_id
        try:
            fn()
        finally:
            clear_request_id(token)

    # -- routes -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._handle_request(self._do_get)

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle_request(self._do_get)

    def do_POST(self) -> None:  # noqa: N802
        self._handle_request(self._do_post)

    def _do_get(self) -> None:
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

    def _do_post(self) -> None:
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

        try:
            client: LLMClient = self.llm_factory()
        except Exception as exc:  # noqa: BLE001 - surfaced to the browser
            return self._error(503, f"LLM provider unavailable: {exc}")

        try:
            with self.lock:
                answer = ask(question, self.graph_store, client, self.config)
        except Exception as exc:  # noqa: BLE001 - surfaced to the browser
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
    port: int | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> BoundedThreadingHTTPServer:
    """Build (but do not start) the UI server."""
    config = config or store.config
    port = default_ui_port() if port is None else port

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
    server = bind_server(handler, host, port)
    server.daemon_threads = True
    return server


def serve(
    store: GraphStore,
    config: GraphRAGConfig | None = None,
    host: str = "127.0.0.1",
    port: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    open_browser: bool = True,
) -> int:
    """Run the UI until interrupted. Returns a process exit code."""
    import webbrowser

    config = config or store.config

    if host not in ("127.0.0.1", "localhost", "::1"):
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


def _main() -> None:
    import argparse
    from graphrag.config import GraphRAGConfig

    p = argparse.ArgumentParser(
        prog="python -m ui.graphrag_web",
        description="GraphRAG Web UI",
    )
    p.add_argument("--port", type=int, default=None,
                   help=f"Port to bind (default: {UI_PORT_ENV} or {DEFAULT_UI_PORT})")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--provider", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--db", type=Path, default=None,
                   help="Database file to serve")
    args = p.parse_args()

    from graphrag.store import GraphStore, default_db_path
    store = GraphStore(args.db) if args.db else GraphStore(default_db_path())
    
    try:
        serve(store, host=args.host, port=args.port, provider=args.provider,
              model=args.model, open_browser=not args.no_browser)
    except OSError as exc:
        log.error("%s", exc)
        raise SystemExit(1)


if __name__ == "__main__":
    _main()