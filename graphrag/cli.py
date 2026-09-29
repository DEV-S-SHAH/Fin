"""Command-line interface for the domain-agnostic GraphRAG pipeline."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import DEFAULT_UI_PORT, UI_PORT_ENV, GraphRAGConfig
from .envfile import load_env_file
from .ingest import ingest_pdf
from .llm import LLMError, resolve_client
from .qa import ask
from .store import GraphStore


def _default_db() -> str:
    for candidate in ("data/aapl-2026.lbug", "./graphrag_db.lbug"):
        if Path(candidate).exists():
            return candidate
    return "./graphrag_db.lbug"


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        default=_default_db(),
        help="path to the Ladybug database file (default: %(default)s)",
    )
    parser.add_argument(
        "--provider",
        choices=("auto", "gemini", "nvidia", "openai", "anthropic", "ollama", "heuristic"),
        default="auto",
        help="LLM provider; 'auto' uses whichever API key is present",
    )
    parser.add_argument("--model", default=None, help="override the provider's model")
    parser.add_argument("--verbose", "-v", action="store_true")


def _build_config(args: argparse.Namespace) -> GraphRAGConfig:
    return GraphRAGConfig.from_env(
        provider=args.provider,
        model=args.model,
        hops=getattr(args, "hops", None),
    )


def _open(args: argparse.Namespace, read_only: bool = False) -> tuple[GraphStore, object]:
    client = resolve_client(args.provider, args.model)
    store = GraphStore(args.db, _build_config(args), read_only=read_only)
    return store, client


def cmd_ingest(args: argparse.Namespace) -> int:
    pdf_paths: list[Path] = []
    for raw in args.pdf:
        path = Path(raw)
        if path.is_dir():
            pdf_paths.extend(sorted(path.glob("*.pdf")))
        else:
            pdf_paths.append(path)

    if not pdf_paths:
        print("error: no PDF files given", file=sys.stderr)
        return 2
    missing = [p for p in pdf_paths if not p.exists()]
    if missing:
        for path in missing:
            print(f"error: not found: {path}", file=sys.stderr)
        return 2

    store, client = _open(args)
    try:
        store.ensure_schema()
        if args.reset:
            store.reset()
        if not client.is_model:
            print(
                "note: no LLM credentials found; using the offline heuristic "
                "provider (lexical, not model-quality)",
                file=sys.stderr,
            )
        print(f"provider: {client.name}")
        print(f"database: {store.path}")

        for pdf in pdf_paths:
            print(f"\n--- ingesting {pdf} ---")
            report = ingest_pdf(pdf, store, client, store.config, progress=args.verbose)
            print(report.summary())
            if not report.ok:
                print("  (completed with chunk failures)", file=sys.stderr)
        stats = store.stats()
        print(
            f"\ngraph now holds {stats['nodes']} entities and "
            f"{stats['edges']} relationships"
        )
    finally:
        store.close()
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    if not Path(args.db).exists():
        print(
            f"error: no graph at {args.db}; run 'ingest' first",
            file=sys.stderr,
        )
        return 2

    store, client = _open(args, read_only=False)
    try:
        store.ensure_schema()
        answer = ask(args.question, store, client, store.config)
        if args.json:
            print(
                json.dumps(
                    {
                        "question": answer.question,
                        "answer": answer.text,
                        "cited": answer.cited,
                        "used_tags": answer.used_tags,
                        "linked_entities": answer.linked,
                        "context_entities": answer.context_entities,
                        "context_relationships": answer.context_edges,
                        "grounded": answer.grounded,
                        "note": answer.note,
                    },
                    indent=2,
                )
            )
        else:
            print(answer.render())
    finally:
        store.close()
    return 0 if answer.grounded else 1


def cmd_serve(args: argparse.Namespace) -> int:
    from .web import serve

    config = _build_config(args)
    store = GraphStore(args.db, config, read_only=True)
    try:
        return serve(
            store,
            config,
            host=args.host,
            port=args.port,
            provider=args.provider,
            model=args.model,
            open_browser=not args.no_browser,
        )
    finally:
        store.close()


def cmd_stats(args: argparse.Namespace) -> int:
    if not Path(args.db).exists():
        print(f"error: no graph at {args.db}", file=sys.stderr)
        return 2
    store = GraphStore(args.db, _build_config(args), read_only=True)
    try:
        stats = store.stats()
        if args.json:
            print(json.dumps(stats, indent=2))
            return 0
        print(f"database   : {stats['path']}")
        print(f"buffer pool: {stats['buffer_pool_size'] // (1024 * 1024)} MB")
        print(f"entities   : {stats['nodes']}")
        print(f"relations  : {stats['edges']}")
        if stats["entity_types"]:
            print("\nentity types discovered by the LLM:")
            for name, count in stats["entity_types"]:
                print(f"  {count:>5}  {name}")
        if stats["relation_types"]:
            print("\nrelation types discovered by the LLM:")
            for name, count in stats["relation_types"]:
                print(f"  {count:>5}  {name}")
    finally:
        store.close()
    return 0


def cmd_entities(args: argparse.Namespace) -> int:
    store = GraphStore(args.db, _build_config(args), read_only=True)
    try:
        for entity in store.all_entities(limit=args.limit):
            print(f"{entity.id}\t{entity.entity_type}\t{entity.name}")
    finally:
        store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="graphrag",
        description=(
            "Domain-agnostic GraphRAG: build a knowledge graph from any PDF "
            "and answer questions over it."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="extract a PDF into the graph")
    _add_common(ingest)
    ingest.add_argument("pdf", nargs="+", help="PDF path(s) or a directory of them")
    ingest.add_argument(
        "--reset", action="store_true", help="clear the graph before ingesting"
    )
    ingest.set_defaults(func=cmd_ingest)

    ask_cmd = sub.add_parser("ask", help="answer a question over the graph")
    _add_common(ask_cmd)
    ask_cmd.add_argument("question", help="the question to answer")
    ask_cmd.add_argument("--hops", type=int, default=None, help="traversal depth")
    ask_cmd.add_argument("--json", action="store_true", help="emit JSON")
    ask_cmd.set_defaults(func=cmd_ask)

    stats = sub.add_parser("stats", help="show graph statistics")
    _add_common(stats)
    stats.add_argument("--json", action="store_true")
    stats.set_defaults(func=cmd_stats)

    entities = sub.add_parser("entities", help="list stored entities")
    _add_common(entities)
    entities.add_argument("--limit", type=int, default=100)
    entities.set_defaults(func=cmd_entities)

    serve_cmd = sub.add_parser("serve", help="open the graph viewer in a browser")
    _add_common(serve_cmd)
    serve_cmd.add_argument(
        "--host",
        default="127.0.0.1",
        help="interface to bind; anything but loopback exposes an "
        "unauthenticated endpoint that spends provider quota (default: %(default)s)",
    )
    serve_cmd.add_argument(
        "--port",
        type=int,
        default=None,
        help="port to listen on (default: $%s, else %d)"
        % (UI_PORT_ENV, DEFAULT_UI_PORT),
    )
    serve_cmd.add_argument(
        "--no-browser", action="store_true", help="do not open a browser window"
    )
    serve_cmd.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return int(args.func(args))
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        # A busy port is an operator decision, not a crash: report it and exit
        # non-zero rather than printing a traceback from inside http.server.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        # Same reasoning for a mistyped PORT_GRAPHRAG_UI. Surfacing it as a
        # traceback would make a one-character slip look like a code fault.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
