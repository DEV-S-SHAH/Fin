"""Tests for the standalone universal document loader.

The loader is exercised against real files on disk -- a genuine multi-page PDF,
and HTML/CSV/Markdown/text fixtures written to a temp directory -- rather than
against mocked parsers, because most of the defects worth catching here
(extractor disagreement, layout-artefact tables, dropped word spaces) only show
up on real bytes.
"""

from __future__ import annotations

import logging
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import document_loader as dl

# The corrupt-PDF fixtures below feed pypdf deliberately broken bytes, which it
# reports through its logger. That is the test working, not a problem, so keep it
# out of the test output.
logging.getLogger("pypdf").setLevel(logging.CRITICAL)
logging.getLogger("pdfminer").setLevel(logging.CRITICAL)


SAMPLES = Path(__file__).resolve().parent.parent / "samples"
ATTENTION_PDF = SAMPLES / "attention_is_all_you_need.pdf"

HTML_PAGE = """<!DOCTYPE html>
<html><head><title>Ignored Title</title>
<style>.hidden { display: none }</style>
<script>var tracker = 1;</script>
</head>
<body>
<nav><a href="/">Home</a><a href="/about">About</a></nav>
<header><h1>Site Header Must Go</h1></header>
<main>
  <h1>Quarterly Report</h1>
  <p>The <b>revenue</b> rose by 12% year over year, driven mainly by the
     northern region and a new gadget line.</p>
  <h2>Regional Detail</h2>
  <table>
    <tr><th>Region</th><th>Revenue</th></tr>
    <tr><td>North</td><td>1,200</td></tr>
    <tr><td>South</td><td>980</td></tr>
  </table>
  <h2>Outlook</h2>
  <ul><li>Margin pressure from input costs</li><li>New factory in Q3</li></ul>
  <pre><code class="language-python">def total(rows):
    return sum(r["revenue"] for r in rows)
</code></pre>
</main>
<footer>Copyright 2026. All rights reserved.</footer>
</body></html>
"""

CSV_ROWS = """Region,Quarter,Revenue,Product
North,Q1,1200,Widget
South,Q1,980,Widget
North,Q2,1540,Gadget
"""

MARKDOWN_DOC = """# On Estimators

Given a sample $X \\sim \\mathcal{N}(\\mu, \\sigma^2)$ we estimate $\\mu$ with
$\\hat{\\mu} = \\frac{1}{n}\\sum_{i=1}^{n} x_i$.

$$
\\mathcal{L}(\\theta) = -\\sum_{i=1}^{n} \\log p_\\theta(y_i \\mid x_i)
$$

## Code

```python
def   mean(xs):   # deliberate odd spacing
    return sum(xs) / len(xs)
```

| a | b |
|---|---|
| 1 | 2 |
"""

LONG_PROSE = " ".join(
    f"Sentence number {index} carries a little payload about topic {index % 7}."
    for index in range(400)
)


class _TempFileMixin(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, name: str, text: str) -> Path:
        """Write a fixture with the newlines the literal already has.

        ``write_text`` opens in text mode, so on Windows every ``\n`` in the
        source literal becomes ``\r\n`` on disk -- and the loader faithfully
        preserves what was written. A test that then compares against the
        LF-only literal it passed in is comparing against different bytes, and
        reports a byte-exactness failure for content that survived intact.
        Pinning the newline here makes the fixture mean the same thing on every
        platform, which is the only way "survives byte-for-byte" is testable.
        """
        path = self.dir / name
        path.write_text(text, encoding="utf-8", newline="\n")
        return path


