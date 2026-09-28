"""Universal document parser: any supported file into clean Markdown chunks.

The module has three layers, each usable on its own:

1. **Loaders** (:func:`load_document`) turn a file into a single Markdown
   string. Every format is normalised to Markdown so the chunker only ever sees
   one shape of text, and so a chunk keeps whatever structure the source had
   (headings, tables, code fences, LaTeX).
2. **A chunker** (:func:`chunk_text`) splits that Markdown into overlapping
   windows using a *recursive* strategy: try to break on the most semantic
   boundary available, and only fall back to coarser ones for pieces that are
   still too large.
3. **A convenience layer** (:func:`load_and_chunk`, :func:`load_directory`)
   that runs the first two and always returns the four-key chunk dictionaries
   the pipeline consumes.

Design notes worth knowing before changing anything here:

- **The two PDF libraries are used for different jobs.** pdfplumber recovers
  ruling-line tables but, on some PDFs, drops every word space
  (``AttentionIsAllYouNeed``). pypdf keeps the spaces and never sees tables. So
  tables always come from pdfplumber, while each page's prose is taken from
  whichever extractor reads it properly -- judged by spaces-per-alphanumeric
  character, which is a cheap proxy for "word boundaries survived". Grids that
  are clearly layout artefacts rather than data are dropped.
- **Structure is protected, not normalised away.** LaTeX spans, fenced code and
  Markdown tables are lifted out before splitting and stitched back afterwards,
  so a window boundary can never cut a formula or a table in half. The cost is
  that one oversized block (a 5000-character code fence) yields one oversized
  chunk rather than a corrupted one.
- **CSV is never emitted raw.** Row-per-line comma soup is unreadable to a
  retrieval pipeline because the column names -- which carry most of the
  meaning -- are only present once, in the header. Each row becomes a
  declarative sentence naming the file and every column, so any single chunk
  is self-describing.
- **Tokens are approximated**, not BPE. A "token" here is a word or
  punctuation mark, which tracks real tokenizers closely enough to size a
  window. This keeps the module dependency-light and matches the convention
  already used by :mod:`graphrag.document`.
- **Nothing raises out of the batch helpers.** A directory walk reports a
  per-file failure and carries on, because one corrupt PDF should not cost you
  the other 400 documents. Single-file helpers do raise, so a caller that asked
  for one specific file finds out that it failed.

Optional dependencies: ``pdfplumber`` and ``beautifulsoup4`` (with ``lxml`` for
the faster parser). ``pypdf`` is required for PDF. Every one of them is imported
lazily, so a text-only caller needs none of them.
"""


from __future__ import annotations

import csv
import io
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

__all__ = [
    "SUPPORTED_EXTENSIONS",
    "ParsedDocument",
    "ChunkingError",
    "DocumentLoadError",
    "load_document",
    "chunk_text",
    "load_and_chunk",
    "load_directory",
    "count_tokens",
    "tokenize",
]

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

SUPPORTED_EXTENSIONS: dict[str, str] = {
    ".pdf": "pdf",
    ".csv": "csv",
    ".tsv": "csv",
    ".html": "html",
    ".htm": "html",
    ".xhtml": "html",
    ".txt": "text",
    ".text": "text",
    ".md": "markdown",
    ".markdown": "markdown",
}

DEFAULT_MAX_TOKENS = 800
DEFAULT_MAX_CHARS = 3000
DEFAULT_OVERLAP_TOKENS = 100
DEFAULT_OVERLAP_CHARS = 400

#: Pieces are rejoined with a blank line. That separator is real content the
#: character budget has to pay for, so it is accounted for when packing chunks.
_PIECE_SEPARATOR = "\n\n"

#: Splits tried in order, most semantic first. The recursive splitter walks this
#: list downward and only reaches coarse separators when a piece is still over
#: budget, which keeps word-level fragmentation off the common path.
_SEPARATORS: tuple[str, ...] = (
    "\n\n",  # paragraph
    "\n",  # line / list item
    ". ",  # sentence
    "; ",
    ", ",
    " ",  # word
    "",  # hard character split, last resort
)

#: Sentence end, including the abbreviations where "." is not a break.
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])[\"')\]]*\s+")

# Words and single punctuation marks; mirrors graphrag.document.tokenize.
_TOKEN_RE = re.compile(r"\w+(?:[-'’]\w+)*|[^\w\s]")

# Trailing spaces and tabs, and runs of 3+ blank lines.
_SPACES = re.compile(r"[ \t ]+")
_EXCESS_BLANK_LINES = re.compile(r"\n{3,}")

