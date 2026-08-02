"""Tests for parser implementations: Markdown, TXT, PDF, and factory."""

from __future__ import annotations

from pathlib import Path

import pytest
from domain.parsing import (
    ParsedDocument,
    ParseError,
    ParseErrorCode,
    ParseMetadata,
    ParseSuccess,
    StructNodeType,
)
from infrastructure.parsers.factory import ParserFactory, get_parser
from infrastructure.parsers.markdown_parser import MarkdownParser
from infrastructure.parsers.pdf_parser import PdfParser
from infrastructure.parsers.txt_parser import TxtParser

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


# ===========================================================================
#  Helpers
# ===========================================================================


async def _parse(parser, name: str) -> ParseSuccess:
    raw = (FIXTURES / name).read_bytes()
    mime_map = {
        "sample.md": "text/markdown",
        "sample.txt": "text/plain",
        "sample.pdf": "application/pdf",
    }
    meta = ParseMetadata(file_name=name, mime_type=mime_map.get(name, ""))
    result = await parser.parse(raw, meta)
    assert isinstance(result, ParseSuccess), f"Expected success, got error: {result}"
    return result


# ===========================================================================
#  Markdown parser
# ===========================================================================


class TestMarkdownParser:
    @pytest.fixture
    def parser(self) -> MarkdownParser:
        return MarkdownParser()

    async def test_parse_sample(self, parser) -> None:
        doc = (await _parse(parser, "sample.md")).document
        assert doc.total_lines > 0
        assert "Chapter 1" in doc.text

    async def test_structure(self, parser) -> None:
        doc = (await _parse(parser, "sample.md")).document
        types = {n.node_type for n in doc.structure}
        assert StructNodeType.HEADING in types
        assert StructNodeType.CODE_BLOCK in types
        assert StructNodeType.LIST_ITEM in types

    async def test_empty_document(self, parser) -> None:
        result = await parser.parse(
            b"", ParseMetadata(file_name="empty.md", mime_type="text/markdown")
        )
        assert isinstance(result, ParseError) and result.code == ParseErrorCode.EMPTY_DOCUMENT

    async def test_encoding_failure(self, parser) -> None:
        meta = ParseMetadata(file_name="bad.md", mime_type="text/markdown", encoding="ascii")
        result = await parser.parse(b"\xff\xfe\x00\xff", meta)
        assert isinstance(result, ParseError) and result.code == ParseErrorCode.ENCODING_FAILURE

    async def test_line_numbers_1based(self, parser) -> None:
        doc = (await _parse(parser, "sample.md")).document
        assert all(n.start_line >= 1 for n in doc.structure if n.start_line > 0)


# ===========================================================================
#  TXT parser
# ===========================================================================


class TestTxtParser:
    @pytest.fixture
    def parser(self) -> TxtParser:
        return TxtParser()

    async def test_parse_sample(self, parser) -> None:
        doc = (await _parse(parser, "sample.txt")).document
        assert doc.total_lines > 0
        assert doc.metadata.encoding == "utf-8"

    async def test_paragraphs_from_blank_lines(self, parser) -> None:
        doc = (await _parse(parser, "sample.txt")).document
        paras = [n for n in doc.structure if n.node_type == StructNodeType.PARAGRAPH]
        assert len(paras) >= 3

    async def test_empty_document(self, parser) -> None:
        result = await parser.parse(
            b"", ParseMetadata(file_name="empty.txt", mime_type="text/plain")
        )
        assert isinstance(result, ParseError) and result.code == ParseErrorCode.EMPTY_DOCUMENT

    async def test_encoding_fallback(self, parser) -> None:
        raw = "café résumé".encode("latin-1")
        meta = ParseMetadata(file_name="test.txt", mime_type="text/plain", encoding="utf-8")
        result = await parser.parse(raw, meta)
        assert isinstance(result, ParseSuccess)

    async def test_whitespace_only_is_empty(self, parser) -> None:
        result = await parser.parse(
            b"   \n  ", ParseMetadata(file_name="sp.txt", mime_type="text/plain")
        )
        assert isinstance(result, ParseError) and result.code == ParseErrorCode.EMPTY_DOCUMENT


# ===========================================================================
#  PDF parser
# ===========================================================================


class TestPdfParser:
    @pytest.fixture
    def parser(self) -> PdfParser:
        return PdfParser()

    async def test_parse_sample(self, parser) -> None:
        doc = (await _parse(parser, "sample.pdf")).document
        assert doc.total_lines > 0
        assert "Hello World" in doc.text

    async def test_page_nodes(self, parser) -> None:
        doc = (await _parse(parser, "sample.pdf")).document
        pages = [n for n in doc.structure if n.start_page is not None]
        assert len(pages) >= 1
        assert all(p.start_page and p.start_page >= 1 for p in pages)

    async def test_empty_pdf(self, parser) -> None:
        from io import BytesIO

        from pypdf import PdfWriter

        w = PdfWriter()
        w.add_blank_page(612, 792)
        buf = BytesIO()
        w.write(buf)
        meta = ParseMetadata(file_name="blank.pdf", mime_type="application/pdf")
        result = await parser.parse(buf.getvalue(), meta)
        assert isinstance(result, ParseError) and result.code == ParseErrorCode.SCANNED_PDF

    async def test_corrupt_pdf(self, parser) -> None:
        meta = ParseMetadata(file_name="corrupt.pdf", mime_type="application/pdf")
        result = await parser.parse(b"not a pdf", meta)
        assert isinstance(result, ParseError) and result.code == ParseErrorCode.CONTENT_CORRUPT