class FormatTests(_TempFileMixin):
    def test_csv_rows_become_self_describing_sentences(self):
        doc = dl.load_document(self.write("data.csv", CSV_ROWS))
        self.assertEqual(doc.file_type, "csv")
        self.assertEqual(doc.metadata["rows"], 3)
        self.assertIn("Record in [data.csv]:", doc.content)
        # Header names carry into every sentence, so a retrieved row still
        # says what the values mean.
        self.assertIn("Region is North, Quarter is Q1, Revenue is 1200", doc.content)
        self.assertIn("Product is Gadget", doc.content)
        self.assertEqual(doc.content.count("Record in [data.csv]:"), 3)

    def test_csv_without_header_falls_back_to_column_position(self):
        doc = dl.load_document(self.write("plain.csv", "1,2,3\n4,5,6\n"))
        self.assertIn("column_1 is 1", doc.content)
        self.assertIn("column_3 is 3", doc.content)

    def test_html_keeps_structure_and_drops_chrome(self):
        content = dl.load_document(self.write("page.html", HTML_PAGE)).content

        # Structure survives.
        self.assertIn("# Quarterly Report", content)
        self.assertIn("## Regional Detail", content)
        self.assertIn("| Region | Revenue |", content)
        self.assertIn("- Margin pressure from input costs", content)
        self.assertIn("```python", content)

        # Chrome does not.
        for noise in ("Site Header Must Go", "Copyright 2026", "var tracker",
                      "display: none", "About"):
            self.assertNotIn(noise, content)

    def test_html_table_is_not_duplicated(self):
        content = dl.load_document(self.write("page.html", HTML_PAGE)).content
        self.assertEqual(content.count("| Region | Revenue |"), 1)

    def test_html_joins_inline_tags_into_one_sentence(self):
        content = dl.load_document(self.write("page.html", HTML_PAGE)).content
        # <b> must not split the sentence into three blocks.
        self.assertIn("The revenue rose by 12%", content)
        self.assertNotIn("The\n\nrevenue", content)
        # A hard wrap in the source must not become a hard break in the output.
        self.assertNotIn("the\n   northern", content)
        self.assertIn("the northern region", content)

    def test_markdown_preserves_math_code_and_tables(self):
        content = dl.load_document(self.write("paper.md", MARKDOWN_DOC)).content

        for math in ("$X \\sim \\mathcal{N}(\\mu, \\sigma^2)$",
                     "$\\hat{\\mu} = \\frac{1}{n}\\sum_{i=1}^{n} x_i$",
                     "$$\n\\mathcal{L}(\\theta)",
                     "\\log p_\\theta(y_i \\mid x_i)"):
            self.assertIn(math, content)

        # Display math keeps its own line.
        self.assertIn("$$\n", content)

        # Code block internals are byte-for-byte, including the odd spacing.
        self.assertIn("def   mean(xs):   # deliberate odd spacing", content)
        self.assertIn("    return sum(xs) / len(xs)", content)

        self.assertIn("| a | b |", content)
        self.assertIn("| 1 | 2 |", content)

    def test_text_collapses_whitespace_runs(self):
        doc = dl.load_document(
            self.write("notes.txt", "Plain    text\twith\tspaces.\n\n\n\nSecond para.\n")
        )
        self.assertEqual(doc.file_type, "text")
        self.assertIn("Plain text with spaces.", doc.content)
        self.assertNotIn("\n\n\n", doc.content)

    def test_document_records_token_count_and_metadata(self):
        doc = dl.load_document(self.write("a.md", MARKDOWN_DOC))
        self.assertEqual(doc.file_type, "markdown")
        self.assertGreater(doc.token_count, 0)
        self.assertEqual(doc.metadata["extension"], ".md")
        self.assertEqual(doc.source_file, str(self.dir / "a.md"))