# A placeholder used to park protected blocks. The NUL sentinel makes an
# accidental collision with real content effectively impossible, and the index
# keeps distinct blocks distinct even if a block's text repeats.
_PLACEHOLDER = "\x00PROTECTED{index}\x00"
_PLACEHOLDER_RE = re.compile(r"\x00PROTECTED(\d+)\x00")


class DocumentLoadError(RuntimeError):
    """A file could not be read or produced no usable text."""


class ChunkingError(ValueError):
    """Chunking was asked for an impossible configuration."""


# --------------------------------------------------------------------------
# Tokenisation
# --------------------------------------------------------------------------


def tokenize(text: str) -> list[str]:
    """Split *text* into word/punctuation tokens."""
    return _TOKEN_RE.findall(text or "")


def count_tokens(text: str) -> int:
    """Approximate token count of *text*."""
    return len(tokenize(text))


# --------------------------------------------------------------------------
# Parsed document
# --------------------------------------------------------------------------


@dataclass
class ParsedDocument:
    """A source file normalised to Markdown, plus what the loader learned."""

    source_file: str
    file_type: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def char_count(self) -> int:
        return len(self.content)

    @property
    def token_count(self) -> int:
        return count_tokens(self.content)

    @property
    def is_empty(self) -> bool:
        return not self.content.strip()


# --------------------------------------------------------------------------
# Protected regions
# --------------------------------------------------------------------------

# Order matters: fenced code is matched before inline math so a `$` inside a
# code fence is never mistaken for a formula.
_PROTECTED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("code", re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)),
    ("code", re.compile(r"^~~~.*?^~~~", re.MULTILINE | re.DOTALL)),
    ("display_math", re.compile(r"\$\$.+?\$\$", re.DOTALL)),
    ("display_math", re.compile(r"\\\[.+?\\\]", re.DOTALL)),
    ("display_math", re.compile(r"\\begin\{(?P<env>equation|align|gather|multline)\*?\}.*?\\end\{(?P=env)\*?\}", re.DOTALL)),
    ("inline_math", re.compile(r"(?<!\$)\$(?!\$).+?(?<!\$)\$(?!\$)", re.DOTALL)),
    ("table", re.compile(r"^(?:\|.*\|[ \t]*\n){2,}", re.MULTILINE)),
)


def _protect(text: str) -> tuple[str, list[str]]:
    """Replace structurally sensitive blocks with placeholders.

    Returns the masked text and the blocks in placeholder order. Splitting then
    works on ordinary prose, and :func:`_restore` puts the blocks back
    untouched, so no window boundary can land inside a formula or a table.
    """
    blocks: list[str] = []

    for _, pattern in _PROTECTED_PATTERNS:
        # A table's continuation rows are part of the block, so re-scan the
        # already-masked text: placeholders sit on their own lines and are
        # never matched by these patterns.
        def _swap(match: re.Match[str]) -> str:
            blocks.append(match.group(0))
            return _PLACEHOLDER.format(index=len(blocks) - 1)

        text = pattern.sub(_swap, text)

    return text, blocks


def _restore(text: str, blocks: Sequence[str]) -> str:
    """Put protected blocks back where their placeholders ended up."""

    def _swap(match: re.Match[str]) -> str:
        index = int(match.group(1))
        return blocks[index] if index < len(blocks) else ""

    return _PLACEHOLDER_RE.sub(_swap, text)


# --------------------------------------------------------------------------
# Loaders
# --------------------------------------------------------------------------


def load_document(
    path: str | Path,
    *,
    file_type: str | None = None,
) -> ParsedDocument:
    """Parse *path* into a :class:`ParsedDocument` of clean Markdown.

    *file_type* overrides extension detection, which is occasionally needed for
    files whose extension lies (a ``.txt`` that is really HTML, say).
    """
    resolved = Path(path).expanduser()

    if not resolved.exists():
        raise DocumentLoadError(f"no such file: {resolved}")
    if resolved.is_dir():
        raise DocumentLoadError(f"expected a file but got a directory: {resolved}")

    suffix = resolved.suffix.lower()
    kind = file_type or SUPPORTED_EXTENSIONS.get(suffix)
    if kind is None:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise DocumentLoadError(
            f"unsupported file type {suffix or '(none)'} for {resolved.name}; "
            f"supported: {supported}"
        )

    loaders = {
        "pdf": _load_pdf,
        "csv": _load_csv,
        "html": _load_html,
        "text": _load_text,
        "markdown": _load_markdown,
    }
    if kind not in loaders:
        supported = ", ".join(sorted(loaders))
        raise DocumentLoadError(
            f"unknown file type {kind!r} for {resolved.name}; supported: {supported}"
        )
    loader = loaders[kind]

    try:
        content, metadata = loader(resolved, kind)
    except DocumentLoadError:
        raise
    except Exception as exc:  # noqa: BLE001 - normalise loader internals
        raise DocumentLoadError(f"failed to parse {resolved.name}: {exc}") from exc

    metadata.setdefault("extension", suffix)
    metadata.setdefault("size_bytes", resolved.stat().st_size)

    document = ParsedDocument(
        source_file=str(resolved),
        file_type=kind,
        content=content,
        metadata=metadata,
    )
    if document.is_empty:
        raise DocumentLoadError(
            f"no text extracted from {resolved.name} "
            f"(scanned image, empty file, or markup with no body content?)"
        )
    return document


