"""High-signal section slicing and text normalization for Tier 1 Cold Start.

Extracts core narrative sections (Item 1 Business for 10-K, Item 2 MD&A for 10-Q),
strips markup, boilerplate, and tables, and enforces an input token budget.
"""

from __future__ import annotations

import html
import re

from .ufgs_extract import extract_sections, item_slice


class SectionNotFoundError(Exception):
    """Raised when the target section cannot be located or extracted."""
    pass


_SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_SVG_TABLE_RE = re.compile(r"<(svg|table)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_BLOCK_TAG_RE = re.compile(
    r"</?(?:p|div|h[1-6]|li|blockquote|section|article|br|tr)[^>]*>",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")
_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?](?:\s+|$)")


def _truncate_at_sentence(text: str, max_chars: int) -> str:
    """Enforce character cutoff while preserving complete sentences at boundary."""
    if len(text) <= max_chars:
        return text

    candidate = text[:max_chars]
    matches = list(_SENTENCE_BOUNDARY_RE.finditer(candidate))
    if matches:
        cut_pos = matches[-1].start() + 1
        if cut_pos >= max_chars // 2:
            return candidate[:cut_pos].strip()

    last_space = candidate.rfind(" ")
    if last_space >= max_chars // 2:
        return candidate[:last_space].strip()

    return candidate.strip()


def clean_and_truncate_section(
    html_content: str, form_type: str = "10-K", max_tokens: int = 6000
) -> str:
    """Extract Item 1 (10-K) or Item 2 (10-Q), clean HTML, and truncate to max_tokens.

    Parameters:
    - html_content: Raw filing HTML string.
    - form_type: "10-K" or "10-Q".
    - max_tokens: Maximum tokens budget (1 token ~ 4 characters).

    Returns:
    Cleaned, dense narrative text capped at max_tokens * 4 characters.

    Raises:
    - SectionNotFoundError: If the section cannot be found or cleaned text is empty.
    """
    if not html_content or not isinstance(html_content, str):
        raise SectionNotFoundError("Empty HTML content provided")

    form = form_type.strip().upper()
    item_code = "1" if form == "10-K" else ("2" if form == "10-Q" else "1")

    # 1. Use ufgs_extract to extract sections and slice item
    sections = extract_sections(html_content, form)
    raw_slice = item_slice(html_content, sections, item_code)

    if not raw_slice or not raw_slice.strip():
        raise SectionNotFoundError(
            f"Section Item {item_code} not found in {form_type} filing"
        )

    # 2. Strip scripts and styles
    text = _SCRIPT_STYLE_RE.sub(" ", raw_slice)

    # 3. Strip tables and SVGs
    text = _SVG_TABLE_RE.sub(" ", text)

    # 4. Convert block elements to newlines
    text = _BLOCK_TAG_RE.sub("\n", text)

    # 5. Strip all remaining HTML/XBRL tags
    text = _TAG_RE.sub(" ", text)

    # 6. Unescape entities
    text = html.unescape(text)

    # 7. Compress redundant whitespace
    lines = [_WHITESPACE_RE.sub(" ", line).strip() for line in text.splitlines()]
    dense_text = "\n".join(line for line in lines if line)
    dense_text = re.sub(r"\n{3,}", "\n\n", dense_text).strip()

    if not dense_text:
        raise SectionNotFoundError(
            f"Section Item {item_code} resulted in empty text after cleaning"
        )

    # 8. Apply character cutoff: max_tokens * 4
    max_chars = max_tokens * 4
    return _truncate_at_sentence(dense_text, max_chars)
