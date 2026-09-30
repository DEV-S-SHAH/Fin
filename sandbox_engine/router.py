"""Discriminated entity router for Cold-Start JIT Graph RAG.

Decouples query routing from question synthesis. Routes queries to:
- KNOWN: Entity is present in the knowledge graph.
- COLD_START: Entity identified from query but not indexed in the knowledge graph.
- AMBIGUOUS: No clear entity could be identified. Never falls back to default tickers.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, NamedTuple, Optional


class EntityRoute(Enum):
    KNOWN = "KNOWN"
    COLD_START = "COLD_START"
    AMBIGUOUS = "AMBIGUOUS"


class RoutingResult(NamedTuple):
    route: EntityRoute
    ticker: Optional[str]
    entity_name: Optional[str]
    reason: str


class RouterDatabaseError(RuntimeError):
    """Raised when the database connection or query fails during routing."""
    pass


#: Possessive suffix pattern
_POSSESSIVE_RE = re.compile(r"['\u2019]s\b")

#: Cashtag pattern (e.g. $AAPL, $RIVN)
_CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,5})\b")

#: Uppercase token pattern (1-5 letters)
_UPPERCASE_RE = re.compile(r"\b([A-Z]{1,5})\b")

#: Stop words to ignore when scanning uppercase tokens
FINANCIAL_STOP_WORDS: frozenset[str] = frozenset({
    # Question / interrogative words
    "WHAT", "WHEN", "WHERE", "WHICH", "WHO", "WHOM", "WHY", "HOW",
    # Verbs / auxiliaries
    "CAN", "COULD", "DO", "DOES", "DID", "DONE", "WILL", "WOULD", "SHALL", "SHOULD",
    "MAY", "MIGHT", "MUST", "IS", "ARE", "WAS", "WERE", "BE", "BEEN", "BEING",
    "HAVE", "HAS", "HAD", "HAVING", "GET", "GOT", "TELL", "SHOW", "GIVE", "FIND",
    # Articles / pronouns / conjunctions / prepositions
    "THE", "A", "AN", "AND", "OR", "BUT", "IF", "THEN", "ELSE", "SO", "FOR", "NOR",
    "YET", "IN", "ON", "AT", "TO", "OF", "BY", "FROM", "WITH", "WITHOUT", "ABOUT",
    "AGAINST", "BETWEEN", "INTO", "THROUGH", "DURING", "BEFORE", "AFTER", "ABOVE",
    "BELOW", "UNDER", "OVER", "UP", "DOWN", "OFF", "OUT", "NEAR", "ALL", "ANY",
    "BOTH", "EACH", "FEW", "MORE", "MOST", "OTHER", "SOME", "SUCH", "NO", "NOT",
    "ONLY", "OWN", "SAME", "THAN", "TOO", "VERY", "JUST", "ALSO", "NOW", "HERE",
    "THERE", "THIS", "THAT", "THESE", "THOSE", "I", "ME", "MY", "YOU", "YOUR",
    "HE", "HIM", "HIS", "SHE", "HER", "IT", "ITS", "WE", "US", "OUR", "THEY", "THEM", "THEIR",
    # SEC / Financial / Accounting acronyms & terms
    "FY", "SEC", "CEO", "CFO", "COO", "CTO", "CIO", "USD", "EUR", "GBP", "JPY",
    "CAD", "AUD", "GAAP", "EPS", "EBIT", "EBITDA", "CAGR", "PE", "PB", "ROE",
    "ROA", "ROIC", "DCF", "WACC", "IRR", "NPV", "IPO", "NAV", "AUM", "RAG",
    "LLM", "AI", "API", "ML", "NLP", "CPU", "GPU", "TPU",
    "Q1", "Q2", "Q3", "Q4", "H1", "H2", "K", "Q", "YOY", "MOM", "QOQ", "TTM",
    "ITEM", "NOTE", "FORM", "RISK", "RISKS", "COST", "COSTS", "DEBT", "CASH",
    "TAX", "TAXES", "NET", "GROSS", "TOTAL", "FLOW", "FLOWS", "SHARE", "SHARES",
    "GROWTH", "MARGIN", "INCOME", "PROFIT", "LOSS", "REPORT", "FILING", "YEAR",
    "ANNUAL", "LATEST", "PRICE", "VALUE", "BREAK", "DOWN", "SALES", "REVENUE",
    "TOP", "LINE", "SEGMENT", "EVENT", "CHUNK", "TABLE", "DATA", "CORPUS",
    "JP",
})

#: Built-in company aliases mapping ticker -> (entity_name, aliases)
COMPANY_ALIASES: dict[str, tuple[str, tuple[str, ...]]] = {
    "AAPL": (
        "Apple Inc.",
        ("apple", "apple inc", "aapl", "iphone", "ipad", "mac", "wearables", "airpods"),
    ),
    "MSFT": (
        "Microsoft Corporation",
        (
            "microsoft",
            "microsoft corporation",
            "msft",
            "azure",
            "office 365",
            "office365",
            "windows",
            "xbox",
            "intelligent cloud",
            "more personal computing",
        ),
    ),
    "NVDA": (
        "NVIDIA Corporation",
        ("nvidia", "nvidia corporation", "nvda", "h100", "h200", "blackwell", "grace hopper"),
    ),
    "RIVN": (
        "Rivian Automotive, Inc.",
        ("rivian", "rivian automotive", "rivian automotive inc", "rivn"),
    ),
    "TSLA": (
        "Tesla, Inc.",
        ("tesla", "tesla inc", "tesla motors", "tsla"),
    ),
    "AMZN": (
        "Amazon.com, Inc.",
        ("amazon", "amazon com", "amazon.com", "amzn", "aws"),
    ),
    "GOOGL": (
        "Alphabet Inc.",
        ("alphabet", "alphabet inc", "google", "googl", "goog"),
    ),
    "META": (
        "Meta Platforms, Inc.",
        ("meta", "meta platforms", "facebook", "instagram"),
    ),
    "JPM": (
        "JPMorgan Chase & Co.",
        (
            "jpmorgan chase co",
            "jpmorgan chase",
            "jp morgan chase co",
            "jp morgan chase",
            "jp morgan",
            "jpmorgan",
            "jpm",
        ),
    ),
}

#: Company name -> ticker, keyed on the normalised (lowercased, punctuation
#: stripped) company name. This is the table a human reads to answer "which
#: ticker does 'JP Morgan' mean?" without tracing the alias machinery.
#:
#: Longest name wins, so "jp morgan chase" resolves before "jp morgan" and
#: "jpmorgan chase" before "jpmorgan". A shorter name that is a suffix of a
#: longer one still matches, because matching is done on space-padded
#: substrings rather than whole tokens.
COMPANY_NAME_TO_TICKER: dict[str, str] = {
    # JPMorgan Chase
    "jpmorgan": "JPM",
    "jp morgan": "JPM",
    "jpmorgan chase": "JPM",
    # Alphabet / Google
    "alphabet": "GOOGL",
    "google": "GOOGL",
    # The rest of the megacaps
    "amazon": "AMZN",
    "apple": "AAPL",
    "microsoft": "MSFT",
    "meta": "META",
    "meta platforms": "META",
    "nvidia": "NVDA",
    "rivian": "RIVN",
    "tesla": "TSLA",
}

# Precompute ranked alias patterns sorted by length descending (longest pattern first)
_SORTED_ALIAS_PATTERNS: tuple[tuple[str, str, str], ...] = tuple(
    sorted(
        (
            (pattern.lower(), ticker, entity_name)
            for ticker, (entity_name, patterns) in COMPANY_ALIASES.items()
            for pattern in patterns
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )
)

#: Same longest-first ranking, built from COMPANY_NAME_TO_TICKER. Kept as its
#: own table rather than folded into _SORTED_ALIAS_PATTERNS so the name->ticker
#: contract is independently inspectable and testable.
_SORTED_NAME_TO_TICKER: tuple[tuple[str, str], ...] = tuple(
    sorted(COMPANY_NAME_TO_TICKER.items(), key=lambda item: len(item[0]), reverse=True)
)


def _strip_possessives(text: str) -> str:
    """Remove possessive 's suffixes."""
    return _POSSESSIVE_RE.sub("", text)