class PdfTests(unittest.TestCase):
    def setUp(self):
        if not ATTENTION_PDF.exists():
            self.skipTest(f"{ATTENTION_PDF.name} is not present")

    def test_real_paper_yields_spaced_text_and_page_markers(self):
        doc = dl.load_document(ATTENTION_PDF)
        self.assertEqual(doc.file_type, "pdf")
        self.assertEqual(doc.metadata["pages"], 15)
        self.assertIn("<!-- page 1 -->", doc.content)
        self.assertIn("<!-- page 15 -->", doc.content)

    def test_word_spaces_survive_extraction(self):
        # pdfplumber alone collapses spaces on this PDF, gluing words together
        # and destroying retrieval quality; the loader must not do that.
        doc = dl.load_document(ATTENTION_PDF)
        self.assertGreater(dl._space_density(doc.content), 0.08)
        self.assertIn("Attention Is All You Need", doc.content)
        self.assertIn("Ashish Vaswani", doc.content)
        self.assertNotIn("AttentionIsAllYouNeed", doc.content)

    def test_layout_artefact_tables_are_dropped(self):
        # The paper's ruled figure boxes are not data tables.
        doc = dl.load_document(ATTENTION_PDF)
        self.assertEqual(doc.metadata["tables"], 0)

    def test_ruled_data_table_is_extracted(self):
        try:
            from reportlab.lib import colors
            from reportlab.lib.pagesizes import LETTER
            from reportlab.platypus import SimpleDocTemplate, Table, TableStyle
        except ImportError:
            self.skipTest("reportlab is needed to build a table fixture")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "table.pdf"
            grid = Table(
                [
                    ["Model", "BLEU", "Params", "Layers"],
                    ["Base", "25.8", "65M", "6"],
                    ["Big", "26.9", "213M", "12"],
                    ["Huge", "28.4", "770M", "24"],
                ]
            )
            grid.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 1, colors.black),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                    ]
                )
            )
            SimpleDocTemplate(str(path), pagesize=LETTER).build([grid])

            doc = dl.load_document(path)
            self.assertEqual(doc.metadata["tables"], 1)
            self.assertIn("| Model | BLEU | Params | Layers |", doc.content)
            self.assertIn("| Huge | 28.4 | 770M | 24 |", doc.content)

    def test_table_filter_rejects_artefacts(self):
        artefact = [["tI", "si", "ni", "n"] * 12, ["ht", "ip", "ht", "ni"] * 12]
        self.assertEqual(dl._table_to_markdown(artefact), "")
        # A boxed paragraph is a single column, not a table.
        self.assertEqual(dl._table_to_markdown([["A boxed sentence of prose."], ["Another."]]), "")
        # A header with no body is not a table.
        self.assertEqual(dl._table_to_markdown([["Model", "BLEU"]]), "")