# -- PDF --------------------------------------------------------------------


#: Below this many spaces per alphanumeric character, extracted text has lost
#: its word boundaries and is useless for retrieval. pdfplumber in particular
#: collapses spaces on PDFs that encode space glyphs as unmapped characters,
#: turning "The dominant sequence" into "Thedominantsequence".
_MIN_SPACE_DENSITY = 0.08


def _space_density(text: str) -> float:
    """Spaces per alphanumeric character; a proxy for readable word boundaries."""
    alnum = sum(1 for char in text if char.isalnum())
    if not alnum:
        return 0.0
    return text.count(" ") / alnum


def _readable(text: str) -> bool:
    """True when *text* looks like prose rather than glued-together glyphs."""
    stripped = text.strip()
    if not stripped:
        return False
    return _space_density(stripped) >= _MIN_SPACE_DENSITY


def _load_pdf(path: Path, kind: str) -> tuple[str, dict[str, Any]]:
    """Extract text and tables from a PDF.

    The two libraries are good at different things, so this uses each for what
    it does well. pdfplumber recovers ruling-line tables that pypdf flattens
    into unreadable text, but on many PDFs it drops word spaces. pypdf keeps
    the spaces and never sees tables. So tables always come from pdfplumber,
    and each page's prose is taken from whichever extractor reads it properly.
    """
    try:
        import pdfplumber
    except ImportError:
        pdfplumber = None  # type: ignore[assignment]

    metadata: dict[str, Any] = {
        "pages": 0,
        "tables": 0,
        "text_source": None,
        "table_source": "pdfplumber" if pdfplumber is not None else None,
    }

    plumber_pages: list[dict[str, Any]] = []
    plumber_failed = False
    if pdfplumber is not None:
        try:
            with pdfplumber.open(str(path)) as pdf:
                metadata["pages"] = len(pdf.pages)
                for page in pdf.pages:
                    tables: list[str] = []
                    try:
                        for table in page.extract_tables() or []:
                            rendered = _table_to_markdown(table)
                            if rendered:
                                tables.append(rendered)
                    except Exception:  # noqa: BLE001 - a bad table must not lose the page
                        pass
                    plumber_pages.append(
                        {"text": page.extract_text() or "", "tables": tables}
                    )
        except Exception as exc:  # noqa: BLE001 - fall through to pypdf
            metadata["pdfplumber_error"] = str(exc)[:200]
            plumber_failed = True
            plumber_pages = []

    # pypdf supplies the prose for any page pdfplumber cannot read properly.
    pypdf_pages: list[str] = []
    try:
        from pypdf import PdfReader
    except ImportError:
        if not plumber_pages:
            raise DocumentLoadError(
                f"reading {path.name} needs pypdf or pdfplumber "
                "(pip install pypdf pdfplumber)"
            ) from None
        PdfReader = None  # type: ignore[assignment]
    else:
        try:
            reader = PdfReader(str(path))
        except Exception as exc:  # noqa: BLE001
            if not plumber_pages:
                raise DocumentLoadError(
                    f"cannot read PDF {path.name}: {exc} "
                    "(encrypted or corrupt files need decrypting first)"
                ) from exc
            reader = None
        if reader is not None:
            if getattr(reader, "is_encrypted", False):
                try:
                    reader.decrypt("")
                except Exception as exc:  # noqa: BLE001
                    raise DocumentLoadError(
                        f"{path.name} is encrypted and cannot be opened without a password"
                    ) from exc
            metadata["pages"] = max(metadata["pages"], len(reader.pages))
            for page in reader.pages:
                try:
                    pypdf_pages.append(page.extract_text() or "")
                except Exception:  # noqa: BLE001 - skip an unreadable page
                    pypdf_pages.append("")

    if not plumber_pages and not pypdf_pages:
        raise DocumentLoadError(f"no text could be extracted from {path.name}")

    parts: list[str] = []
    sources: set[str] = set()
    for index in range(max(len(plumber_pages), len(pypdf_pages))):
        page = plumber_pages[index] if index < len(plumber_pages) else None
        tables = page["tables"] if page else []
        plumber_text = page["text"] if page else ""
        pypdf_text = pypdf_pages[index] if index < len(pypdf_pages) else ""

        if _readable(plumber_text):
            text, source = plumber_text, "pdfplumber"
        elif _readable(pypdf_text):
            text, source = pypdf_text, "pypdf"
        elif pypdf_text.strip() or plumber_text.strip():
            # Neither reads well. Prefer whichever has more word boundaries,
            # because it at least breaks apart, even if it is still degraded.
            if pypdf_text.strip() and not plumber_text.strip():
                text, source = pypdf_text, "pypdf"
            elif plumber_text.strip() and not pypdf_text.strip():
                text, source = plumber_text, "pdfplumber"
            elif _space_density(pypdf_text) >= _space_density(plumber_text):
                text, source = pypdf_text, "pypdf"
            else:
                text, source = plumber_text, "pdfplumber"
        else:
            text, source = "", None

        metadata["tables"] += len(tables)
        blocks = ([text.strip()] if text.strip() else []) + tables
        if blocks:
            if source:
                sources.add(source)
            parts.append(f"\n\n{_blocks_to_markdown(blocks, index + 1)}")

    if sources:
        metadata["text_source"] = (
            next(iter(sources)) if len(sources) == 1 else "mixed:" + ",".join(sorted(sources))
        )
    if plumber_failed:
        metadata["warning"] = "pdfplumber failed; tables not extracted"
    elif pdfplumber is None:
        metadata["warning"] = "pdfplumber unavailable; tables not extracted"

    return _normalise_markdown("".join(parts)), metadata


