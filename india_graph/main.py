"""Entry points for the India Company Graph.

UI:    streamlit run india_graph/ui.py
CLI:   python -m india_graph build --cin <CIN> [--name <name>]

The CLI build command scrapes NSE/BSE/MCA for a CIN and prints the resulting
graph as nodes/edges — useful for testing without Streamlit.
"""

from __future__ import annotations

import argparse
import json

from .config import IndiaConfig
from .orchestrator import IndiaOrchestrator
from .resolver import TickerCINResolver


def build_cli(cin: str, name: str = "") -> None:
    config = IndiaConfig()
    resolver = TickerCINResolver(config)
    orch = IndiaOrchestrator(config=config, resolver=resolver)
    graph = orch.build_for_cin(cin, name)
    print(json.dumps({
        "cin": graph.cin,
        "company_name": graph.company_name,
        "nodes": graph.as_nodes(),
        "edges": graph.as_edges(),
    }, indent=2, default=str))


def main() -> None:
    parser = argparse.ArgumentParser(prog="india_graph")
    sub = parser.add_subparsers(dest="command")
    b = sub.add_parser("build", help="Build a session graph for a CIN")
    b.add_argument("--cin", required=True, help="MCA Corporate Identification Number")
    b.add_argument("--name", default="", help="Company name (optional)")
    args = parser.parse_args()
    if args.command == "build":
        build_cli(args.cin, args.name)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
