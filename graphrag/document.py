"""PDF text extraction and sliding-window chunking.

Chunking is measured in *tokens*, defined here as word and punctuation units
(see :func:`tokenize`). That avoids a heavyweight tokenizer dependency and is
close enough to BPE token counts for window sizing, since the window only needs
to bound how much text reaches the model.

Page provenance is preserved on every chunk so an extracted description can be
traced back to the page it came from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

# Words and single punctuation marks. Keeps counts stable across the pipeline.
_TOKEN_RE = re.compile(r"\w+(?:[-'’]\w+)*|[^\w\s]")

# Hyphenation across a line break turns into a stray hyphen inside a word.
_DEHYPHENATE = re.compile(r"(\w)-\n(\w)")
# Runs of blank lines mark paragraph boundaries more reliably than \n alone.
_MULTI_NEWLINE = re.compile(r"\n{2,}")
_SPACES = re.compile(r"[ \t ]+")


def tokenize(text: str) -> list[str]:
    """Split *text* into word/punctuation tokens."""
    return _TOKEN_RE.findall(text)


def count_tokens(text: str) -> int:
    return len(tokenize(text))


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str
    page_start: int
    page_end: int
    token_count: int

    @property
    def location(self) -> str:
        if self.page_start == self.page_end:
            return f"page {self.page_start}"
        return f"pages {self.page_start}-{self.page_end}"


@dataclass(frozen=True)
class Document:
    path: Path
    pages: list[str]

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def text(self) -> str:
        return "\n\n".join(self.pages)

    @property
    def token_count(self) -> int:
        return count_tokens(self.text)


class PDFExtractionError(RuntimeError):
    pass


def load_pdf(path: str | Path) -> Document:
    """Extract per-page text from *path* using pypdf.

    Encrypted PDFs are decrypted with the empty password when possible, which
    covers the common "no password, just restricted" case. A page that fails to
    extract is recorded as empty rather than aborting the whole document.
    """
    pdf_path = Path(path)
    if not pdf_path.exists():
        raise PDFExtractionError(f"PDF not found: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise PDFExtractionError(f"Expected a .pdf file, got: {pdf_path.name}")

    try:
        reader = PdfReader(str(pdf_path))
    except PdfReadError as exc:
        raise PDFExtractionError(f"Unreadable PDF {pdf_path.name}: {exc}") from exc

    if reader.is_encrypted:
        try:
            if reader.decrypt("") == 0:
                raise PDFExtractionError(
                    f"{pdf_path.name} is password protected; cannot extract text"
                )
        except (NotImplementedError, PdfReadError) as exc:
            raise PDFExtractionError(
                f"{pdf_path.name} is encrypted and cannot be opened: {exc}"
            ) from exc

    pages: list[str] = []
    for page in reader.pages:
        try:
            raw = page.extract_text() or ""
        except Exception:  # noqa: BLE001 - a bad page must not kill the run
            raw = ""
        pages.append(_normalise(raw))
    return Document(path=pdf_path, pages=pages)


def _normalise(text: str) -> str:
    text = _DEHYPHENATE.sub(r"\1\2", text)
    text = _MULTI_NEWLINE.sub("\n\n", text)
    text = _SPACES.sub(" ", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def chunk_document(
    document: Document, chunk_tokens: int = 800, overlap_tokens: int = 100
) -> list[Chunk]:
    """Split a document into overlapping windows of *chunk_tokens*.

    The window advances by ``chunk_tokens - overlap_tokens`` so that a sentence
    spanning a boundary appears whole in at least one chunk. Page numbers are
    attributed by mapping each token back to the page it came from.
    """
    if chunk_tokens <= 0:
        raise ValueError("chunk_tokens must be positive")
    if overlap_tokens < 0 or overlap_tokens >= chunk_tokens:
        raise ValueError("overlap_tokens must be in [0, chunk_tokens)")
    stride = chunk_tokens - overlap_tokens

    # Flatten pages into a token stream that remembers its page of origin.
    tokens: list[str] = []
    page_of: list[int] = []
    for page_no, page_text in enumerate(document.pages, start=1):
        page_tokens = tokenize(page_text)
        if not page_tokens:
            continue
        tokens.extend(page_tokens)
        page_of.extend([page_no] * len(page_tokens))

    if not tokens:
        return []

    def render(start: int, end: int) -> str:
        # Detokenise for readability: no space before closing punctuation.
        out: list[str] = []
        for i in range(start, end):
            tok = tokens[i]
            if not out:
                out.append(tok)
                continue
            prev = tokens[i - 1]
            if tok in ".,;:!?)]}" and prev not in "([":
                out.append(tok)
            elif prev in "([":
                out.append(tok)
            elif out and out[-1] in "([{":
                out.append(tok)
            else:
                out.append(" " + tok)
        return "".join(out).strip()

    chunks: list[Chunk] = []
    start = 0
    index = 0
    total = len(tokens)
    while start < total:
        end = min(start + chunk_tokens, total)
        text = render(start, end)
        if text:
            pages = page_of[start:end]
            chunks.append(
                Chunk(
                    index=index,
                    text=text,
                    page_start=min(pages),
                    page_end=max(pages),
                    token_count=end - start,
                )
            )
            index += 1
        if end == total:
            break
        start += stride
    return chunks


def load_and_chunk(
    path: str | Path, chunk_tokens: int = 800, overlap_tokens: int = 100
) -> tuple[Document, list[Chunk]]:
    document = load_pdf(path)
    return document, chunk_document(document, chunk_tokens, overlap_tokens)