def _blocks_to_markdown(blocks: list[str], page_number: int) -> str:
    """Render one page's blocks, tagging the page for provenance."""
    body = "\n\n".join(blocks)
    return f"<!-- page {page_number} -->\n\n{body}"


#: A grid that is mostly empty cells, or one sliced into dozens of one-character
#: columns, is a figure frame or a layout artefact rather than data. Emitting it
#: as a Markdown table buries the page prose in empty columns, so it is dropped.
#: The page's own text still carries the real content either way.
_MIN_TABLE_FILL = 0.4
_MAX_TABLE_COLUMNS = 20
_MIN_MEDIAN_CELL = 3


def _table_to_markdown(table: list[list[str | None]]) -> str:
    """Render an extracted table grid as Markdown, or '' if it is not real data.

    Papers draw rules around figures, equations and sidebars, and pdfplumber
    reports those boxes as tables. Their signature is many narrow columns whose
    cells hold one or two characters, which is what these checks reject.
    """
    if not table:
        return ""

    rows = [row for row in table if row is not None]
    rows = [row for row in rows if any((cell or "").strip() for cell in row)]
    if len(rows) < 2:
        return ""

    width = max(len(row) for row in rows)
    if width < 2 or width > _MAX_TABLE_COLUMNS:
        return ""

    cells = [(cell or "").strip() for row in rows for cell in row]
    filled = [cell for cell in cells if cell]
    if not filled:
        return ""
    if len(filled) / len(cells) < _MIN_TABLE_FILL:
        return ""
    if statistics.median(len(cell) for cell in filled) < _MIN_MEDIAN_CELL:
        return ""

    return _render_table(rows, width)


def _render_table(rows: list[list[str | None]], width: int) -> str:
    """Convert a validated pdfplumber table grid to a Markdown table."""
    cleaned = [
        [_clean_cell(cell) for cell in row]
        for row in rows
    ]
    cleaned = [row + [""] * (width - len(row)) for row in cleaned]

    header, body = cleaned[0], cleaned[1:]
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def _clean_cell(cell: str | None) -> str:
    """Make a table cell safe for a Markdown row."""
    if cell is None:
        return ""
    # Pipes would split the row; newlines would end it.
    return re.sub(r"\s+", " ", str(cell).replace("|", r"\|")).strip()


# -- CSV --------------------------------------------------------------------