class ChunkingTests(_TempFileMixin):
    def test_chunks_respect_both_budgets(self):
        path = self.write("long.md", LONG_PROSE)
        chunks = dl.load_and_chunk(path, max_tokens=200, max_chars=700, overlap_tokens=30)

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(dl.count_tokens(chunk["content"]), 200)
            self.assertLessEqual(len(chunk["content"]), 700)

    def test_chunk_schema_is_exactly_as_specified(self):
        path = self.write("long.md", LONG_PROSE)
        chunks = dl.load_and_chunk(path, max_tokens=120, overlap_tokens=20)
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertEqual(set(chunk), {"chunk_id", "source_file", "file_type", "content"})
            self.assertIsInstance(chunk["chunk_id"], str)
            self.assertIsInstance(chunk["content"], str)
            self.assertTrue(chunk["content"].strip())
            self.assertEqual(chunk["file_type"], "markdown")
            self.assertEqual(chunk["source_file"], str(path))

    def test_chunk_ids_are_unique_and_ordered(self):
        path = self.write("long.md", LONG_PROSE)
        chunks = dl.load_and_chunk(path, max_tokens=120, overlap_tokens=20)
        ids = [chunk["chunk_id"] for chunk in chunks]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids, sorted(ids))

    def test_consecutive_chunks_overlap(self):
        path = self.write("long.md", LONG_PROSE)
        chunks = dl.load_and_chunk(path, max_tokens=200, overlap_tokens=50)
        self.assertGreater(len(chunks), 2)
        overlaps = 0
        for first, second in zip(chunks, chunks[1:]):
            tail = set(dl.tokenize(first["content"])[-25:])
            head = set(dl.tokenize(second["content"])[:25])
            if tail & head:
                overlaps += 1
        self.assertEqual(overlaps, len(chunks) - 1)

    def test_no_content_is_lost_between_chunks(self):
        path = self.write("long.md", LONG_PROSE)
        chunks = dl.load_and_chunk(path, max_tokens=200, overlap_tokens=40)
        seen = set()
        for chunk in chunks:
            seen.update(dl.tokenize(chunk["content"]))
        # Every distinctive token of the source survives the chunking round trip.
        for index in range(0, 400, 7):
            self.assertIn(f"{index}", seen)

    def test_zero_overlap_is_allowed(self):
        path = self.write("long.md", LONG_PROSE)
        chunks = dl.load_and_chunk(path, max_tokens=150, max_chars=600, overlap_tokens=0, overlap_chars=0)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(dl.count_tokens(chunk["content"]), 150)

    def test_oversized_protected_block_is_kept_whole(self):
        # A code fence larger than the budget cannot be split without corrupting
        # it, so it is emitted intact rather than cut in half.
        big_fence = "```python\n" + "".join(f"step_{i} = compute({i})\n" for i in range(400)) + "```"
        path = self.write("code.md", f"# Title\n\n{big_fence}\n")
        chunks = dl.load_and_chunk(path, max_tokens=100, max_chars=400, overlap_tokens=0, overlap_chars=0)
        self.assertTrue(chunks)
        self.assertIn("```python", chunks[0]["content"])
        self.assertTrue(chunks[0]["content"].rstrip().endswith("```"))
        self.assertIn("step_399 = compute(399)", chunks[0]["content"])

    def test_code_block_is_never_split_across_chunks(self):
        body = "".join(f"line_{i} = value_{i}\n" for i in range(200))
        block = f"```python\n{body}```"
        path = self.write("code.md", f"{block}\n")
        chunks = dl.load_and_chunk(
            path, max_tokens=80, max_chars=300, overlap_tokens=20, overlap_chars=40
        )

        # Fences delimit the block; an odd count in a chunk would mean it was
        # cut open. An even count means either absent or whole.
        for chunk in chunks:
            self.assertEqual(chunk["content"].count("```") % 2, 0)

        # The oversized block is emitted intact rather than divided.
        carriers = [chunk for chunk in chunks if "```" in chunk["content"]]
        self.assertEqual(len(carriers), 1)
        self.assertIn("line_199 = value_199", carriers[0]["content"])
        self.assertIn("line_0 = value_0", carriers[0]["content"])

    def test_overlap_must_be_smaller_than_the_window(self):
        # An overlap at or above the window size would stall the cursor forever,
        # so it is rejected up front rather than hanging.
        with self.assertRaises(dl.ChunkingError):
            dl.chunk_text(LONG_PROSE, max_tokens=80, max_chars=300, overlap_tokens=90)
        with self.assertRaises(dl.ChunkingError):
            dl.chunk_text(LONG_PROSE, max_tokens=80, max_chars=300, overlap_chars=400)

    def test_nonsensical_budgets_are_rejected(self):
        for kwargs in (
            {"max_tokens": 0},
            {"max_chars": 0},
            {"max_tokens": -5},
            {"overlap_tokens": -1},
        ):
            with self.assertRaises(dl.ChunkingError):
                dl.chunk_text(LONG_PROSE, **kwargs)

    def test_math_block_is_never_split(self):
        block = "$$\n" + " \\frac{1}{n} " * 60 + "\n$$"
        path = self.write("math.md", f"Intro text here.\n\n{block}\n\nClosing text.\n")
        chunks = dl.load_and_chunk(
            path, max_tokens=60, max_chars=200, overlap_tokens=0, overlap_chars=0
        )

        # A display-math block that is larger than the budget cannot be cut
        # without corrupting it, so it is emitted intact and allowed to overshoot
        # rather than being split across chunks.
        carriers = [chunk for chunk in chunks if "$$" in chunk["content"]]
        self.assertEqual(len(carriers), 1, "the math block must live in exactly one chunk")
        self.assertIn(block, carriers[0]["content"])

        # No chunk may contain a half-open delimiter: an odd number of $$ means
        # the block was cut.
        for chunk in chunks:
            self.assertEqual(chunk["content"].count("$$") % 2, 0)

        # The surrounding prose survives.
        joined = "\n".join(chunk["content"] for chunk in chunks)
        self.assertIn("Intro text here.", joined)
        self.assertIn("Closing text.", joined)

    def test_table_is_never_split(self):
        table = "| name | value |\n| --- | --- |\n" + "".join(
            f"| row {i} | {i * 7} |\n" for i in range(60)
        )
        path = self.write("table.md", f"# Data\n\n{table}\n")
        chunks = dl.load_and_chunk(
            path, max_tokens=70, max_chars=280, overlap_tokens=0, overlap_chars=0
        )

        # The table is atomic: it appears whole, in one chunk, with its header
        # separator intact rather than being divided across chunks.
        carriers = [chunk for chunk in chunks if "| --- |" in chunk["content"]]
        self.assertEqual(len(carriers), 1)
        self.assertIn(table.strip(), carriers[0]["content"])

    def test_heading_context_option_labels_each_chunk(self):
        body = "".join(
            f"## Section {section}\n\n" + " ".join(f"word{section}_{i}" for i in range(60)) + "\n\n"
            for section in range(3)
        )
        path = self.write("sections.md", body)
        plain = dl.load_and_chunk(path, max_tokens=90, max_chars=320, overlap_tokens=0, overlap_chars=0)
        labelled = dl.load_and_chunk(
            path, max_tokens=90, max_chars=320, overlap_tokens=0, overlap_chars=0, add_heading_context=True
        )
        self.assertEqual(len(plain), len(labelled))
        # A chunk cut off from its heading still says which section it is from.
        self.assertTrue(
            any(chunk["content"].startswith("## Section") for chunk in labelled)
        )
        # The original content is still present underneath the added label.
        self.assertIn("word1_30", "\n".join(chunk["content"] for chunk in labelled))

    def test_empty_document_is_reported_not_silently_chunked(self):
        # A file with no extractable text is an error worth surfacing, not an
        # empty chunk list that looks like a successful run over nothing.
        with self.assertRaises(dl.DocumentLoadError):
            dl.load_and_chunk(self.write("empty.md", "   \n\n  \n"))

    def test_chunk_text_of_blank_content_returns_no_chunks(self):
        self.assertEqual(dl.chunk_text("   \n\n  "), [])


