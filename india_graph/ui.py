"""Streamlit chat UI for the India Company Graph.

Run with:  streamlit run india_graph/ui.py   (or: python -m streamlit run india_graph/ui.py)

Localhost-only for now. The user picks a company (ticker -> CIN), asks a
question, and gets a provenance-tagged answer over a just-in-time graph that
is destroyed when the session ends.
"""

from __future__ import annotations

import streamlit as st

from .config import NIFTY_50, IndiaConfig
from .graph import SessionGraph
from .orchestrator import build_graph_for_session
from .provenance import (
    build_evidence,
    grade_answer,
)
from .resolver import TickerCINResolver
from .session import SessionManager

st.set_page_config(page_title="India Company Graph", page_icon="🇮🇳", layout="wide")

# -- session state ----------------------------------------------------------

if "config" not in st.session_state:
    st.session_state.config = IndiaConfig()
if "resolver" not in st.session_state:
    st.session_state.resolver = TickerCINResolver(st.session_state.config)
if "session_mgr" not in st.session_state:
    st.session_state.session_mgr = SessionManager(st.session_state.config)
if "session_id" not in st.session_state:
    st.session_state.session_id = st.session_state.session_mgr.create_session()
if "graph_built" not in st.session_state:
    st.session_state.graph_built = False
if "bound_cin" not in st.session_state:
    st.session_state.bound_cin = None
if "messages" not in st.session_state:
    st.session_state.messages = []

config: IndiaConfig = st.session_state.config
resolver: TickerCINResolver = st.session_state.resolver
session_mgr: SessionManager = st.session_state.session_mgr

# -- header ----------------------------------------------------------------

st.title("🇮🇳 India Company Graph")
st.caption("NSE + BSE + MCA · just-in-time graph · provenance-tagged evidence · ephemeral session")

with st.sidebar:
    st.header("Session")
    st.write(f"Session: `{st.session_state.session_id[:8]}`")
    if st.button("New session / destroy graph"):
        session_mgr.destroy_session(st.session_state.session_id)
        st.session_state.session_id = session_mgr.create_session()
        st.session_state.graph_built = False
        st.session_state.bound_cin = None
        st.session_state.messages = []
        st.rerun()
    st.divider()
    st.header("Company")
    ticker_input = st.text_input("NSE/BSE ticker (e.g. RELIANCE)", value="")
    if st.button("Select company") and ticker_input.strip():
        cin = resolver.resolve(ticker_input.strip())
        if cin is None:
            st.warning(f"CIN not cached for {ticker_input.strip()}. MCA live lookup not yet wired — enter CIN directly.")
        else:
            st.session_state.bound_cin = cin
            ok = session_mgr.bind_company(st.session_state.session_id, cin, ticker_input.strip().upper())
            if ok:
                st.session_state.graph_built = False
                st.success(f"Bound to {cin}")
            else:
                st.error("Session expired — create a new session.")
    cin_input = st.text_input("Or enter MCA CIN directly", value="")
    if st.button("Bind by CIN") and cin_input.strip():
        cin = cin_input.strip().upper()
        st.session_state.bound_cin = cin
        session_mgr.bind_company(st.session_state.session_id, cin, cin)
        st.session_state.graph_built = False
        st.success(f"Bound to {cin}")
    st.divider()
    st.header("Launch scope (NIFTY 50)")
    st.write(f"{len(NIFTY_50)} tickers in day-one scope")
    st.caption("Resolver cache: data/india/ticker_cin_cache.json")

# -- main -----------------------------------------------------------------

if not st.session_state.bound_cin:
    st.info("Select a company from the sidebar to begin.")
    st.stop()

cin = st.session_state.bound_cin

# Build graph JIT on first interaction
if not st.session_state.graph_built:
    with st.spinner(f"Building graph for {cin} from NSE + BSE + MCA…"):
        try:
            graph = build_graph_for_session(
                st.session_state.session_id,
                cin,
                st.session_state.bound_cin,
                config=config,
                resolver=resolver,
            )
            st.session_state.graph = graph
            st.session_state.graph_built = True
            session_mgr.record_question(st.session_state.session_id)
        except Exception as exc:
            st.error(f"Graph build failed: {exc}")
            st.stop()

graph: SessionGraph = st.session_state.graph

# Show company summary
comp_node = graph.find_company_node()
if comp_node:
    comp = graph.graph.nodes[comp_node]
    st.subheader(f"{comp.get('name', cin)} ({cin})")
    col1, col2, col3 = st.columns(3)
    col1.metric("Filings", len(graph.find_filings()))
    col2.metric("Announcements", len(graph.find_announcements()))
    col3.metric("Metrics", len(graph.find_metrics()))

# -- Q&A ------------------------------------------------------------------

st.divider()
st.header("Ask a question")

for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.write(msg["content"])
        if msg.get("citations"):
            with st.expander("Evidence", expanded=False):
                for cit in msg["citations"]:
                    st.caption(cit)

if prompt := st.chat_input("e.g. What is RELIANCE's latest price?"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.write(prompt)

    # Gather evidence from graph
    nodes = graph.as_nodes()
    edges = graph.as_edges()
    tag_map = graph.tag_map()
    evidence = build_evidence(nodes, edges, tag_map)

    # Simple template-based answer (no LLM key configured yet)
    # In production, wire an LLM here that cites evidence tags.
    answer_parts: list[str] = []
    citations: list[str] = []
    if evidence:
        answer_parts.append(f"Here's what I found for {cin} from NSE, BSE and MCA filings:")
        for ev in evidence[:6]:
            answer_parts.append(f"• [{ev.tag}] {ev.text}  ⟵ {ev.source.cite()}")
            citations.append(ev.block())
    else:
        answer_parts.append(f"No data found yet for {cin}. NSE/BSE scraping may be blocked by anti-bot protection; MCA master data may require direct fetch. This is reported as GAP — no figures were fabricated.")
        citations.append("[GAP] No citable evidence found for this company. → try NSE/BSE direct fetch or MCA master data.")

    answer = "\n".join(answer_parts)

    # Grade the answer
    graded = grade_answer(answer, evidence, question=prompt)
    verdict = graded.verdict
    verdict_color = {"SUPPORTED": "green", "QUALIFIED": "orange", "REFUSED": "red"}.get(verdict, "gray")

    with st.chat_message("assistant"):
        st.write(answer)
        st.markdown(f"**Verdict:** :{verdict_color}[{verdict}]")
        if graded.violations():
            for v in graded.violations():
                st.warning(v)
        with st.expander("Evidence block", expanded=False):
            for cit in citations:
                st.caption(cit)

    st.session_state.messages.append({
        "role": "assistant",
        "content": answer,
        "citations": citations,
        "verdict": verdict,
    })
    session_mgr.record_question(st.session_state.session_id)