def _load_csv(path: Path, kind: str) -> tuple[str, dict[str, Any]]:
    """Convert tabular rows into explicit declarative sentences.

    Emitting the rows verbatim would put the column names in the document once,
    in a header line, and nowhere near the values they describe. Every row is
    therefore self-describing::

        Record in [sales.csv]: Region is North, Revenue is 1200, Quarter is Q1.

    That keeps any single chunk interpretable on its own, which matters once
    chunks are embedded and retrieved in isolation.
    """
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    raw = _read_text(path)

    try:
        dialect = csv.Sniffer().sniff(raw[:8192], delimiters=",;\t|")
        delimiter = dialect.delimiter
    except Exception:  # noqa: BLE001 - the default delimiter is a fine guess
        pass

    reader = csv.reader(io.StringIO(raw), delimiter=delimiter)
    try:
        rows = list(reader)
    except csv.Error as exc:
        raise DocumentLoadError(f"malformed CSV in {path.name}: {exc}") from exc

    rows = [row for row in rows if any(_clean_cell(cell) for cell in row)]
    if not rows:
        return "", {"rows": 0, "columns": 0, "delimiter": delimiter}

    header = [_clean_cell(cell) for cell in rows[0]]
    # A header of pure numbers is almost certainly data with no header row.
    has_header = bool(header) and not all(_looks_numeric(cell) for cell in header)
    if not has_header:
        header = [f"column_{i + 1}" for i in range(len(rows[0]))]
        body = rows
    else:
        body = rows[1:]

    width = len(header)
    name = path.name
    lines: list[str] = []
    for row in body:
        pairs: list[str] = []
        for index in range(width):
            key = header[index] if index < len(header) and header[index] else f"column_{index + 1}"
            value = _clean_cell(row[index]) if index < len(row) else ""
            pairs.append(f"{key} is {value}" if value else f"{key} is not specified")
        lines.append(f"Record in [{name}]: " + ", ".join(pairs) + ".")

    metadata = {
        "rows": len(body),
        "columns": width,
        "delimiter": delimiter,
        "has_header": has_header,
    }
    if not has_header:
        metadata["warning"] = "no header row detected; columns were named positionally"
    return "\n\n".join(lines), metadata


def _looks_numeric(value: str) -> bool:
    try:
        float(value.replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return False
    return True


# -- HTML -------------------------------------------------------------------


#: Tags whose contents are chrome, not document text.
_HTML_DROP_TAGS = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "canvas",
    "iframe",
    "form",
    "button",
    "input",
    "select",
    "textarea",
    "nav",
    "header",
    "footer",
    "aside",
    "menu",
    "dialog",
)

#: Container tags that mark the real article body.
_HTML_CONTENT_TAGS = ("article", "main")


def _load_html(path: Path, kind: str) -> tuple[str, dict[str, Any]]:
    """Convert HTML to Markdown, keeping the outline and tables.

    trafilatura is tried first because it isolates the article body from site
    chrome more reliably than tag heuristics. BeautifulSoup is the fallback and
    the primary path for local files, where we already know the content is a
    single document rather than a scraped page.
    """
    raw = _read_text(path)

    content = _trafilatura_markdown(raw)
    extractor = "trafilatura"
    if not content or len(content) < 200:
        soup_content = _bs4_markdown(raw)
        # Prefer whichever recovered more, unless trafilatura was decisive.
        if soup_content and (not content or len(soup_content) > len(content)):
            content, extractor = soup_content, "beautifulsoup4"

    return _normalise_markdown(content or ""), {"extractor": extractor}


def _trafilatura_markdown(raw: str) -> str:
    try:
        import trafilatura
    except ImportError:
        return ""
    try:
        return trafilatura.extract(
            raw,
            output_format="markdown",
            include_links=False,
            include_tables=True,
            include_comments=False,
            favor_precision=True,
        ) or ""
    except Exception:  # noqa: BLE001 - fall back to the parser we control
        return ""


def _bs4_markdown(raw: str) -> str:
    """Parse HTML with BeautifulSoup and emit structured Markdown."""
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise DocumentLoadError(
            "HTML parsing needs beautifulsoup4 (pip install beautifulsoup4) "
            "or trafilatura"
        ) from exc

    try:
        soup = BeautifulSoup(raw, "lxml")
    except Exception:  # noqa: BLE001 - lxml may be absent
        soup = BeautifulSoup(raw, "html.parser")

    for tag in soup(list(_HTML_DROP_TAGS)):
        tag.decompose()

    root = None
    for selector in _HTML_CONTENT_TAGS:
        found = soup.find(selector)
        if found and len(found.get_text(strip=True)) > 200:
            root = found
            break
    node = root or soup.body or soup

    out: list[str] = []
    _render_html_node(node, out, heading_level=0)
    return "\n\n".join(part for part in out if part.strip())


#: Elements that flow inside a sentence. Their text belongs to the surrounding
#: block, so it must be joined into that block rather than emitted separately --
#: otherwise ``<b>revenue</b>`` splits one sentence into three chunks.
_INLINE_TAGS = frozenset(
    """
    a abbr b bdi bdo big cite code del dfn em font i img ins kbd mark nobr q rp rt
    ruby s samp small span strike strong sub sup time tt u var wbr
    """.split()
)


