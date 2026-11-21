"""REST API routes for the India Company Graph.

Endpoints:
  POST   /api/session              -> { session_id }
  POST   /api/session/{id}/bind    -> { ok, cin, company_name }
  POST   /api/session/{id}/ask     -> { answer, verdict, citations, evidence }
  GET    /api/session/{id}/graph   -> { nodes, edges }
  GET    /api/session/{id}/company -> { cin, name, sector, ... }
  GET    /api/companies            -> [{ ticker, name, cin? }]
  DELETE /api/session/{id}         -> { ok }
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi import Path as PathParam
from pydantic import BaseModel, Field

from ..config import NIFTY_50, IndiaConfig
from ..orchestrator import IndiaOrchestrator
from ..resolver import TickerCINResolver
from ..session import SessionManager

router = APIRouter()

# -- shared backend state (one process, localhost dev) -----------------------

_config: IndiaConfig | None = None
_resolver: TickerCINResolver | None = None
_session_mgr: SessionManager | None = None


def _deps() -> tuple[IndiaConfig, TickerCINResolver, SessionManager]:
    global _config, _resolver, _session_mgr
    if _config is None:
        _config = IndiaConfig()
        _resolver = TickerCINResolver(_config)
        _session_mgr = SessionManager(_config)
    return _config, _resolver, _session_mgr


# -- request / response models ----------------------------------------------

class SessionCreateRequest(BaseModel):
    pass


class SessionCreateResponse(BaseModel):
    session_id: str


class BindCompanyRequest(BaseModel):
    ticker: str = Field(..., description="NSE/BSE ticker symbol, e.g. RELIANCE")
    company_name: str | None = Field(None, description="Optional company name")
    use_fixtures: bool = Field(False, description="Use bundled offline sample data")


class BindCompanyResponse(BaseModel):
    ok: bool
    cin: str | None = None
    company_name: str | None = None
    error: str | None = None


class AskRequest(BaseModel):
    question: str = Field(..., description="Natural-language question about the company")


class CitationOut(BaseModel):
    tag: str
    text: str
    source: str


class AskResponse(BaseModel):
    answer: str
    verdict: str  # SUPPORTED / QUALIFIED / REFUSED
    citations: list[CitationOut]
    evidence: list[dict[str, Any]]


class GraphOut(BaseModel):
    cin: str
    company_name: str
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]


class CompanyOut(BaseModel):
    ticker: str
    name: str


class CompanyDetailOut(BaseModel):
    cin: str
    name: str
    sector: str | None = None
    industry: str | None = None
    date_of_incorporation: str | None = None
    registered_office_address: str | None = None
    source: str | None = None


# -- routes -----------------------------------------------------------------

@router.post("/session", response_model=SessionCreateResponse)
def create_session() -> SessionCreateResponse:
    _, _, mgr = _deps()
    session_id = mgr.create_session()
    return SessionCreateResponse(session_id=session_id)


@router.post("/session/{session_id}/bind", response_model=BindCompanyResponse)
def bind_company(
    session_id: str = PathParam(...),
    req: BindCompanyRequest | None = None,
) -> BindCompanyResponse:
    _config, resolver, mgr = _deps()
    if req is None:
        raise HTTPException(status_code=400, detail="Request body required")

    # Validate session exists before doing any work
    sess = mgr.get_session(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    symbol = req.ticker.strip().upper()
    cin = resolver.resolve(symbol)
    if cin is None:
        # Try MCA live lookup as fallback
        cin = resolver.resolve_live(symbol)
    if cin is None:
        return BindCompanyResponse(
            ok=False,
            error=f"CIN not found for ticker {symbol}. Not in resolver cache.",
        )
    company_name = req.company_name or symbol
    ok = mgr.bind_company(session_id, cin, company_name)
    if not ok:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    # Remember whether to use fixtures for this session
    sess["use_fixtures"] = bool(req.use_fixtures)
    return BindCompanyResponse(ok=True, cin=cin, company_name=company_name)


@router.post("/session/{session_id}/ask", response_model=AskResponse)
def ask(
    session_id: str = PathParam(...),
    req: AskRequest | None = None,
) -> AskResponse:
    config, resolver, mgr = _deps()
    sess = mgr.get_session(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    if req is None:
        raise HTTPException(status_code=400, detail="Request body required")

    cin = sess.get("cin")
    if not cin:
        raise HTTPException(status_code=400, detail="No company bound to session")

    use_fixtures = bool(sess.get("use_fixtures"))

    # Build graph JIT if not yet built
    graph = mgr.get_or_create_graph(session_id)
    if graph is None:
        raise HTTPException(status_code=500, detail="Could not create graph")

    # Only build on first ask; rebuild if forced
    if not graph.graph.nodes:
        orch = IndiaOrchestrator(
            config=config,
            resolver=resolver,
            use_fixtures=use_fixtures,
        )
        graph = orch.build_for_cin(cin, sess.get("company_name") or "")
        sess["graph"] = graph

    mgr.record_question(session_id)

    # Deterministic, provenance-grounded answer via retrieval
    orch = IndiaOrchestrator(config=config, resolver=resolver, use_fixtures=use_fixtures)
    result = orch.answer(graph, req.question)

    return AskResponse(
        answer=result["answer"],
        verdict=result["verdict"],
        citations=[CitationOut(**c) for c in result["citations"]],
        evidence=result["evidence"],
    )


@router.get("/session/{session_id}/graph", response_model=GraphOut)
def get_graph(session_id: str = PathParam(...)) -> GraphOut:
    _, _, mgr = _deps()
    sess = mgr.get_session(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    graph = mgr.get_or_create_graph(session_id)
    if graph is None:
        raise HTTPException(status_code=500, detail="Could not create graph")

    return GraphOut(
        cin=graph.cin,
        company_name=graph.company_name,
        nodes=graph.as_nodes(),
        edges=graph.as_edges(),
    )


@router.get("/session/{session_id}/company", response_model=CompanyDetailOut)
def get_company(session_id: str = PathParam(...)) -> CompanyDetailOut:
    _, _, mgr = _deps()
    sess = mgr.get_session(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail="Session not found or expired")

    graph = mgr.get_or_create_graph(session_id)
    if graph is None:
        raise HTTPException(status_code=500, detail="Could not create graph")

    comp_node = graph.find_company_node()
    if not comp_node:
        raise HTTPException(status_code=404, detail="Company not found in graph")

    data = graph.graph.nodes[comp_node]
    return CompanyDetailOut(
        cin=data.get("cin", ""),
        name=data.get("name", ""),
        sector=data.get("sector") or None,
        industry=data.get("industry") or None,
        date_of_incorporation=data.get("date_of_incorporation") or None,
        registered_office_address=data.get("registered_office_address") or None,
        source=data.get("source") or None,
    )


@router.get("/companies", response_model=list[CompanyOut])
def list_companies() -> list[CompanyOut]:
    # Day-one launch scope: NIFTY 50 tickers. Names/CINs would come from the
    # resolver cache once built; here we return tickers as placeholders.
    return [CompanyOut(ticker=t, name=t) for t in NIFTY_50]


@router.delete("/session/{session_id}")
def delete_session(session_id: str = PathParam(...)) -> dict[str, bool]:
    _, _, mgr = _deps()
    ok = mgr.destroy_session(session_id)
    return {"ok": ok}
