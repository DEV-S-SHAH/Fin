"""Tests for the ticker -> CIN resolver."""

import json
from datetime import datetime, timezone

from india_graph.config import IndiaConfig
from india_graph.resolver import TickerCINResolver


def test_resolve_cached_mapping(tmp_path):
    cache = tmp_path / "ticker_cin.json"
    cache.write_text(json.dumps({
        "loaded_at": datetime.now(timezone.utc).isoformat(),
        "symbol_to_cin": {"RELIANCE": "L17110MH1973PLC019786", "TCS": "L22210MH1995PLC084781"},
        "cin_to_symbol": {"L17110MH1973PLC019786": "RELIANCE", "L22210MH1995PLC084781": "TCS"},
    }))
    cfg = IndiaConfig(resolver_cache_path=cache)
    r = TickerCINResolver(cfg)
    assert r.resolve("RELIANCE") == "L17110MH1973PLC019786"
    assert r.resolve("tcs") == "L22210MH1995PLC084781"  # case-insensitive
    assert r.resolve("UNKNOWN") is None


def test_register_and_bulk_register():
    cfg = IndiaConfig()
    r = TickerCINResolver(cfg)
    r.register("INFY", "L85110KA1981PLC004174")
    assert r.resolve("INFY") == "L85110KA1981PLC004174"
    assert r.is_cached("INFY")

    r.bulk_register([("WIPRO", "L45101KA1945PLC020844"), ("HDFCBANK", "L99999MH1994PLC081704")])
    assert r.resolve("WIPRO") == "L45101KA1945PLC020844"
    assert r.resolve("HDFCBANK") == "L99999MH1994PLC081704"


def test_stats():
    cfg = IndiaConfig()
    r = TickerCINResolver(cfg)
    r.bulk_register([("A", "C1"), ("B", "C2")])
    s = r.stats()
    assert s["symbols_cached"] >= 2
    assert s["cins_cached"] >= 2