def _inline_text(node: Any) -> str:
    """Flatten an element's inline content into one whitespace-normalised string.

    Block-level descendants (a ``<div>`` inside a ``<p>``, say) are separated by a
    space rather than a paragraph break, because at this level we are inside a
    single block of text.
    """
    from bs4 import NavigableString, Tag

    parts: list[str] = []
    for child in getattr(node, "children", []):
        if isinstance(child, NavigableString):
            parts.append(str(child))
            continue
        if not isinstance(child, Tag):
            continue
        name = child.name.lower()
        if name in _HTML_DROP_TAGS:
            continue
        if name == "br":
            parts.append(" ")
            continue
        if name == "img":
            alt = str(child.get("alt", "")).strip()
            if alt:
                parts.append(alt)
            continue
        parts.append(_inline_text(child))
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _is_inline_only(node: Any) -> bool:
    """True when *node* contains no block-level descendants."""
    from bs4 import Tag

    for descendant in node.descendants:
        if isinstance(descendant, Tag):
            name = descendant.name.lower()
            if name in _INLINE_TAGS or name in _HTML_DROP_TAGS:
                continue
            if name in ("table", "ul", "ol", "pre"):
                return False
            return False
    return True


def _render_html_node(node: Any, out: list[str], heading_level: int) -> None:
    """Walk the DOM in document order, emitting Markdown as we go."""
    from bs4 import NavigableString, Tag

    for child in getattr(node, "children", []):
        if isinstance(child, NavigableString):
            text = re.sub(r"\s+", " ", str(child)).strip()
            if text:
                out.append(text)
            continue
        if not isinstance(child, Tag):
            continue

        name = child.name.lower()
        if name in _HTML_DROP_TAGS:
            continue

        if re.fullmatch(r"h[1-6]", name):
            text = _inline_text(child)
            if text:
                # Never exceed h6, however deep the source nests headings.
                level = min(int(name[1]) + heading_level, 6)
                out.append(f"{'#' * level} {text}")
            continue

        if name == "table":
            rendered = _html_table_to_markdown(child)
            if rendered:
                out.append(rendered)
            continue

        if name in ("ul", "ol"):
            items = child.find_all("li", recursive=False)
            for index, item in enumerate(items, start=1):
                text = _inline_text(item)
                if not text:
                    continue
                marker = f"{index}." if name == "ol" else "-"
                out.append(f"{marker} {text}")
            continue

        if name == "pre":
            text = child.get_text()
            if text.strip():
                language = _code_language(child)
                out.append(f"```{language}\n{text.rstrip()}\n```")
            continue

        if name in ("p", "div", "section", "blockquote", "dl", "figure", "figcaption", "dd", "dt"):
            if _is_inline_only(child):
                # One block of prose: join it, and do not let a hard wrap in the
                # source become a hard break in the Markdown.
                text = _inline_text(child)
                if text:
                    out.append(f"> {text}" if name == "blockquote" else text)
            else:
                _render_html_node(child, out, heading_level)
            continue

        if name == "br":
            out.append("\n")
            continue

        if name == "a":
            # Keep the anchor text; the href is dropped because a bare URL in
            # the middle of prose is noise for retrieval.
            _render_html_node(child, out, heading_level)
            continue

        if name == "img":
            alt = _inline_text(child)
            if alt:
                out.append(f"![{alt}]")
            continue

        _render_html_node(child, out, heading_level)


def _code_language(pre: Any) -> str:
    """Best-effort language hint from a <pre><code class="language-x">."""
    code = pre.find("code")
    if not code:
        return ""
    classes = code.get("class") or []
    for value in classes:
        if str(value).startswith("language-"):
            return str(value)[len("language-") :]
    return ""


def _html_table_to_markdown(table: Any) -> str:
    """Render an HTML <table>, using the first row as the header."""
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        rows.append([_clean_cell(cell.get_text(" ", strip=True)) for cell in cells])
    if not rows:
        return ""

    # A table with no <th> still needs a header row for Markdown to render.
    header = rows[0]
    body = rows[1:]
    width = max(len(row) for row in rows)
    header = header + [""] * (width - len(header))
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    lines.extend(
        "| " + " | ".join(row + [""] * (width - len(row))) + " |" for row in body
    )
    return "\n".join(lines)


# -- TXT / Markdown ---------------------------------------------------------


def _load_text(path: Path, kind: str) -> tuple[str, dict[str, Any]]:
    """Plain text, whitespace-cleaned but with LaTeX left intact."""
    raw = _read_text(path)
    return _normalise_prose(raw), {"format": kind}


def _load_markdown(path: Path, kind: str) -> tuple[str, dict[str, Any]]:
    """Markdown is already the target format; only tidy the whitespace."""
    raw = _read_text(path)
    return _normalise_markdown(raw), {"format": kind}


def _read_text(path: Path) -> str:
    """Read a text file, tolerating whatever encoding it turns out to be."""
    data = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(data).best()
        if best is not None:
            return str(best)
    except Exception:  # noqa: BLE001 - fall through to a lossy read
        pass
    return data.decode("utf-8", errors="replace")


