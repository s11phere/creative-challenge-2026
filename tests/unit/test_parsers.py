"""Tests for parser implementations: Markdown, TXT, PDF, and factory."""

from __future__ import annotations

from pathlib import Path

import pytest
from domain.parsing import (
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

    async def test_nested_list_does_not_consume_following_paragraph(self, parser) -> None:
        raw = b"- parent\n  - child\n\nafter list\n"
        result = await parser.parse(
            raw,
            ParseMetadata(file_name="nested.md", mime_type="text/markdown"),
        )

        assert isinstance(result, ParseSuccess)
        assert any(
            node.node_type is StructNodeType.PARAGRAPH
            and node.text == "after list"
            and node.start_line == 4
            for node in result.document.structure
        )

    async def test_blockquote_preserves_text_and_source_range(self, parser) -> None:
        result = await parser.parse(
            b"> quoted line\n> second line\n",
            ParseMetadata(file_name="quote.md", mime_type="text/markdown"),
        )

        assert isinstance(result, ParseSuccess)
        quotes = [
            node
            for node in result.document.structure
            if node.node_type is StructNodeType.QUOTE_BLOCK
        ]
        assert len(quotes) == 1
        assert quotes[0].text == "quoted line second line"
        assert (quotes[0].start_line, quotes[0].end_line) == (1, 2)


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
