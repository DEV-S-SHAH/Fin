"""Universal Financial Graph Schema (UFGS-2026-09) -- the reference tables.

This module is the *vocabulary* half of the schema: the forty canonical
concept anchors, the twenty-five normalization rules that map as-filed facts
onto them, the sector/SIC registry, and the 10-K / 10-Q / 8-K item taxonomies.
It contains no I/O and no graph code, so the tables can be read (and tested)
without a database.

Reference
---------
Z.ai Knowledge Graph Practice, *Universal Financial Graph Schema -- A
Sector-Aware Ontology for SEC 10-K, 10-Q, and 8-K Ingestion*, UFGS-2026-09 v1.0,
September 2026.

The three things this schema is for
-----------------------------------

**Dual track.** A fact is never replaced by its normalized form. The as-filed
label and the XBRL tag stay on the ``RawFact``; the canonical anchor is a
separate ``StandardizedConcept`` reached by a ``NORMALIZES_TO`` edge carrying
the rule id. The spec rejects overwriting because a cross-sector corpus has no
single taxonomy to overwrite *with* -- Apple's "Net sales", NVIDIA's
"Revenues", and JPMorgan's "Total net revenue" are one economic concept with
three surface forms, and each issuer's own word is evidence worth keeping.

**Sector as a parameter, not a fork.** ``SectorOverlay`` carries the concepts
that only exist inside one GICS sector (Basel III ratios for banks, production
volumes for energy). Non-bank filers get no empty nodes for ``SC-37..SC-40``;
the overlay is simply not loaded. That is what lets the schema absorb a new
sector by adding ``SC-41+`` and ``R-26+`` without touching the universal layer.

**Period is a node.** ``FiscalPeriod`` is a first-class node rather than two
columns on ``Filing``, because "fiscal year 2023" is ambiguous across a
corpus: NVDA's FYE is the last Sunday of January and AAPL's is the last
Saturday of September, so the same string means different date ranges per
filer. ``calendar_year_overlap`` is the field that makes cross-issuer
comparison possible, and ``reporting_lag_in_days`` is the field that makes
cross-issuer *timeliness* comparison possible.

A note on the rule table
------------------------

``us-gaap`` tag names in the spec are the ones the spec's six-issuer corpus
emits, and they are not always the ones every filer emits. NVIDIA tags cash as
``us-gaap:CashEquivalentsAtCarryingValue`` where the spec's R-11 lists
``us-gaap:CashAndCashEquivalentsAtCarryingValue``. :data:`TAG_ALIASES` records
the variants seen in practice, and a match always reports the tag that *actually*
matched rather than the one the rule was written with -- otherwise the graph
claims a provenance that does not exist.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

__all__ = [
    "SCHEMA_REFERENCE",
    "SECTORS",
    "SIC_RANGES",
    "TICKER_SECTOR",
    "Concept",
    "Rule",
    "STANDARDIZED_CONCEPTS",
    "CONCEPTS_BY_ID",
    "NORMALIZATION_RULES",
    "RULES_BY_ID",
    "TAG_ALIASES",
    "TEN_K_ITEMS",
    "TEN_Q_ITEMS",
    "EIGHT_K_ITEMS",
    "ITEM_TITLES",
    "CAUSAL_RELATION_TYPES",
    "ENTITY_TYPES",
    "sector_for_sic",
    "sector_for_ticker",
    "rules_for_sector",
    "concepts_for_sector",
    "match_rule",
    "rule_id_for_tag",
    "item_title",
    "calendar_year_overlap",
]

SCHEMA_REFERENCE = "UFGS-2026-09"

# ---------------------------------------------------------------------------
# Sectors
# ---------------------------------------------------------------------------

#: The four GICS sectors the reference corpus covers. ``sector_applicability``
#: on a concept and ``sector`` on a rule are matched against these keys.
SECTORS: tuple[str, ...] = ("technology", "banking", "healthcare", "energy")

#: SIC prefix -> sector. The spec names the six sample filers' exact SIC codes
#: (AAPL 3571, NVDA 3674, JPM 6021, GS 6211, PFE 2834, XOM 2911); the ranges
#: below widen each to the rest of its major group so a filer outside the
#: sample still resolves instead of falling through to ``None``.
SIC_RANGES: tuple[tuple[str, str], ...] = (
    ("3571", "technology"),   # Electronic Computers
    ("3674", "technology"),   # Semiconductors
    ("7372", "technology"),   # Prepackaged Software
    ("7371", "technology"),   # Prepackaged Computer Programs
    ("7379", "technology"),   # Services-Computer Programming
    ("3575", "technology"),   # Computer Terminals
    ("5812", "technology"),   # Retail-Catalog/Specialty Distribution
    ("6021", "banking"),      # National Commercial Banks
    ("6022", "banking"),      # State Commercial Banks
    ("6211", "banking"),      # Security Brokers, Dealers
    ("6199", "banking"),      # Finance Services
    ("2834", "healthcare"),   # Pharmaceutical Preparations
    ("2836", "healthcare"),   # Biological Products
    ("3842", "healthcare"),   # Surgical/Instruments
    ("8071", "healthcare"),   # Services-Laboratories
    ("2911", "energy"),       # Crude Petroleum & Natural Gas
    ("2912", "energy"),       # Refined Petroleum Products
    ("2860", "energy"),       # Industrial Organic Chemicals
    ("1220", "energy"),       # Coal Mining
    ("1040", "energy"),       # Crude Petroleum
)

#: Corpus issuers, so a filing whose SIC tag is missing still resolves. A ticker
#: is the weakest signal available and the ``SEC`` filings in this repo are
#: named by ticker, so it is the only key the parser can use before it has read
#: any XBRL.
TICKER_SECTOR: dict[str, str] = {
    "AAPL": "technology",
    "NVDA": "technology",
    "MSFT": "technology",
    "AMZN": "technology",
    "META": "technology",
    "TSLA": "technology",
    "GOOGL": "technology",
    "JPM": "banking",
    "GS": "banking",
    "BAC": "banking",
    "WFC": "banking",
    "PFE": "healthcare",
    "MRK": "healthcare",
    "JNJ": "healthcare",
    "XOM": "energy",
    "CVX": "energy",
    "COP": "energy",
}


def sector_for_sic(sic: str | None) -> str | None:
    """Sector for a SIC code, by four-digit major group.

    A leading-zero SIC (``"03571"``) and a bare four-digit one both resolve,
    because SEC submissions carry both forms.
    """
    if not sic:
        return None
    digits = re.sub(r"\D", "", str(sic))
    for prefix, sector in SIC_RANGES:
        if digits.startswith(prefix):
            return sector
    return None


def sector_for_ticker(ticker: str | None) -> str | None:
    """Sector for a known corpus ticker, or ``None``."""
    if not ticker:
        return None
    return TICKER_SECTOR.get(str(ticker).strip().upper())


# ---------------------------------------------------------------------------
# Standardized concepts (SC-01 .. SC-40)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Concept:
    """One canonical anchor from the forty-concept list."""

    concept_id: str
    name: str
    definition: str
    statement_type: str
    #: ``"all"`` for the universal layer, otherwise the sector that adds the
    #: concept as an overlay. A bank-only concept is never emitted for a
    #: technology filer, which is the whole point of the overlay architecture.
    sector_applicability: str = "all"


def _c(
    concept_id: str,
    name: str,
    definition: str,
    statement_type: str,
    sector: str = "all",
) -> Concept:
    return Concept(concept_id, name, definition, statement_type, sector)


#: The forty canonical anchors, in the spec's numbering: SC-01..SC-14 income
#: statement, SC-15..SC-30 balance sheet, SC-31..SC-36 cash flow, SC-37..SC-40
#: sector-specific regulatory metrics.
STANDARDIZED_CONCEPTS: tuple[Concept, ...] = (
    # -- income statement -------------------------------------------------
    _c("SC-01", "TotalRevenue",
       "Top-line revenue inclusive of sector-specific composition",
       "income_statement"),
    _c("SC-02", "CostOfRevenue",
       "Direct cost of producing goods or services sold",
       "income_statement"),
    _c("SC-03", "GrossProfit",
       "Revenue less cost of revenue; bank equivalent is net interest income",
       "income_statement"),
    _c("SC-04", "ResearchAndDevelopment",
       "Research and development expense per ASC 730",
       "income_statement"),
    _c("SC-05", "SellingGeneralAdministrative",
       "SG&A excluding R&D; bank equivalent is occupancy plus professional services",
       "income_statement"),
    _c("SC-06", "OperatingIncome",
       "Revenue less all operating expenses; bank equivalent is pre-provision net revenue",
       "income_statement"),
    _c("SC-07", "InterestExpense",
       "Cost of interest-bearing liabilities",
       "income_statement"),
    _c("SC-08", "NonInterestIncome",
       "Fee, trading, and service-charge income; bank-only concept",
       "income_statement"),
    _c("SC-09", "ProvisionForCreditLosses",
       "Expected credit loss provisioning under CECL",
       "income_statement"),
    _c("SC-10", "IncomeBeforeTax",
       "Pre-tax income from continuing operations",
       "income_statement"),
    _c("SC-11", "IncomeTaxExpense",
       "Current plus deferred income tax expense",
       "income_statement"),
    _c("SC-12", "NetIncome",
       "Bottom-line net income attributable to common shareholders",
       "income_statement"),
    _c("SC-13", "NetIncomeFromDiscontinuedOps",
       "Post-tax income from discontinued operations under ASC 205-20",
       "income_statement"),
    _c("SC-14", "DilutedEarningsPerShare",
       "Earnings per share on the diluted share count",
       "income_statement"),
    # -- balance sheet ---------------------------------------------------
    _c("SC-15", "CashAndEquivalents",
       "Cash plus due from banks plus interest-bearing deposits",
       "balance_sheet"),
    _c("SC-16", "ShortTermInvestments",
       "Marketable securities with maturity under twelve months",
       "balance_sheet"),
    _c("SC-17", "AccountsReceivableNet",
       "Net trade receivables",
       "balance_sheet"),
    _c("SC-18", "Inventory",
       "Finished goods plus work in process plus raw materials",
       "balance_sheet"),
    _c("SC-19", "PPENet",
       "Net property, plant, and equipment; bank equivalent is premises",
       "balance_sheet"),
    _c("SC-20", "Goodwill",
       "Acquisition goodwill, unamortized",
       "balance_sheet"),
    _c("SC-21", "IntangibleAssetsNet",
       "Acquired intangibles, net of amortization",
       "balance_sheet"),
    _c("SC-22", "LoansHeldForInvestmentNet",
       "Net loans held for investment, net of allowance for credit losses",
       "balance_sheet", "banking"),
    _c("SC-23", "TradingAssets",
       "Trading inventory carried at fair value",
       "balance_sheet", "banking"),
    _c("SC-24", "TotalAssets",
       "Sum of all assets; integrity constraint TA = TL + TE",
       "balance_sheet"),
    _c("SC-25", "AccountsPayable",
       "Trade payables",
       "balance_sheet"),
    _c("SC-26", "ShortTermDebt",
       "Debt maturing within twelve months",
       "balance_sheet"),
    _c("SC-27", "LongTermDebt",
       "Debt maturing beyond twelve months",
       "balance_sheet"),
    _c("SC-28", "Deposits",
       "Customer deposit liabilities",
       "balance_sheet", "banking"),
    _c("SC-29", "TotalLiabilities",
       "Sum of all liabilities",
       "balance_sheet"),
    _c("SC-30", "TotalEquity",
       "Shareholders' equity including non-controlling interest",
       "balance_sheet"),
    # -- cash flow -------------------------------------------------------
    _c("SC-31", "CashFromOperations",
       "Operating cash flow before working capital changes",
       "cash_flow"),
    _c("SC-32", "CapitalExpenditure",
       "Cash spent on property, plant, and equipment additions",
       "cash_flow"),
    _c("SC-33", "AcquisitionsNetOfDivestitures",
       "Cash used for mergers and acquisitions, net of disposals",
       "cash_flow"),
    _c("SC-34", "DividendsPaid",
       "Common plus preferred dividends paid",
       "cash_flow"),
    _c("SC-35", "ShareRepurchases",
       "Common stock buybacks",
       "cash_flow"),
    _c("SC-36", "DebtIssuanceNet",
       "Net debt issuance or repayment",
       "cash_flow"),
    # -- sector overlays -------------------------------------------------
    _c("SC-37", "Tier1CapitalRatio",
       "Basel III Tier 1 capital divided by risk-weighted assets",
       "regulatory_metric", "banking"),
    _c("SC-38", "CommonEquityTier1Ratio",
       "CET1 capital divided by risk-weighted assets",
       "regulatory_metric", "banking"),
    _c("SC-39", "RiskWeightedAssets",
       "Basel III risk-weighted asset base",
       "regulatory_metric", "banking"),
    _c("SC-40", "ValueAtRiskOneDay",
       "One-day value at risk",
       "regulatory_metric", "banking"),
)

CONCEPTS_BY_ID: dict[str, Concept] = {c.concept_id: c for c in STANDARDIZED_CONCEPTS}


def concepts_for_sector(sector: str | None) -> tuple[Concept, ...]:
    """Concepts applicable to *sector*: the universal layer plus its overlay.

    A filer whose sector could not be determined gets the universal layer only.
    Emitting the banking overlay for an unidentified filer would put capital
    ratios in the graph for a company that has none, which is worse than the
    concept being absent.
    """
    return tuple(
        c for c in STANDARDIZED_CONCEPTS
        if c.sector_applicability == "all" or c.sector_applicability == sector
    )


# ---------------------------------------------------------------------------
# Normalization rules (R-01 .. R-25)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """One XBRL-tag-or-regex -> canonical concept mapping.

    ``tags`` are exact ``us-gaap`` tag names. ``label_patterns`` are the
    fallback for filings with no usable XBRL: a filing whose tables parsed but
    whose fact tags did not still normalizes, on the as-filed label. A rule can
    carry both, and :func:`match_rule` prefers the tag.

    ``transform`` is the spec's enum: ``identity``, ``sum``, ``difference``,
    ``negate``, or ``sector_conditional``. ``negate`` is load-bearing for cash
    flow: ``PaymentsToAcquirePropertyPlantAndEquipment`` is an outflow reported
    as a positive magnitude, so capital expenditure is its negation. Storing
    the raw sign would make ``CapitalExpenditure`` rank alongside inflows.
    """

    rule_id: str
    target: str
    transform: str
    notes: str
    tags: tuple[str, ...] = ()
    label_patterns: tuple[str, ...] = ()
    sector: str | None = None

    def applies_to_sector(self, sector: str | None) -> bool:
        return self.sector is None or self.sector == sector


def _r(
    rule_id: str,
    target: str,
    transform: str,
    notes: str,
    *,
    tags: Iterable[str] = (),
    labels: Iterable[str] = (),
    sector: str | None = None,
) -> Rule:
    # ``tuple("abc")`` is ``('a', 'b', 'c')``, and a one-element ``labels=(x)``
    # is a parenthesized string rather than a tuple -- so a single pattern
    # written without a trailing comma would silently become one rule per
    # character. Normalising here keeps that mistake from compiling into a
    # lookup that matches single letters.
    def _as_tuple(value: Iterable[str]) -> tuple[str, ...]:
        return (value,) if isinstance(value, str) else tuple(value)

    return Rule(
        rule_id, target, transform, notes,
        _as_tuple(tags), _as_tuple(labels), sector,
    )


#: The twenty-five rules of the spec's Table 6.1, plus label fallbacks so a
#: filing without inline XBRL still normalizes. The label patterns are written
#: against the as-filed wording the spec's own corpus uses ("Net sales",
#: "Total revenues", "Total net revenue") -- which is also why label matching
#: is the fallback and not the primary path: it is a guess, and the graph
#: records which path fired on the edge.
NORMALIZATION_RULES: tuple[Rule, ...] = (
    _r("R-01", "SC-01", "identity",
       "AAPL, NVDA, PFE primary revenue tag",
       tags=("us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",),
       labels=(r"^revenue from contracts with customers?$", r"^net sales$", r"^net revenue$")),
    _r("R-02", "SC-01", "identity",
       "XOM primary; combine with R-08 for total revenues and other income",
       tags=("us-gaap:Revenues",),
       labels=(r"^total revenues?$", r"^revenues$", r"^total revenue$")),
    _r("R-03", "SC-01", "sum",
       "JPM, GS bank revenue construction: interest plus non-interest income",
       tags=("us-gaap:InterestIncome", "us-gaap:NoninterestIncome"),
       sector="banking"),
    _r("R-04", "SC-02", "identity",
       "AAPL, NVDA, PFE cost of sales",
       tags=("us-gaap:CostOfGoodsAndServicesSold",),
       labels=(r"^cost of (sales|revenue|goods sold)$",)),
    _r("R-05", "SC-02", "identity",
       "XOM production and distribution costs",
       tags=("us-gaap:ProductionAndDistributionExpenses",),
       labels=(r"^production and distribution expenses?$",)),
    _r("R-06", "SC-04", "identity",
       "AAPL, NVDA, PFE research and development",
       tags=("us-gaap:ResearchAndDevelopmentExpense",),
       labels=(r"^research and development$", r"^research and development expense$")),
    _r("R-07", "SC-05", "identity",
       "JPM, GS SG&A proxy via noninterest expense; sector-conditional",
       tags=("us-gaap:NoninterestExpense",),
       labels=(r"^selling, general and administrative$",
               r"^general and administrative$"),
       sector="banking"),
    _r("R-08", "SC-24", "identity",
       "All sectors; integrity constraint TA = TL + TE",
       tags=("us-gaap:Assets",),
       labels=(r"^total assets$",)),
    _r("R-09", "SC-29", "identity", "All sectors",
       tags=("us-gaap:Liabilities",),
       labels=(r"^total liabilities$",)),
    _r("R-10", "SC-30", "identity", "All sectors; includes NCI where reported",
       tags=("us-gaap:StockholdersEquity",),
       labels=(r"^total (stockholders|shareholders)['’]? equity$",)),
    _r("R-11", "SC-15", "identity",
       "All sectors; bank version includes due from banks",
       tags=("us-gaap:CashAndCashEquivalentsAtCarryingValue",),
       labels=(r"^cash and cash equivalents$",)),
    _r("R-12", "SC-16", "identity",
       "AAPL, NVDA; bank equivalent is SC-23 TradingAssets via R-13",
       tags=("us-gaap:MarketableSecuritiesCurrent",),
       labels=(r"^marketable securities$", r"^short-term investments?$")),
    _r("R-13", "SC-23", "identity", "JPM, GS only; non-bank N/A",
       tags=("us-gaap:TradingSecurities",),
       sector="banking"),
    _r("R-14", "SC-17", "identity", "AAPL, NVDA, PFE, XOM; bank N/A",
       tags=("us-gaap:AccountsReceivableNetCurrent",),
       labels=(r"^accounts receivable,? net$", r"^accounts receivable$")),
    _r("R-15", "SC-18", "identity",
       "AAPL, PFE, XOM; NVDA minimal (fabless); bank N/A",
       tags=("us-gaap:InventoryNet",),
       labels=(r"^inventories$",)),
    _r("R-16", "SC-19", "identity", "All sectors; XOM largest; bank equals premises",
       tags=("us-gaap:PropertyPlantAndEquipmentNet",),
       labels=(r"^property,? plant and equipment,? net$",)),
    _r("R-17", "SC-20", "identity", "All sectors; PFE elevated post-Seagen",
       tags=("us-gaap:Goodwill",),
       labels=(r"^goodwill$",)),
    _r("R-18", "SC-22", "identity", "JPM, GS only; non-bank N/A",
       tags=("us-gaap:LoansAndLeasesReceivableNetOfAllowanceForCreditLosses",),
       sector="banking"),
    _r("R-19", "SC-27", "identity", "All sectors; current portion is SC-26",
       tags=("us-gaap:LongTermDebt",),
       labels=(r"^long-term debt$",)),
    _r("R-20", "SC-28", "identity", "JPM, GS only",
       tags=("us-gaap:Deposits",),
       sector="banking"),
    _r("R-21", "SC-31", "identity", "All sectors",
       tags=("us-gaap:NetCashProvidedByUsedInOperatingActivities",),
       labels=(r"^net cash provided by operating activities$",)),
    _r("R-22", "SC-32", "negate",
       "All sectors; XOM and NVDA largest as a share of revenue",
       tags=("us-gaap:PaymentsToAcquirePropertyPlantAndEquipment",),
       labels=(r"^purchases? of property,? plant and equipment$",)),
    _r("R-23", "SC-35", "negate", "All sectors; AAPL largest program in the corpus",
       tags=("us-gaap:PaymentsForRepurchaseOfCommonStock",),
       labels=(r"^repurchases? of common stock$",)),
    _r("R-24", "SC-34", "negate", "All sectors; separate where reported",
       tags=("us-gaap:PaymentsOfDividends", "us-gaap:PaymentsOfDividendsCommonStock"),
       labels=(r"^(payments of )?dividends paid$",)),
    _r("R-25", "SC-37", "identity", "JPM, GS only; standardized overlay",
       tags=("us-gaap:TierOneCapitalRatio",),
       sector="banking"),
)

RULES_BY_ID: dict[str, Rule] = {r.rule_id: r for r in NORMALIZATION_RULES}

#: Tag variants seen in the wild that the spec's rule table does not list. The
#: rule's *target* concept is unchanged; only the tag that matches it moves.
#: Keyed by rule id, so adding a variant never silently re-points a rule at a
#: different concept.
TAG_ALIASES: dict[str, tuple[str, ...]] = {
    # NVDA tags cash as CashEquivalentsAtCarryingValue, not
    # CashAndCashEquivalentsAtCarryingValue.
    "R-11": ("us-gaap:CashEquivalentsAtCarryingValue",),
    "R-12": ("us-gaap:MarketableSecurities", "us-gaap:AvailableForSaleSecuritiesDebtSecuritiesCurrent"),
    "R-16": ("us-gaap:PropertyPlantAndEquipmentNet",
             "us-gaap:PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization"),
    "R-19": ("us-gaap:LongTermDebtNoncurrent", "us-gaap:LongTermDebtAndCapitalLeaseObligations"),
    "R-23": ("us-gaap:PaymentsForRepurchaseOfEquity",
             "us-gaap:StockRepurchasedAndRetiredDuringPeriodValue"),
    "R-24": ("us-gaap:PaymentsOfDividendsCommonStock",
             "us-gaap:PaymentsOfDividends"),
    "R-01": ("us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",),
}


def rules_for_sector(sector: str | None) -> tuple[Rule, ...]:
    """Rules applicable to *sector*: the universal ones plus its overlay."""
    return tuple(r for r in NORMALIZATION_RULES if r.applies_to_sector(sector))


def _tags_of(rule: Rule) -> tuple[str, ...]:
    return tuple(rule.tags) + TAG_ALIASES.get(rule.rule_id, ())


#: tag (lowercased) -> rule, built once. The XBRL tag is the primary key for
#: normalization, so this is the map the parser hits for every fact.
_TAG_INDEX: dict[str, Rule] = {
    tag.lower(): rule
    for rule in NORMALIZATION_RULES
    for tag in _tags_of(rule)
}

#: compiled label matchers, in rule order. Order is the spec's: rules are
#: "ordered by concept family and applied in sequence", and a fact that matches
#: two rules keeps both via separate normalization edges -- so this is a list,
#: not a first-match-wins lookup.
_LABEL_INDEX: tuple[tuple[Rule, re.Pattern[str]], ...] = tuple(
    (rule, re.compile(pattern, re.I))
    for rule in NORMALIZATION_RULES
    for pattern in rule.label_patterns
)


def rule_id_for_tag(tag: str | None) -> str | None:
    """Rule id for an exact ``us-gaap`` tag, or ``None`` if unmapped."""
    if not tag:
        return None
    rule = _TAG_INDEX.get(tag.strip().lower())
    return rule.rule_id if rule else None


def match_rule(
    tag: str | None,
    as_filed_label: str | None = None,
    sector: str | None = None,
) -> list[dict[str, Any]]:
    """Normalization rules that fire for one fact.

    Returns a list rather than a single rule, and deliberately so: the spec
    requires that a fact matching several rules keeps *every* mapping as a
    separate ``NORMALIZES_TO`` edge, because JPMorgan's interest expense is
    both ``SC-07`` in its own right and a component of the bank revenue
    construction in R-03. Collapsing to one rule would silently drop the second.

    Each hit records ``matched_on`` (``"xbrl_tag"`` or ``"label"``) and
    ``matched_value`` -- the actual tag or label that fired -- so a reader can
    tell a tag match from a label guess without re-running the rule engine.
    Sector-conditional rules are filtered out for other sectors; a non-bank
    filer emitting ``us-gaap:NoninterestExpense`` is rare but legal for a
    diversified issuer with a financial subsidiary, and the spec sends that
    case to manual review rather than to SC-05.
    """
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()

    if tag:
        rule = _TAG_INDEX.get(tag.strip().lower())
        if rule is not None and rule.applies_to_sector(sector):
            hits.append({
                "rule_id": rule.rule_id,
                "concept_id": rule.target,
                "transform": rule.transform,
                "matched_on": "xbrl_tag",
                "matched_value": tag.strip(),
                "sector_conditional": rule.sector is not None,
            })
            seen.add(rule.rule_id)

    label = (as_filed_label or "").strip()
    if label:
        for rule, pattern in _LABEL_INDEX:
            if rule.rule_id in seen or not rule.applies_to_sector(sector):
                continue
            if pattern.search(label):
                hits.append({
                    "rule_id": rule.rule_id,
                    "concept_id": rule.target,
                    "transform": rule.transform,
                    "matched_on": "label",
                    "matched_value": label,
                    "sector_conditional": rule.sector is not None,
                })
                seen.add(rule.rule_id)

    return hits


# ---------------------------------------------------------------------------
# Form taxonomies
# ---------------------------------------------------------------------------

#: Form 10-K item -> (title, schema role). ``Item 10-16`` is a single entry: the
#: spec treats the proxy-incorporated back half as a pointer rather than absent
#: content, because a query for director information should still find a node
#: to traverse from.
TEN_K_ITEMS: dict[str, tuple[str, str]] = {
    "1": ("Business", "ProductFamily, Competitor, Supplier, Customer, GeographicMarket source"),
    "1A": ("Risk Factors", "RiskFactor and causal graph source"),
    "1B": ("Unresolved Staff Comments", "optional, often empty"),
    "1C": ("Cybersecurity", "CyberRisk and mitigation relations; new under Item 1C of Reg S-K 2023"),
    "2": ("Properties", "GeographicAsset source"),
    "3": ("Legal Proceedings", "LegalMatter source"),
    "4": ("Mine Safety", "Energy sector specific"),
    "5": ("Market for Registrant's Common Equity", "ShareRepurchases and DividendsPaid source"),
    "6": ("Selected Financial Data", "largely eliminated by the FAST Act; optional"),
    "7": ("Management's Discussion and Analysis", "Causal narrative and comparison facts source"),
    "7A": ("Quantitative and Qualitative Market Risk", "VaR, interest rate sensitivity, FX"),
    "8": ("Financial Statements and Supplementary Data", "Core RawFact source"),
    "9": ("Changes in Disagreement with Accountants", "Audit quality signal"),
    "9A": ("Controls and Procedures", "ICFR weakness source"),
    "9B": ("Other Information", "catch-all"),
    "9C": ("Disclosure Regarding Foreign Jurisdictions", "HFCAA compliance flag"),
    "10-16": ("Director, Executive Officer and Corporate Governance",
              "incorporated by reference from the proxy statement; pointer only"),
}

#: Form 10-Q. The critical invariant is not structural: every RawFact from a
#: 10-Q is ``unaudited``, and Chapter 7 depends on that distinction to decide
#: whether a later 10-K/A is a restatement of an audited fact or a revision of
#: a preliminary one.
TEN_Q_ITEMS: dict[str, tuple[str, str]] = {
    "1": ("Financial Statements", "Condensed statements; all facts unaudited"),
    "2": ("Management's Discussion and Analysis", "Quarter-over-quarter and YTD comparison"),
    "3": ("Quantitative and Qualitative Market Risk", "sector-conditional"),
    "4": ("Controls and Procedures", "ICFR weakness source"),
    "1.1": ("Legal Proceedings", "Part II"),
    "1.2": ("Unregistered Sales of Equity Securities", "Part II"),
    "1.3": ("Material Defaults on Senior Securities", "Part II"),
    "1.4": ("Mine Safety Disclosures", "Part II; Energy sector only"),
    "1.5": ("Other Information", "Part II"),
    "1.6": ("Exhibits", "Part II"),
}

#: Form 8-K, by section. Sub-item codes are kept as ranges because the ~30
#: sub-items are the part that carries the event semantics.
EIGHT_K_ITEMS: dict[str, tuple[str, str]] = {
    "1": ("Entry into a Material Definitive Agreement", "1.01-1.04 MaterialAgreement"),
    "2": ("Completion of Acquisition or Disposition of Assets",
          "2.01-2.07 acquisition, results of operations, bankruptcy"),
    "3": ("Material Modification to Rights of Security Holders",
          "3.01-3.07 bankruptcy, receivership, delisting, ListingStatusChange"),
    "4": ("Changes in Registrant's Certifying Accountant",
          "4.01-4.02 AuditorChange; ICFR review flag"),
    "5": ("Other Information", "5.01-5.08; 5.02 carries executive appointments"),
    "6": ("Absence of Material Changes", "no-op"),
    "7": ("Regulation FD Disclosure", "7.01 Regulation FD"),
    "8": ("Other Events", "8.01"),
    "9": ("Financial Statements and Exhibits", "9.01"),
}

ITEM_TITLES: dict[str, dict[str, tuple[str, str]]] = {
    "10-K": TEN_K_ITEMS,
    "10-Q": TEN_Q_ITEMS,
    "8-K": EIGHT_K_ITEMS,
}


def item_title(form_type: str, item_code: str) -> str:
    """Title for an item code under *form_type*, or ``""`` if unknown."""
    code = (item_code or "").strip().upper()
    if not code:
        return ""
    table = ITEM_TITLES.get((form_type or "").strip().upper(), {})
    if code in table:
        return table[code][0]
    # "Item 7A" reported as "7a", or an 8-K sub-item "2.01" reported as "2".
    for candidate, (title, _role) in table.items():
        if candidate.upper() == code:
            return title
    head = code.split(".")[0]
    if head in table:
        return table[head][0]
    return ""


# ---------------------------------------------------------------------------
# Causal layer
# ---------------------------------------------------------------------------

#: The six typed causal relations. The spec rejects a generic ``related_to``
#: because "all things related to inflation" returns an unmanageable tangle of
#: weak signals, while "which issuers explicitly state that inflation
#: IMPACTS_MARGIN" returns a focused, comparable set.
CAUSAL_RELATION_TYPES: tuple[str, ...] = (
    "DRIVES",
    "IMPACTS_MARGIN",
    "MITIGATES",
    "CREATES_EXPOSURE_TO",
    "COMPOUNDS",
    "OFFSETS",
)

#: The seven narrative entity types, each with a LadybugDB node table. The spec
#: caps this at seven on purpose: more fragments the graph, fewer loses
#: discriminative power.
ENTITY_TYPES: tuple[str, ...] = (
    "ProductFamily",
    "GeographicMarket",
    "Competitor",
    "Supplier",
    "Customer",
    "RegulatoryBody",
    "MacroVariable",
)


# ---------------------------------------------------------------------------
# Fiscal periods
# ---------------------------------------------------------------------------


def calendar_year_overlap(period_start: str | None, period_end: str | None) -> int | None:
    """The calendar year holding the majority of a fiscal period's days.

    This is the field that makes a cross-issuer query possible. "Revenue in
    calendar 2023" means something different for each filer in the spec's
    corpus: for a December-31 filer it is FY2023, but NVDA's fiscal year ends
    the last Sunday of January, so its FY24 (ending 2024-01-28) contributes more
    days to calendar 2023 than to calendar 2024, and the query must select
    FY24 -- not FY23 -- for that filer.

    A period shorter than a month is attributed to the year of its end date,
    because a 10-K balance-sheet instant context has a single date and no
    meaningful "majority of days". Returns ``None`` when either bound is
    missing or unparseable, which is stored as the fiscal-year sentinel rather
    than guessed: an issuer that lied about its own year-end is rarer than a
    parse that failed, and a wrong overlap silently re-aligns a whole time
    series.
    """
    import datetime as _dt

    def _d(value: str | None) -> _dt.date | None:
        if not value:
            return None
        try:
            return _dt.date.fromisoformat(str(value).strip()[:10])
        except ValueError:
            return None

    start, end = _d(period_start), _d(period_end)
    if end is None:
        return None
    if start is None:
        return end.year
    span = (end - start).days + 1
    if span <= 0:
        return end.year
    if span < 28:
        return end.year

    span_end = end + _dt.timedelta(days=1)
    if start.year == end.year:
        return end.year
    boundary = _dt.date(end.year, 1, 1)
    in_end_year = (span_end - boundary).days
    return end.year if in_end_year * 2 >= span else end.year - 1
