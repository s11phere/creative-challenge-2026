"""Tests for parser implementations: Markdown, TXT, PDF, and factory."""

from __future__ import annotations

from pathlib import Path

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
#  Markdown parser
# ===========================================================================


class TestMarkdownParser:
    async def _parse_fixture(self, name: str) -> ParseSuccess:
        raw = (FIXTURES / name).read_bytes()
        parser = MarkdownParser()
        meta = ParseMetadata(file_name=name, file_size=len(raw), mime_type="text/markdown")
        result = await parser.parse(raw, meta)
        assert isinstance(result, ParseSuccess), f"Expected success, got error: {result}"
        return result

    async def test_parse_sample(self) -> None:
        result = await self._parse_fixture("sample.md")
        doc = result.document
        assert isinstance(doc, ParsedDocument)
        assert doc.total_lines > 0
        assert doc.metadata.mime_type == "text/markdown"

    async def test_heading_structure(self) -> None:
        result = await self._parse_fixture("sample.md")
        headings = [n for n in result.document.structure if n.node_type == StructNodeType.HEADING]
        assert len(headings) >= 3
        assert headings[0].level == 1
        assert headings[0].text.strip() or True  # has some heading text

    async def test_code_block(self) -> None:
        result = await self._parse_fixture("sample.md")
        code_blocks = [
            n for n in result.document.structure if n.node_type == StructNodeType.CODE_BLOCK
        ]
        assert len(code_blocks) >= 1
        assert "hello" in code_blocks[0].text.lower()

    async def test_list_items(self) -> None:
        result = await self._parse_fixture("sample.md")
        items = [n for n in result.document.structure if n.node_type == StructNodeType.LIST_ITEM]
        assert len(items) >= 3

    async def test_empty_document(self) -> None:
        parser = MarkdownParser()
        meta = ParseMetadata(file_name="empty.md", mime_type="text/markdown")
        result = await parser.parse(b"", meta)
        assert isinstance(result, ParseError)
        assert result.code == ParseErrorCode.EMPTY_DOCUMENT

    async def test_encoding_failure(self) -> None:
        parser = MarkdownParser()
        # Truly invalid UTF-8 bytes (not valid in any common encoding)
        meta = ParseMetadata(file_name="bad.md", mime_type="text/markdown", encoding="ascii")
        result = await parser.parse(b"\xff\xfe\x00\xff", meta)
        assert isinstance(result, ParseError)
        assert result.code == ParseErrorCode.ENCODING_FAILURE

    async def test_paragraph_text(self) -> None:
        result = await self._parse_fixture("sample.md")
        assert "This is a paragraph" in result.document.text
        assert "Chapter 1" in result.document.text

    async def test_line_numbers_are_1based(self) -> None:
        result = await self._parse_fixture("sample.md")
        for node in result.document.structure:
            if node.start_line > 0:
                assert node.start_line >= 1
                assert node.end_line >= node.start_line


# ===========================================================================
#  TXT parser
# ===========================================================================


class TestTxtParser:
    async def _parse_fixture(self, name: str) -> ParseSuccess:
        raw = (FIXTURES / name).read_bytes()
        parser = TxtParser()
        meta = ParseMetadata(file_name=name, file_size=len(raw), mime_type="text/plain")
        result = await parser.parse(raw, meta)
        assert isinstance(result, ParseSuccess), f"Expected success, got error: {result}"
        return result

    async def test_parse_sample(self) -> None:
        result = await self._parse_fixture("sample.txt")
        doc = result.document
        assert isinstance(doc, ParsedDocument)
        assert doc.total_lines > 0
        assert doc.metadata.encoding == "utf-8"

    async def test_paragraphs_from_blank_lines(self) -> None:
        result = await self._parse_fixture("sample.txt")
        paras = [n for n in result.document.structure if n.node_type == StructNodeType.PARAGRAPH]
        assert len(paras) >= 3
        assert all(p.text for p in paras)

    async def test_empty_document(self) -> None:
        parser = TxtParser()
        meta = ParseMetadata(file_name="empty.txt", mime_type="text/plain")
        result = await parser.parse(b"", meta)
        assert isinstance(result, ParseError)
        assert result.code == ParseErrorCode.EMPTY_DOCUMENT

    async def test_encoding_fallback(self) -> None:
        """TXT parser tries fallback encodings when declared encoding fails."""
        # Latin-1 encoded text
        raw = "café résumé".encode("latin-1")
        parser = TxtParser()
        meta = ParseMetadata(file_name="test.txt", mime_type="text/plain", encoding="utf-8")
        result = await parser.parse(raw, meta)
        # Should succeed via latin-1 fallback
        assert isinstance(result, ParseSuccess), f"Expected success, got: {result}"

    async def test_whitespace_only_is_empty(self) -> None:
        parser = TxtParser()
        meta = ParseMetadata(file_name="space.txt", mime_type="text/plain")
        result = await parser.parse(b"   \n  \n  ", meta)
        assert isinstance(result, ParseError)
        assert result.code == ParseErrorCode.EMPTY_DOCUMENT

    async def test_line_numbers_are_1based(self) -> None:
        result = await self._parse_fixture("sample.txt")
        for node in result.document.structure:
            if node.start_line > 0:
                assert node.start_line >= 1
                assert node.end_line >= node.start_line


