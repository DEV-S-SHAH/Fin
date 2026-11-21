"""Ticker -> CIN resolver.

Maps an NSE/BSE trading symbol (e.g. RELIANCE) to its MCA Corporate
Identification Number (CIN). The CIN is the canonical company identity; the
ticker is an alias.

The mapping is built once at setup from MCA master data + exchange company
directories, cached to disk, and refreshed weekly. MCA is queried live only as
a fallback for unresolved tickers. Unresolved tickers are reported as GAP —
never guessed.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import IndiaConfig

#: How long the cached mapping is trusted before a refresh.
CACHE_TTL_DAYS = 7


class TickerCINResolver:
    """Resolves trading symbols to MCA CINs, with a disk-backed cache."""

    def __init__(self, config: IndiaConfig | None = None, cache_path: Path | None = None) -> None:
        self.config = config or IndiaConfig()
        self.cache_path = cache_path or self.config.resolver_cache_path
        self._lock = threading.Lock()
        self._symbol_to_cin: dict[str, str] = {}
        self._cin_to_symbol: dict[str, str] = {}
        self._loaded_at: datetime | None = None
        self._load()

    # -- cache --------------------------------------------------------------

    def _load(self) -> None:
        """Load the cached mapping from disk if present and fresh."""
        try:
            if not self.cache_path.exists():
                self._loaded_at = None
                return
            payload = json.loads(self.cache_path.read_text())
            self._symbol_to_cin = {k.upper(): v for k, v in payload.get("symbol_to_cin", {}).items()}
            self._cin_to_symbol = {k.upper(): v for k, v in payload.get("cin_to_symbol", {}).items()}
            ts = payload.get("loaded_at")
            self._loaded_at = datetime.fromisoformat(ts) if ts else None
        except (json.JSONDecodeError, OSError, ValueError):
            self._symbol_to_cin = {}
            self._cin_to_symbol = {}
            self._loaded_at = None

    def _save(self) -> None:
        """Persist the mapping to disk (atomic-ish: write then replace)."""
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        payload = {
            "loaded_at": datetime.now(timezone.utc).isoformat(),
            "symbol_to_cin": self._symbol_to_cin,
            "cin_to_symbol": self._cin_to_symbol,
        }
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(self.cache_path)

    def _stale(self) -> bool:
        if self._loaded_at is None:
            return True
        return datetime.now(timezone.utc) - self._loaded_at > timedelta(days=CACHE_TTL_DAYS)

    # -- public API ---------------------------------------------------------

    def resolve(self, symbol: str) -> str | None:
        """Return the MCA CIN for *symbol*, or None if unresolved.

        Checks the in-memory cache first; refreshes from disk if stale.
        Does not hit MCA live — that is :meth:`resolve_live`.
        """
        symbol = symbol.upper().strip()
        with self._lock:
            if self._stale():
                self._load()
            return self._symbol_to_cin.get(symbol)

    def reverse_resolve(self, cin: str) -> str | None:
        """Return the trading symbol for an MCA *cin*, or None if unresolved.

        Used by the exchange scrapers which are keyed on symbol, not CIN.
        """
        cin = cin.strip().upper()
        with self._lock:
            if self._stale():
                self._load()
            return self._cin_to_symbol.get(cin)

    def resolve_live(self, symbol: str) -> str | None:
        """Query MCA live for *symbol* (fallback when cache misses).

        The real implementation scrapes MCA's company-master search. This
        stub returns None so unresolved tickers surface as GAP rather than
        being guessed. Wire in the MCA search endpoint when available.
        """
        # TODO: Implement MCA live symbol->CIN lookup (MCA21 public search).
        return None

    @classmethod
    def load_seed(cls, path: str | Path, config: IndiaConfig | None = None) -> TickerCINResolver:
        """Build a resolver from a seed JSON file produced by a setup-time MCA
        + exchange-directory build.

        Expected file shape::

            {
              "loaded_at": "2026-11-20T19:12:52Z",
              "symbol_to_cin": {"RELIANCE": "L17110MH1973PLC019786", ...},
              "cin_to_symbol": {"L17110MH1973PLC019786": "RELIANCE", ...}
            }

        This is the supported way to populate the day-one NIFTY 50 cache
        offline — never guess CINs.
        """
        cfg = config or IndiaConfig()
        resolver = cls(cfg)
        payload = json.loads(Path(path).read_text())
        mappings = [
            (sym, cin) for sym, cin in payload.get("symbol_to_cin", {}).items()
        ]
        resolver.bulk_register(mappings)
        return resolver

    def register(self, symbol: str, cin: str) -> None:
        """Record a symbol->CIN mapping (called during setup build or MCA crawl)."""
        symbol = symbol.upper().strip()
        cin = cin.strip().upper()
        with self._lock:
            self._symbol_to_cin[symbol] = cin
            self._cin_to_symbol[cin] = symbol
            self._loaded_at = datetime.now(timezone.utc)

    def bulk_register(self, mappings: list[tuple[str, str]]) -> None:
        """Register many symbol->CIN mappings at once (setup-time build)."""
        with self._lock:
            for symbol, cin in mappings:
                self._symbol_to_cin[symbol.upper().strip()] = cin.strip().upper()
                self._cin_to_symbol[cin.strip().upper()] = symbol.upper().strip()
            self._loaded_at = datetime.now(timezone.utc)
            self._save()

    def is_cached(self, symbol: str) -> bool:
        return symbol.upper().strip() in self._symbol_to_cin

    def stats(self) -> dict[str, int]:
        return {
            "symbols_cached": len(self._symbol_to_cin),
            "cins_cached": len(self._cin_to_symbol),
        }