def _normalise_markdown(text: str) -> str:
    """Tidy Markdown: strip trailing space, collapse blank runs.

    Protected regions are parked first so a code fence or formula keeps its
    internal spacing, which is load-bearing in both.
    """
    masked, blocks = _protect(text)
    masked = "\n".join(line.rstrip() for line in masked.splitlines())
    masked = _EXCESS_BLANK_LINES.sub("\n\n", masked).strip()
    return _restore(masked, blocks)


def _normalise_prose(text: str) -> str:
    """Clean prose whitespace while leaving code and LaTeX exactly as written.

    Collapsing runs of spaces is wrong inside a formula (``a  +  b``) and inside
    a code block, where indentation carries meaning, so those regions are masked
    before any substitution happens.
    """
    masked, blocks = _protect(text)
    masked = masked.replace("\r\n", "\n").replace("\r", "\n")
    masked = _SPACES.sub(" ", masked)
    # A blank line is a paragraph break; more than one is noise.
    masked = _EXCESS_BLANK_LINES.sub("\n\n", masked)
    masked = "\n".join(line.strip() for line in masked.splitlines())
    return _restore(masked, blocks).strip()


# --------------------------------------------------------------------------
# Recursive splitting
# --------------------------------------------------------------------------


def _split_once(text: str, separator: str) -> list[str]:
    """Split *text* on *separator*, keeping the separator on the left piece."""
    if separator == "":
        return list(text)
    if separator == ". ":
        parts = [p for p in _SENTENCE_BOUNDARY.split(text) if p.strip()]
        return parts or [text]
    return [p for p in text.split(separator) if p.strip()]


def _recursive_split(
    text: str,
    max_tokens: int,
    max_chars: int,
    separators: Sequence[str],
) -> list[str]:
    """Split *text* into pieces that each fit the budget, coarsest boundary first.

    Walks *separators* from most to least semantic. A piece already within
    budget is emitted untouched; an oversized piece is re-split with the next
    separator. The character-level separator guarantees termination.
    """
    text = text.strip()
    if not text:
        return []
    if count_tokens(text) <= max_tokens and len(text) <= max_chars:
        return [text]

    if not separators:
        return [text]

    separator, rest = separators[0], separators[1:]
    pieces = _split_once(text, separator)

    # The separator is a boundary, not content: if a single piece is still over
    # budget the split made no progress, so try a finer boundary instead.
    if len(pieces) <= 1 and rest:
        return _recursive_split(text, max_tokens, max_chars, rest)

    out: list[str] = []
    for piece in pieces:
        if count_tokens(piece) <= max_tokens and len(piece) <= max_chars:
            out.append(piece)
        else:
            out.extend(_recursive_split(piece, max_tokens, max_chars, rest))
    return out


def _assign_headings(pieces: Sequence[str]) -> list[str]:
    """The heading in force at the start of each piece.

    Pieces are in document order, so a single forward pass is enough: a piece
    that *is* a heading becomes the current heading for everything after it.
    This lets a retrieved window say which section it came from once it is
    separated from its neighbours.
    """
    headings: list[str] = []
    current = ""
    for piece in pieces:
        match = re.match(r"^(#{1,6}\s+\S.*)$", piece.strip())
        if match:
            current = match.group(1).strip()
        headings.append(current)
    return headings


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------