class ErrorHandlingTests(_TempFileMixin):
    def test_missing_file_raises_load_error(self):
        with self.assertRaises(dl.DocumentLoadError):
            dl.load_document(self.dir / "nope.md")

    def test_directory_raises_load_error(self):
        with self.assertRaises(dl.DocumentLoadError):
            dl.load_document(self.dir)

    def test_unsupported_extension_raises_load_error(self):
        path = self.dir / "data.bin"
        path.write_bytes(b"\x00\x01\x02")
        with self.assertRaises(dl.DocumentLoadError):
            dl.load_document(path)

    def test_explicit_file_type_overrides_extension(self):
        path = self.write("data.txt", CSV_ROWS)
        doc = dl.load_document(path, file_type="csv")
        self.assertEqual(doc.file_type, "csv")
        self.assertIn("Record in [data.txt]:", doc.content)

    def test_invalid_file_type_raises_load_error(self):
        path = self.write("a.md", "# Title")
        with self.assertRaises(dl.DocumentLoadError):
            dl.load_document(path, file_type="docx")

    def test_corrupt_pdf_raises_load_error(self):
        path = self.dir / "broken.pdf"
        path.write_bytes(b"%PDF-1.4 this is not really a pdf")
        with self.assertRaises(dl.DocumentLoadError):
            dl.load_document(path)

    def test_csv_with_ragged_rows_is_tolerated(self):
        path = self.write("ragged.csv", "A,B,C\n1,2\n3,4,5,6\n")
        doc = dl.load_document(path)
        self.assertIn("A is 1", doc.content)
        self.assertIn("C is 5", doc.content)

    def test_undecodable_bytes_are_recovered(self):
        path = self.dir / "latin.txt"
        path.write_bytes("caf\xe9 na\xefve".encode("latin-1"))
        doc = dl.load_document(path)
        self.assertIn("na", doc.content)
        self.assertIn("ve", doc.content)


