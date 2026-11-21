"""Offline sample fixtures for the India Company Graph.

Bundled sample data so the graph -> evidence -> Q&A flow can be demonstrated
without hitting NSE/BSE/MCA. Filenames are keyed by MCA CIN. Clearly sample
data — not real filings.
"""

from .hdfcbank import HDFCBANK_FIXTURE
from .reliance import RELIANCE_FIXTURE

__all__ = ["HDFCBANK_FIXTURE", "RELIANCE_FIXTURE"]

_FIXTURES = {
    "L17110MH1973PLC019786": RELIANCE_FIXTURE,
    "L99999MH1994PLC081704": HDFCBANK_FIXTURE,
}


def load(cin: str) -> dict | None:
    """Return the fixture dict for *cin*, or None if no fixture exists."""
    return _FIXTURES.get(cin.strip().upper())