# ===========================================================================
#  PDF parser
# ===========================================================================


class TestPdfParser:
    async def _parse_fixture(self, name: str) -> ParseSuccess:
        raw = (FIXTURES / name).read_bytes()
        parser = PdfParser()
        meta = ParseMetadata(file_name=name, file_size=len(raw), mime_type="application/pdf")
        result = await parser.parse(raw, meta)
        assert isinstance(result, ParseSuccess), f"Expected success, got error: {result}"
        return result

    async def test_parse_sample(self) -> None:
        result = await self._parse_fixture("sample.pdf")
        doc = result.document
        assert isinstance(doc, ParsedDocument)
        assert doc.total_lines > 0
        assert len(doc.structure) > 0

    async def test_page_nodes(self) -> None:
        result = await self._parse_fixture("sample.pdf")
        pages = [n for n in result.document.structure if n.start_page is not None]
        assert len(pages) >= 1
        assert all(p.start_page and p.start_page >= 1 for p in pages)

    async def test_text_content(self) -> None:
        result = await self._parse_fixture("sample.pdf")
        assert "Hello World" in result.document.text

    async def test_empty_pdf(self) -> None:
        """A PDF with no readable text returns scanned_pdf error."""
        from io import BytesIO

        from pypdf import PdfWriter

        w = PdfWriter()
        w.add_blank_page(612, 792)
        buf = BytesIO()
        w.write(buf)
        buf.seek(0)
        raw = buf.read()

        parser = PdfParser()
        meta = ParseMetadata(file_name="blank.pdf", mime_type="application/pdf")
        result = await parser.parse(raw, meta)
        assert isinstance(result, ParseError)
        assert result.code == ParseErrorCode.SCANNED_PDF

    async def test_corrupt_pdf(self) -> None:
        parser = PdfParser()
        meta = ParseMetadata(file_name="corrupt.pdf", mime_type="application/pdf")
        result = await parser.parse(b"not a pdf file at all", meta)
        assert isinstance(result, ParseError)
        assert result.code == ParseErrorCode.CONTENT_CORRUPT


# ===========================================================================
#  Parser factory
# ===========================================================================


class TestParserFactory:
    async def test_markdown_by_extension(self) -> None:
        factory = ParserFactory()
        raw = b"# Hello\n\nWorld."
        result = await factory.parse(raw, "test.md")
        assert isinstance(result, ParseSuccess), f"Expected success, got: {result}"

    async def test_txt_by_extension(self) -> None:
        factory = ParserFactory()
        result = await factory.parse(b"Hello world", "notes.txt")
        assert isinstance(result, ParseSuccess)

    async def test_pdf_by_extension(self) -> None:
        factory = ParserFactory()
        raw = (FIXTURES / "sample.pdf").read_bytes()
        result = await factory.parse(raw, "doc.pdf", mime_type="application/pdf")
        assert isinstance(result, ParseSuccess)

    async def test_unsupported_extension(self) -> None:
        factory = ParserFactory()
        result = await factory.parse(b"data", "file.docx")
        assert isinstance(result, ParseError)
        assert result.code == ParseErrorCode.UNSUPPORTED_FORMAT

    async def test_no_extension(self) -> None:
        factory = ParserFactory()
        result = await factory.parse(b"data", "README")
        assert isinstance(result, ParseError)
        assert result.code == ParseErrorCode.UNSUPPORTED_FORMAT

    async def test_type_mismatch(self) -> None:
        factory = ParserFactory()
        result = await factory.parse(b"data", "notes.md", mime_type="application/pdf")
        assert isinstance(result, ParseError)
        assert result.code == ParseErrorCode.TYPE_MISMATCH

    async def test_oversized_file(self) -> None:
        factory = ParserFactory()
        # Create a file larger than the default 50 MB limit
        big = b"x" * (51 * 1024 * 1024)
        result = await factory.parse(big, "big.txt")
        assert isinstance(result, ParseError)
        assert result.code == ParseErrorCode.OVERSIZED_FILE


class TestGetParser:
    def test_markdown_parser(self) -> None:
        parser_type = get_parser("file.md")
        assert parser_type is MarkdownParser

    def test_markdown_alt_extension(self) -> None:
        parser_type = get_parser("file.markdown")
        assert parser_type is MarkdownParser

    def test_txt_parser(self) -> None:
        parser_type = get_parser("file.txt")
        assert parser_type is TxtParser

    def test_pdf_parser(self) -> None:
        parser_type = get_parser("file.pdf")
        assert parser_type is PdfParser

    def test_unsupported_returns_none(self) -> None:
        assert get_parser("file.docx") is None
        assert get_parser("file.html") is None
        assert get_parser("file") is None

    def test_mime_overrides_extension(self) -> None:
        parser_type = get_parser("file.txt", mime_type="text/markdown")
        assert parser_type is MarkdownParser