def chunk_text(
    content: str,
    source_file: str = "<memory>",
    file_type: str = "text",
    *,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
    add_heading_context: bool = False,
) -> list[dict[str, str]]:
    """Split Markdown *content* into overlapping chunks.

    Sizing is bounded by whichever of *max_tokens* / *max_chars* runs out first,
    so long-token or long-character documents are both contained. Overlap is
    likewise whichever of *overlap_tokens* / *overlap_chars* is smaller.

    Formulae, code fences and Markdown tables are never split: they are masked
    during splitting and restored afterwards. A protected block too large for
    the budget becomes one oversized chunk, which is the honest outcome -- a
    truncated formula is worse than a long one.

    Set *add_heading_context* to prefix each chunk with the Markdown heading it
    falls under. This helps a retrieved chunk stay interpretable once it is
    separated from its neighbours, at the cost of repeating the heading.
    """
    if max_tokens < 1 or max_chars < 1:
        raise ChunkingError("max_tokens and max_chars must both be positive")
    if overlap_tokens < 0 or overlap_chars < 0:
        raise ChunkingError("overlap must not be negative")
    if overlap_tokens >= max_tokens or overlap_chars >= max_chars:
        raise ChunkingError(
            f"overlap must be smaller than the window "
            f"(got {overlap_tokens}/{max_tokens} tokens, "
            f"{overlap_chars}/{max_chars} chars)"
        )

    if not content or not content.strip():
        return []

    masked, blocks = _protect(content)

    pieces = _recursive_split(masked, max_tokens, max_chars, _SEPARATORS)
    if not pieces:
        return []

    headings = _assign_headings(pieces) if add_heading_context else []

    chunks: list[dict[str, str]] = []
    index = 0
    position = 0
    total = len(pieces)

    while position < total:
        window: list[str] = []
        used_tokens = 0
        used_chars = 0
        cursor = position

        while cursor < total:
            piece = pieces[cursor]
            piece_tokens = count_tokens(piece)
            # Pieces are rejoined with a blank line, so that separator is part of
            # this piece's cost. Forgetting it lets a chunk overshoot the budget
            # by two characters per piece it contains.
            joiner = len(_PIECE_SEPARATOR) if window else 0
            if window and (
                used_tokens + piece_tokens > max_tokens
                or used_chars + joiner + len(piece) > max_chars
            ):
                break
            window.append(piece)
            used_tokens += piece_tokens
            used_chars += joiner + len(piece)
            cursor += 1
            if used_tokens >= max_tokens or used_chars >= max_chars:
                break

        if not window:  # a single piece over budget: emit it rather than loop
            window = [pieces[position]]
            cursor = position + 1

        body = _restore(_PIECE_SEPARATOR.join(window), blocks).strip()

        if add_heading_context and headings and body:
            heading = headings[position]
            if heading and not body.startswith(heading):
                body = f"{heading}\n\n{body}"

        if body:
            index += 1
            chunks.append(
                {
                    "chunk_id": f"{Path(source_file).stem}-{index:04d}",
                    "source_file": str(source_file),
                    "file_type": file_type,
                    "content": body,
                }
            )

        if cursor >= total:
            break

        # Rewind for overlap: take whole trailing pieces that still fit.
        back: list[str] = []
        overlap_tokens_used = 0
        overlap_chars_used = 0
        probe = cursor - 1
        while probe > position:
            candidate = pieces[probe]
            if (
                overlap_tokens_used + count_tokens(candidate) > overlap_tokens
                or overlap_chars_used + len(candidate) > overlap_chars
            ):
                break
            back.insert(0, candidate)
            overlap_tokens_used += count_tokens(candidate)
            overlap_chars_used += len(candidate)
            probe -= 1

        position = cursor - len(back)

    return chunks


# --------------------------------------------------------------------------
# Convenience layer
# --------------------------------------------------------------------------


def load_and_chunk(
    path: str | Path,
    **chunk_kwargs: Any,
) -> list[dict[str, str]]:
    """Parse *path* and return its chunks.

    Raises :class:`DocumentLoadError` if the file cannot be parsed. A file that
    parses to no chunks returns an empty list rather than raising, so a caller
    iterating a corpus can keep going.
    """
    document = load_document(path, file_type=chunk_kwargs.pop("file_type", None))
    return chunk_text(
        document.content,
        source_file=document.source_file,
        file_type=document.file_type,
        **chunk_kwargs,
    )


def load_directory(
    directory: str | Path,
    *,
    pattern: str = "*",
    recursive: bool = True,
    on_error: str = "collect",
    **chunk_kwargs: Any,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Chunk every supported file under *directory*.

    Returns ``(chunks, errors)``. By default failures are collected rather than
    raised, so one unreadable file cannot abort a corpus run. Each error is
    ``{"source_file": ..., "error": ...}``.

    Pass ``on_error="raise"`` to fail fast on the first bad file instead, which
    is what a test or a small, fully-trusted corpus usually wants.
    """
    if on_error not in {"collect", "raise"}:
        raise DocumentLoadError(
            f"on_error must be 'collect' or 'raise', not {on_error!r}"
        )

    root = Path(directory).expanduser()
    if not root.is_dir():
        raise DocumentLoadError(f"not a directory: {root}")

    paths: Iterable[Path]
    paths = sorted(root.rglob(pattern) if recursive else root.glob(pattern))

    chunks: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    for candidate in paths:
        if not candidate.is_file():
            continue
        if candidate.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        try:
            chunks.extend(load_and_chunk(candidate, **chunk_kwargs))
        except (DocumentLoadError, ChunkingError) as exc:
            if on_error == "raise":
                raise
            errors.append({"source_file": str(candidate), "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - never lose the rest of the corpus
            if on_error == "raise":
                raise DocumentLoadError(
                    f"failed to parse {candidate.name}: {exc}"
                ) from exc
            errors.append({"source_file": str(candidate), "error": f"unexpected: {exc}"})

    return chunks, errors
