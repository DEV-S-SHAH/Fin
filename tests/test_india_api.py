"""Tests for the India Company Graph FastAPI backend."""

from fastapi.testclient import TestClient

from india_graph.api.main import app


def test_health():
    client = TestClient(app)
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_create_session():
    client = TestClient(app)
    res = client.post("/api/session")
    assert res.status_code == 200
    data = res.json()
    assert "session_id" in data
    assert len(data["session_id"]) > 0


def test_bind_company_unknown_ticker():
    client = TestClient(app)
    # Create session first
    s = client.post("/api/session").json()
    sid = s["session_id"]

    res = client.post(f"/api/session/{sid}/bind", json={"ticker": "NOTAREALSYMBOL"})
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is False
    assert "error" in data


def test_bind_company_invalid_session():
    client = TestClient(app)
    # Session doesn't exist -> 404 before ticker resolution
    res = client.post("/api/session/nonexistent/bind", json={"ticker": "RELIANCE"})
    assert res.status_code == 404


def test_bind_company_success_with_cached_ticker():
    client = TestClient(app)
    # Access module-level deps by creating a session then binding a known ticker
    s = client.post("/api/session").json()
    sid = s["session_id"]
    # Manually register RELIANCE in the resolver cache
    from india_graph.api.routes import _deps
    _, resolver_api, _ = _deps()
    resolver_api.register("RELIANCE", "L17110MH1973PLC019786")

    res = client.post(f"/api/session/{sid}/bind", json={"ticker": "RELIANCE"})
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["cin"] == "L17110MH1973PLC019786"
    assert data["company_name"] == "RELIANCE"


def test_ask_without_bound_company():
    client = TestClient(app)
    s = client.post("/api/session").json()
    sid = s["session_id"]

    res = client.post(f"/api/session/{sid}/ask", json={"question": "What is revenue?"})
    assert res.status_code == 400


def test_list_companies():
    client = TestClient(app)
    res = client.get("/api/companies")
    assert res.status_code == 200
    data = res.json()
    assert isinstance(data, list)
    assert len(data) > 0
    assert "RELIANCE" in [c["ticker"] for c in data]


def test_delete_session():
    client = TestClient(app)
    s = client.post("/api/session").json()
    sid = s["session_id"]

    res = client.delete(f"/api/session/{sid}")
    assert res.status_code == 200
    assert res.json() == {"ok": True}

    # Session should be gone
    res2 = client.get(f"/api/session/{sid}/graph")
    assert res2.status_code == 404


def test_ask_with_fixtures_returns_grounded_answer():
    client = TestClient(app)
    # Seed resolver with RELIANCE
    from india_graph.api.routes import _deps
    _, resolver_api, _ = _deps()
    resolver_api.register("RELIANCE", "L17110MH1973PLC019786")

    s = client.post("/api/session").json()
    sid = s["session_id"]

    # Bind with fixtures (offline sample data)
    res = client.post(f"/api/session/{sid}/bind", json={
        "ticker": "RELIANCE", "use_fixtures": True,
    })
    assert res.status_code == 200
    assert res.json()["ok"] is True

    # Ask about price
    res = client.post(f"/api/session/{sid}/ask", json={"question": "What is RELIANCE price?"})
    assert res.status_code == 200
    data = res.json()
    assert data["verdict"] == "SUPPORTED"
    assert "2950.50" in data["answer"]
    assert len(data["citations"]) >= 1

    # Ask about revenue
    res = client.post(f"/api/session/{sid}/ask", json={"question": "What is RELIANCE revenue?"})
    assert res.status_code == 200
    data = res.json()
    assert data["verdict"] == "SUPPORTED"
    assert "3750000000000" in data["answer"]

    # Graph endpoint returns nodes/edges
    res = client.get(f"/api/session/{sid}/graph")
    assert res.status_code == 200
    g = res.json()
    assert len(g["nodes"]) >= 1
    assert len(g["edges"]) >= 1

    # Company detail endpoint
    res = client.get(f"/api/session/{sid}/company")
    assert res.status_code == 200
    comp = res.json()
    assert comp["cin"] == "L17110MH1973PLC019786"
    assert comp["name"] == "RELIANCE"