# ===========================================================================
#  PDF structure parser (geometry heuristics)
# ===========================================================================


def _make_pdf(pages: list[list[tuple[float, float, float, str]]]) -> bytes:
    """Build a PDF from page descriptions.

    Each page is a list of ``(x, y, size, text)`` insertions.  Text is inserted
    with Helvetica at the given size; y is the baseline in PDF points.
    """
    from io import BytesIO

    import fitz

    doc = fitz.open()
    for page_items in pages:
        page = doc.new_page(width=612, height=792)
        for x, y, size, text in page_items:
            page.insert_text((x, y), text, fontsize=size, fontname="helv")
    buf = BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


class TestPdfStructureParser:
    """Structural extraction: headings, paragraphs, list items, math spans."""

    @pytest.fixture
    def parser(self) -> PdfParser:
        return PdfParser()

    async def _parse_bytes(self, parser, raw: bytes, name: str = "struct.pdf") -> ParsedDocument:
        meta = ParseMetadata(file_name=name, file_size=len(raw), mime_type="application/pdf")
        result = await parser.parse(raw, meta)
        assert isinstance(result, ParseSuccess), f"Expected success, got error: {result}"
        return result.document

    async def test_heading_detection(self, parser) -> None:
        # body 11pt; headings 22 / 16pt, plus a 11pt paragraph
        raw = _make_pdf(
            [
                [
                    (72, 100, 22, "Section One"),
                    (72, 140, 16, "Subsection"),
                    (72, 180, 11, "This is body text that should be a paragraph."),
                ]
            ]
        )
        doc = await self._parse_bytes(parser, raw)
        types = [n.node_type for n in doc.structure]
        assert StructNodeType.HEADING in types
        heading = next(n for n in doc.structure if n.node_type == StructNodeType.HEADING)
        assert heading.text == "Section One"
        assert heading.level == 1
        para = next(n for n in doc.structure if n.node_type == StructNodeType.PARAGRAPH)
        assert "body text" in para.text

    async def test_wrapped_heading_merged_by_same_size(self, parser) -> None:
        # A heading that wraps to a second line at the SAME size merges.  Body
        # text must dominate character count so the 16pt lines read as headings.
        body = (
            "This is a long body paragraph that fills the page so the "
            "body font size carries more characters than the short heading "
            "lines and the heuristic selects the body size as dominant."
        )
        raw = _make_pdf(
            [
                [
                    (72, 100, 16, "Part Six Regularization"),
                    (72, 125, 16, "and model selection"),
                    (72, 170, 11, body),
                    (72, 185, 11, "More body text on the following line to dominate."),
                ]
            ]
        )
        doc = await self._parse_bytes(parser, raw)
        headings = [n for n in doc.structure if n.node_type == StructNodeType.HEADING]
        assert len(headings) == 1
        assert "Regularization" in headings[0].text and "model selection" in headings[0].text

    async def test_different_size_headings_stay_separate(self, parser) -> None:
        # Title (22pt) and section (16pt) are different headings, not merged.
        raw = _make_pdf(
            [
                [
                    (72, 100, 22, "Lecture Notes"),
                    (72, 140, 16, "Part One"),
                    (72, 180, 11, "Some body paragraph."),
                ]
            ]
        )
        doc = await self._parse_bytes(parser, raw)
        headings = [n.text for n in doc.structure if n.node_type == StructNodeType.HEADING]
        assert headings == ["Lecture Notes", "Part One"]

    async def test_list_item_detection(self, parser) -> None:
        raw = _make_pdf(
            [
                [
                    (72, 100, 11, "1. First item"),
                    (72, 120, 11, "2. Second item"),
                    (72, 160, 11, "A normal paragraph."),
                ]
            ]
        )
        doc = await self._parse_bytes(parser, raw)
        items = [n for n in doc.structure if n.node_type == StructNodeType.LIST_ITEM]
        assert len(items) == 2
        assert items[0].text.startswith("1. First")
        assert items[1].text.startswith("2. Second")

    async def test_math_subscript_merged_into_line(self, parser) -> None:
        # "h_i" rendered as two spans (h at 11pt, subscript i at 8pt) must merge
        # into a single visual line, not fragment into a stray "i" node.
        raw = _make_pdf(
            [
                [
                    (72, 100, 11, "The formula h"),
                    (72 + 60, 105, 8, "i"),
                    (72 + 66, 100, 11, " appears inline."),
                ]
            ]
        )
        doc = await self._parse_bytes(parser, raw)
        paras = [n for n in doc.structure if n.node_type == StructNodeType.PARAGRAPH]
        assert len(paras) == 1
        combined = paras[0].text
        # subscript "i" should be adjacent to "h" with no stray standalone node
        assert "h" in combined and "i" in combined
        assert "appears inline" in combined

    async def test_span_reading_order_fixes_math(self, parser) -> None:
        # Spans on one baseline appear in x-order regardless of insertion order.
        raw = _make_pdf(
            [
                [
                    (200, 100, 11, "right"),
                    (72, 100, 11, "left"),
                    (140, 100, 11, "middle"),
                ]
            ]
        )
        doc = await self._parse_bytes(parser, raw)
        paras = [n for n in doc.structure if n.node_type == StructNodeType.PARAGRAPH]
        assert len(paras) == 1
        text = paras[0].text
        # reading order is left→middle→right
        assert text.index("left") < text.index("middle") < text.index("right")

    async def test_two_column_layout_not_interleaved(self, parser) -> None:
        # Two columns at the same baselines must stay separate (each column is
        # its own PyMuPDF block), not interleaved into scrambled lines.
        raw = _make_pdf(
            [
                [
                    # left column
                    (72, 100, 11, "Left column first line."),
                    (72, 115, 11, "Left column second line."),
                    # right column, same baselines
                    (360, 100, 11, "Right column first line."),
                    (360, 115, 11, "Right column second line."),
                ]
            ]
        )
        doc = await self._parse_bytes(parser, raw)
        paras = [n for n in doc.structure if n.node_type == StructNodeType.PARAGRAPH]
        combined = " ".join(p.text for p in paras)
        # left column content must read continuously, not interspersed with right
        assert combined.index("Left column first") < combined.index("Left column second")
        assert combined.index("Right column first") < combined.index("Right column second")
        # interleaving would put "Right column first" between the two left lines
        left_span = combined.index("Left column first"), combined.index("Left column second")
        assert not (left_span[0] < combined.index("Right column first") < left_span[1])

    async def test_node_carries_page_number(self, parser) -> None:
        # Page 1 carries body text (anchors body size); page 2 has only a
        # heading, which must still be detected via the running body estimate.
        body = (
            "This is the first page body paragraph with enough characters "
            "to anchor the document body font size for the running estimate."
        )
        raw = _make_pdf(
            [
                [(72, 100, 11, body)],
                [(72, 100, 16, "Second Page Heading")],
            ]
        )
        doc = await self._parse_bytes(parser, raw)
        by_page: dict[int, list[str]] = {}
        for n in doc.structure:
            by_page.setdefault(n.start_page or 0, []).append(n.node_type.value)
        assert by_page[1] == ["paragraph"]
        assert by_page[2] == ["heading"]

    async def test_scanned_page_still_returns_error(self, parser) -> None:
        # A PDF with no text spans still maps to scanned_pdf.
        raw = _make_pdf([[(72, 100, 11, "")]])  # empty text insertion
        meta = ParseMetadata(file_name="blank2.pdf", mime_type="application/pdf")
        result = await parser.parse(raw, meta)
        assert isinstance(result, ParseError) and result.code == ParseErrorCode.SCANNED_PDF


