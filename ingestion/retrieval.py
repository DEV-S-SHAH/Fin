"""Company-Isolated Retrieval - GraphRAG queries with company scoping.

Ensures that:
- Vector searches are scoped to a company
- Graph traversals respect company boundaries
- Cross-company comparisons are intentional
- Cache keys include company context
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .registry import Company, CompanyRegistry, get_registry

__all__ = [
    "CompanyScope",
    "CompanyIsolatedRetriever",
    "CrossCompanyQuery",
    "get_retriever",
]


@dataclass(frozen=True)
class CompanyScope:
    """Defines the company scope for a retrieval operation."""
    
    # Single company (most queries)
    ticker: str | None = None
    
    # Explicit list for cross-company comparison
    tickers: tuple[str, ...] = ()
    
    # All companies (for global searches)
    all_companies: bool = False
    
    def __post_init__(self) -> None:
        if self.ticker and self.tickers:
            raise ValueError("Specify either ticker or tickers, not both")
        if self.all_companies and (self.ticker or self.tickers):
            raise ValueError("all_companies cannot be combined with ticker/tickers")
    
    @property
    def is_single_company(self) -> bool:
        return self.ticker is not None
    
    @property
    def is_cross_company(self) -> bool:
        return bool(self.tickers)
    
    @property
    def is_global(self) -> bool:
        return self.all_companies
    
    def get_tickers(self, registry: CompanyRegistry) -> list[str]:
        """Resolve to list of tickers."""
        if self.all_companies:
            return registry.tickers()
        if self.ticker:
            return [self.ticker]
        return list(self.tickers)
    
    def cache_key_suffix(self) -> str:
        """Suffix for cache keys to ensure company isolation."""
        if self.all_companies:
            return "global"
        if self.ticker:
            return self.ticker.lower()
        if self.tickers:
            return "_".join(sorted(t.lower() for t in self.tickers))
        return "none"


class CompanyIsolatedRetriever:
    """Retriever that enforces company isolation at all levels."""
    
    def __init__(
        self,
        registry: CompanyRegistry | None = None,
        pgvector_conn: Any = None,
        neo4j_driver: Any = None,
        ladybug_conn: Any = None,
    ) -> None:
        self.registry = registry or get_registry()
        self.pgvector_conn = pgvector_conn
        self.neo4j_driver = neo4j_driver
        self.ladybug_conn = ladybug_conn
    
    def vector_search(
        self,
        query_embedding: list[float],
        scope: CompanyScope,
        limit: int = 10,
        score_threshold: float = 0.7,
    ) -> list[dict[str, Any]]:
        """Search vectors scoped to company/companies.
        
        In pgvector, this adds a WHERE clause filtering by company_ticker.
        """
        tickers = scope.get_tickers(self.registry)
        
        if not tickers:
            return []
        
        # Build the query with company filter
        # This is a template - actual implementation depends on pgvector schema
        where_clause = "company_ticker = ANY($1)" if len(tickers) > 1 else "company_ticker = $1"
        
        query = f"""
            SELECT chunk_id, content, metadata, company_ticker, embedding <=> $2 AS distance
            FROM document_chunks
            WHERE {where_clause}
            AND embedding <=> $2 < $3
            ORDER BY embedding <=> $2
            LIMIT $4
        """
        
        # This would execute against pgvector_conn
        # results = self.pgvector_conn.execute(query, tickers, query_embedding, 1-score_threshold, limit)
        
        # For now, return structure showing the isolation
        return [
            {
                "query": "vector_search",
                "scope": scope.cache_key_suffix(),
                "tickers": tickers,
                "where_clause": where_clause,
                "note": "Implementation depends on pgvector connection",
            }
        ]
    
    def graph_traverse(
        self,
        seed_entities: list[str],
        scope: CompanyScope,
        hops: int = 2,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Traverse graph with company-aware constraints.
        
        In Neo4j, this adds company filters to all MATCH patterns.
        """
        tickers = scope.get_tickers(self.registry)
        
        if not tickers:
            return {"nodes": [], "edges": []}
        
        # Company-aware Cypher pattern
        # All traversals start from Company nodes and stay within their subgraph
        ticker_filter = ", ".join(f"'{t}'" for t in tickers)
        
        query = f"""
            MATCH (c:Company)-[:HAS_DOCUMENT]->(d:Document)-[:HAS_CHUNK]->(ch:Chunk)
            WHERE c.ticker IN [{ticker_filter}]
            AND ch.id IN $seeds
            CALL apoc.path.expandConfig(ch, {{
                relationshipFilter: ">|<",
                minLevel: 1,
                maxLevel: {hops},
                filterStartNode: false
            }}) YIELD path
            RETURN path
            LIMIT $limit
        """
        
        return {
            "query": "graph_traverse",
            "scope": scope.cache_key_suffix(),
            "tickers": tickers,
            "cypher_template": query,
            "note": "Implementation depends on Neo4j driver",
        }
    
    def graphrag_query(
        self,
        question: str,
        scope: CompanyScope,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a GraphRAG query with company isolation."""
        tickers = scope.get_tickers(self.registry)
        
        # This would integrate with the existing GraphRAG pipeline
        # The key is that all retrieval is scoped to the company tickers
        
        return {
            "question": question,
            "scope": scope.cache_key_suffix(),
            "tickers": tickers,
            "config": config or {},
            "note": "Integrates with existing GraphRAG - adds company filter to all retrieval stages",
        }
    
    def cache_key(self, base_key: str, scope: CompanyScope) -> str:
        """Generate a cache key that includes company scope."""
        suffix = scope.cache_key_suffix()
        combined = f"{base_key}|{suffix}"
        return hashlib.sha256(combined.encode()).hexdigest()[:32]
    
    def invalidate_company_cache(self, ticker: str) -> None:
        """Invalidate all cache entries for a company."""
        # This would integrate with the existing cache system
        # Pattern: delete all keys containing the ticker
        pass


@dataclass
class CrossCompanyQuery:
    """A query that intentionally compares multiple companies."""
    
    question: str
    tickers: list[str]
    comparison_type: str = "compare"  # compare, rank, trend, correlation
    metrics: list[str] | None = None
    date_range: tuple[str, str] | None = None
    
    def to_scope(self) -> CompanyScope:
        return CompanyScope(tickers=tuple(self.tickers))
    
    def validate(self, registry: CompanyRegistry) -> list[str]:
        """Validate all tickers exist in registry."""
        errors = []
        for ticker in self.tickers:
            if ticker.upper() not in registry:
                errors.append(f"Unknown company: {ticker}")
        return errors


# Global retriever instance
_retriever: CompanyIsolatedRetriever | None = None


def get_retriever(
    registry: CompanyRegistry | None = None,
    pgvector_conn: Any = None,
    neo4j_driver: Any = None,
    ladybug_conn: Any = None,
) -> CompanyIsolatedRetriever:
    """Get the global company-isolated retriever."""
    global _retriever
    if _retriever is None:
        _retriever = CompanyIsolatedRetriever(
            registry=registry,
            pgvector_conn=pgvector_conn,
            neo4j_driver=neo4j_driver,
            ladybug_conn=ladybug_conn,
        )
    return _retriever