def _normalise_for_match(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace, and pad with spaces.

    The padding is what makes substring matching safe: without it, "jp morgan"
    would match inside "notjp morganx", and "mac" inside "machine".

    The whitespace collapse is a named local rather than a second substitution
    inside the f-string: a backslash in an f-string expression is PEP 701 and
    needs Python 3.12, and this file is imported by the test suite, so on 3.11
    it was a SyntaxError that stopped thirteen test modules collecting at all.
    Same result either way.
    """
    norm = re.sub(r"[^a-z0-9\s]", " ", text.lower())
    collapsed = re.sub(r"\s+", " ", norm).strip()
    return f" {collapsed} "


def resolve_company_name(query: str) -> tuple[Optional[str], Optional[str]]:
    """Resolve a company name mentioned in prose to its ticker.

    Matches on space-padded substrings, longest name first, so "JP Morgan
    Chase" resolves as JPM rather than as an incidental "jp morgan" inside a
    longer phrase. Returns ``(None, None)`` when no name in
    ``COMPANY_NAME_TO_TICKER`` appears.
    """
    if not isinstance(query, str) or not query.strip():
        return None, None

    padded_query = _normalise_for_match(_strip_possessives(query))
    for name, ticker in _SORTED_NAME_TO_TICKER:
        if f" {_normalise_for_match(name).strip()} " in padded_query:
            return ticker, COMPANY_ALIASES.get(ticker, (None, ()))[0]

    return None, None


def extract_candidate_entity(query: str) -> tuple[Optional[str], Optional[str]]:
    """Extract candidate ticker and entity name from a query.

    Checks:
    1. Cashtag pattern: $TICKER
    2. Company name: matches COMPANY_NAME_TO_TICKER phrases (e.g. 'JP Morgan', 'Rivian')
    3. Alias dictionary: product and brand names (e.g. 'iPhone', 'Azure')
    4. Uppercase token: TICKER (filtering out stop words)
    """
    if not isinstance(query, str) or not query.strip():
        return None, None

    # 1. Cashtag regex: $TICKER
    cashtag_match = _CASHTAG_RE.search(query)
    if cashtag_match:
        ticker = cashtag_match.group(1).upper()
        entity_name = COMPANY_ALIASES.get(ticker, (None, ()))[0]
        return ticker, entity_name

    # 2. Company name -> ticker (e.g. 'JP Morgan', 'Rivian', 'Google')
    ticker, entity_name = resolve_company_name(query)
    if ticker:
        return ticker, entity_name

    # 3. Alias dictionary matching (product/brand names not in COMPANY_NAME_TO_TICKER,
    #    e.g. 'iPhone', 'Azure', 'Blackwell')
    padded_norm = _normalise_for_match(_strip_possessives(query))

    for pattern, ticker, entity_name in _SORTED_ALIAS_PATTERNS:
        if f" {_normalise_for_match(pattern).strip()} " in padded_norm:
            return ticker, entity_name

    # 4. Uppercase token matching (unlisted / unmapped tickers like $COIN, PLTR, BABA)
    stripped = _strip_possessives(query)
    uppercase_tokens = _UPPERCASE_RE.findall(stripped)
    for token in uppercase_tokens:
        if len(token) >= 2 and token not in FINANCIAL_STOP_WORDS:
            ticker = token
            entity_name = COMPANY_ALIASES.get(ticker, (None, ()))[0]
            return ticker, entity_name

    return None, None


def _check_db_presence(kg_connection: Any, ticker: str) -> bool:
    """Test entity presence in LadybugDB.

    Raises RouterDatabaseError if database query fails.
    """
    cypher = "MATCH (c:Company {ticker: $ticker}) RETURN c.ticker LIMIT 1"
    params = {"ticker": ticker}

    try:
        if hasattr(kg_connection, "execute"):
            res = kg_connection.execute(cypher, params)
            if hasattr(res, "get_all"):
                rows = res.get_all()
            else:
                rows = list(res)
            return len(rows) > 0
        elif hasattr(kg_connection, "has_company"):
            return bool(kg_connection.has_company(ticker))
        else:
            raise TypeError(f"Unsupported kg_connection object: {type(kg_connection)}")
    except Exception as exc:
        if isinstance(exc, RouterDatabaseError):
            raise
        raise RouterDatabaseError(f"Database query failed for ticker '{ticker}': {exc}") from exc


def route_query(query: str, kg_connection: Any) -> RoutingResult:
    """Route a natural language query based on extracted entity and database presence.

    Resolution order:
    1. ``$TICKER`` cashtag.
    2. Company name matched against COMPANY_NAME_TO_TICKER, longest name first,
       so a multi-word name like "JP Morgan" resolves where a ticker regex
       cannot see it.
    3. Product/brand aliases, then bare uppercase ticker tokens.
    4. LadybugDB presence decides KNOWN vs COLD_START for whatever ticker was
       resolved. An unresolved query is AMBIGUOUS and stops here -- the
       database is never consulted, and no default ticker is substituted.

    Returns:
    - EntityRoute.KNOWN: Candidate entity found and present in knowledge graph.
    - EntityRoute.COLD_START: Candidate entity found but not indexed in knowledge graph.
    - EntityRoute.AMBIGUOUS: No clear candidate entity identified.
    """
    ticker, entity_name = extract_candidate_entity(query)

    if not ticker:
        return RoutingResult(
            route=EntityRoute.AMBIGUOUS,
            ticker=None,
            entity_name=None,
            reason="No clear entity identified in query",
        )

    exists = _check_db_presence(kg_connection, ticker)

    if exists:
        return RoutingResult(
            route=EntityRoute.KNOWN,
            ticker=ticker,
            entity_name=entity_name,
            reason=f"Entity '{ticker}' found in knowledge graph",
        )
    else:
        return RoutingResult(
            route=EntityRoute.COLD_START,
            ticker=ticker,
            entity_name=entity_name,
            reason=f"Entity '{ticker}' not found in knowledge graph. Cold start required",
        )