# ===========================================================================
#  Parser factory
# ===========================================================================


class TestParserFactory:
    @pytest.mark.parametrize(
        ("name", "data", "mime", "expect_success"),
        [
            ("test.md", b"# Hello", None, True),
            ("notes.txt", b"hello", None, True),
            ("doc.pdf", (FIXTURES / "sample.pdf").read_bytes(), "application/pdf", True),
            ("file.docx", b"data", None, False),
            ("README", b"data", None, False),
        ],
    )
    async def test_by_extension(
        self, name: str, data: bytes, mime: str | None, expect_success: bool
    ) -> None:
        factory = ParserFactory()
        result = await factory.parse(data, name, mime_type=mime)
        if expect_success:
            assert isinstance(result, ParseSuccess)
        else:
            assert (
                isinstance(result, ParseError) and result.code == ParseErrorCode.UNSUPPORTED_FORMAT
            )

    async def test_type_mismatch(self) -> None:
        factory = ParserFactory()
        result = await factory.parse(b"data", "notes.md", mime_type="application/pdf")
        assert isinstance(result, ParseError) and result.code == ParseErrorCode.TYPE_MISMATCH

    async def test_oversized_file(self) -> None:
        factory = ParserFactory()
        result = await factory.parse(b"x" * (51 * 1024 * 1024), "big.txt")
        assert isinstance(result, ParseError) and result.code == ParseErrorCode.OVERSIZED_FILE


class TestGetParser:
    def test_markdown(self) -> None:
        assert get_parser("file.md") is MarkdownParser
        assert get_parser("file.markdown") is MarkdownParser

    def test_txt(self) -> None:
        assert get_parser("file.txt") is TxtParser

    def test_pdf(self) -> None:
        assert get_parser("file.pdf") is PdfParser

    def test_unsupported(self) -> None:
        assert get_parser("file.docx") is None
        assert get_parser("file.html") is None
        assert get_parser("file") is None

    def test_mime_overrides_extension(self) -> None:
        assert get_parser("file.txt", mime_type="text/markdown") is MarkdownParser