class DirectoryTests(_TempFileMixin):
    def test_load_directory_collects_supported_files(self):
        self.write("a.md", "# One\n\nAlpha text.\n")
        self.write("b.csv", CSV_ROWS)
        self.write("c.html", HTML_PAGE)
        self.write("d.txt", "Plain text.\n")
        (self.dir / "skip.bin").write_bytes(b"\x00")
        self.write("skip.py", "raise SystemExit\n")

        chunks, errors = dl.load_directory(self.dir)
        self.assertEqual(errors, [])
        names = sorted({chunk["source_file"] for chunk in chunks})
        self.assertEqual(len(names), 4)
        self.assertTrue(all(n.endswith((".md", ".csv", ".html", ".txt")) for n in names))

    def test_load_directory_does_not_let_one_bad_file_stop_the_rest(self):
        self.write("good.md", "# Fine\n\nReadable text.\n")
        broken = self.dir / "broken.pdf"
        broken.write_bytes(b"%PDF-1.4 not a pdf")
        self.write("alsogood.txt", "Also fine.\n")

        docs, failures = dl.load_directory(self.dir, on_error="collect")

        self.assertEqual(len(docs), 2)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]["source_file"], str(broken))
        self.assertTrue(failures[0]["error"])

    def test_strict_mode_raises_on_a_bad_file(self):
        self.write("good.md", "# Fine\n")
        (self.dir / "broken.pdf").write_bytes(b"%PDF-1.4 not a pdf")
        with self.assertRaises(dl.DocumentLoadError):
            dl.load_directory(self.dir, on_error="raise")

    def test_end_to_end_directory_ingest_produces_chunks(self):
        self.write("a.md", "# One\n\n" + LONG_PROSE)
        self.write("b.txt", LONG_PROSE)
        (self.dir / "skip.bin").write_bytes(b"\x00")

        chunks, errors = dl.load_directory(self.dir, max_tokens=200, max_chars=700, overlap_tokens=30)
        self.assertEqual(errors, [])
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertEqual(set(chunk), {"chunk_id", "source_file", "file_type", "content"})
            self.assertLessEqual(dl.count_tokens(chunk["content"]), 200)


class PublicApiTests(unittest.TestCase):
    def test_tokenizer_counts_words_and_punctuation(self):
        self.assertEqual(dl.count_tokens("Hello, world."), 4)
        self.assertEqual(dl.count_tokens(""), 0)

    def test_documented_defaults(self):
        import inspect

        signature = inspect.signature(dl.chunk_text)
        self.assertEqual(signature.parameters["max_tokens"].default, 800)
        self.assertEqual(signature.parameters["max_chars"].default, 3000)
        self.assertEqual(signature.parameters["overlap_tokens"].default, 100)
        self.assertEqual(signature.parameters["overlap_chars"].default, 400)

    def test_module_exports_are_importable(self):
        for name in dl.__all__:
            self.assertTrue(hasattr(dl, name), f"{name} is exported but missing")

    def test_annotations_resolve_at_runtime(self):
        # A TYPE_CHECKING-only import behind an annotation is invisible to
        # pyflakes but still breaks anything that calls get_type_hints at
        # runtime, so the public and internal signatures are checked directly.
        import typing

        for function in (
            dl.load_document,
            dl.chunk_text,
            dl.load_and_chunk,
            dl.load_directory,
            dl._code_language,
            dl._inline_text,
            dl._is_inline_only,
            dl._render_html_node,
            dl._html_table_to_markdown,
            dl._table_to_markdown,
        ):
            with self.subTest(function=function.__name__):
                self.assertIsInstance(typing.get_type_hints(function), dict)


if __name__ == "__main__":
    unittest.main()
