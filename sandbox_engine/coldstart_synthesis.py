"""Financial synthesis and streaming prompt generation for Cold-Start JIT Graph RAG.

Injects multi-hop hybrid graph paths, evidence quotes, and primary SEC text
into a structured 5-section investment analysis and streams tokens via generators.
"""

from __future__ import annotations

from typing import Any, Generator, Optional
from .traversal import format_provenance_ledger
from .provenance import (
    EvidenceRef,
    SECGradingAdapter,
    serialise_evidence_refs,
    grade_answer_with_refs,
    GradedAnswer,
)

SYNTHESIS_SYSTEM_PROMPT = """You are a senior financial analyst and investigative research director.
You synthesize investment insights by traversing financial knowledge graphs and analyzing primary SEC filings.

Your analysis must strictly follow this exact 5-section structure:
1. Executive Summary & Thesis
2. Direct Dependencies (1-hop)
3. Second-Order Contagion (Supply Chain / Competitors / Key Talent)
4. Capital Allocation & Margin Outlook
5. Verifiable Evidence Chain (Listing exact graph paths)

Rules:
- Be rigorous, dense, and factual.
- Ground all claims in the provided graph paths and SEC filing excerpts.
- In Section 5, quote the exact graph traversal chains from the Provenance Ledger.
- You may ONLY cite tags that appear in the evidence block. Do not invent a tag.
- Cite every factual claim using bracketed tags like [E1] or [E2].
- If you compute a value from cited facts, show the arithmetic so the derivation is visible.
- Never state a number that is not in the evidence and not derived from it.
- Do not use outside knowledge. If a fact is not in the evidence, treat it as unknown rather than supplying it.
"""


class ColdStartSynthesizer:
    """Generates structured synthesis prompts and streams investment reports."""

    def __init__(
        self,
        system_prompt: str = SYNTHESIS_SYSTEM_PROMPT,
        grader: Optional[SECGradingAdapter] = None,
    ) -> None:
        self.system_prompt = system_prompt
        self._grader = grader or SECGradingAdapter()

    def generate_prompts(
        self,
        target_ticker: str,
        query: str,
        paths: list[list[dict[str, Any]]],
        evidence_refs: list[EvidenceRef] | None = None,
        filing_text: str = "",
    ) -> tuple[str, str]:
        """Generate system and user prompts injecting hybrid paths and SEC text."""
        ledger = format_provenance_ledger(paths)
        evidence_block = ""
        if evidence_refs:
            evidence_block = f"\n=== EVIDENCE BLOCK ===\n{serialise_evidence_refs(evidence_refs)}\n"

        user_content = (
            f"TARGET ENTITY: {target_ticker}\n"
            f"INVESTOR QUERY: {query}\n\n"
            f"=== MULTI-HOP GRAPH PROVENANCE LEDGER ===\n"
            f"{ledger}\n"
            f"{evidence_block}"
            f"=== PRIMARY SEC NARRATIVE EXCERPTS ===\n"
            f"{filing_text[:12000]}\n\n"
            f"Synthesize your investment report addressing the investor query using the required 5-section structure:\n"
            f"1. Executive Summary & Thesis\n"
            f"2. Direct Dependencies (1-hop)\n"
            f"3. Second-Order Contagion (Supply Chain / Competitors / Key Talent)\n"
            f"4. Capital Allocation & Margin Outlook\n"
            f"5. Verifiable Evidence Chain (Listing exact graph paths)"
        )
        return self.system_prompt, user_content

    def stream_synthesis(
        self,
        context_dict: dict[str, Any],
        client: Any = None,
        model: Optional[str] = None,
    ) -> Generator[str, None, None]:
        """Stream incremental answer tokens via generator."""
        target_ticker = context_dict.get("target_ticker", "")
        query = context_dict.get("query", "")
        paths = context_dict.get("paths", [])
        filing_text = context_dict.get("filing_text", "")
        evidence_refs = context_dict.get("evidence_refs", [])

        system_msg, user_msg = self.generate_prompts(
            target_ticker=target_ticker,
            query=query,
            paths=paths,
            evidence_refs=evidence_refs,
            filing_text=filing_text,
        )

        if client is not None and hasattr(client, "chat"):
            model_name = model or "gpt-4o-mini"
            stream_resp = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
                stream=True,
                temperature=0.2,
            )
            for chunk in stream_resp:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
            return

        if callable(client):
            res = client(system_msg, user_msg)
            if hasattr(res, "__iter__") and not isinstance(res, (str, bytes, dict)):
                for token in res:
                    yield str(token)
            else:
                yield str(res)
            return

        # Default structured fallback token stream
        ledger = format_provenance_ledger(paths)
        evidence_preview = ""
        if evidence_refs:
            evidence_preview = f"\n=== EVIDENCE (first 3) ===\n" + "\n".join(e.block() for e in evidence_refs[:3])
        mock_response = (
            f"### 1. Executive Summary & Thesis\n"
            f"{target_ticker} demonstrates critical operational dependencies uncovered through cold-start graph analysis.\n\n"
            f"### 2. Direct Dependencies (1-hop)\n"
            f"Primary direct dependencies extracted from Tier 1 filing:\n{ledger[:200]}\n\n"
            f"### 3. Second-Order Contagion (Supply Chain / Competitors / Key Talent)\n"
            f"Cross-boundary contagion revealed macro and multi-tier supply chain exposures.\n\n"
            f"### 4. Capital Allocation & Margin Outlook\n"
            f"Capital expenditure intensity reflects ongoing commitments to resilient sourcing.\n\n"
            f"### 5. Verifiable Evidence Chain\n"
            f"{ledger}"
            f"{evidence_preview}\n"
        )
        words = mock_response.split(" ")
        for i, word in enumerate(words):
            yield word + (" " if i < len(words) - 1 else "")

    def grade_synthesis(
        self,
        answer: str,
        evidence_refs: list[EvidenceRef],
        question: str = "",
    ) -> GradedAnswer:
        """Grade a synthesized answer against evidence refs using SEC provenance rules.

        This enforces the provenance contract: provenance assigned by retrieval,
        never by model. Returns a GradedAnswer with per-sentence verdicts.
        """
        return self._grader.grade(answer, evidence_refs, question)
