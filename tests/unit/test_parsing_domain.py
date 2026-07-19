"""Tests for domain parsing types: ParsedDocument, StructNode, ParseError, etc."""

from __future__ import annotations

import hashlib

import pytest
from domain.parsing import (
    ParsedDocument,
    ParseError,
    ParseErrorCode,
    ParseMetadata,
    ParseSuccess,
    StructNode,
    StructNodeType,
    chunk_text_by_lines,
    compute_blob_hash,
)


class TestStructNode:
    def test_default_creation(self) -> None:
        node = StructNode()
        assert node.node_type == StructNodeType.RAW_TEXT
        assert node.text == ""
        assert node.level == 0
        assert node.start_line == 0
        assert node.end_line == 0
        assert node.start_page is None
        assert node.end_page is None
        assert node.language is None
        assert node.children == ()

    def test_heading_node(self) -> None:
        node = StructNode(
            node_type=StructNodeType.HEADING,
            text="Introduction",
            level=1,
            start_line=1,
            end_line=1,
        )
        assert node.node_type == StructNodeType.HEADING
        assert node.text == "Introduction"
        assert node.level == 1
        assert node.start_line == 1
        assert node.end_line == 1

    def test_code_block_with_language(self) -> None:
        node = StructNode(
            node_type=StructNodeType.CODE_BLOCK,
            text="print('hello')",
            language="python",
            start_line=10,
            end_line=12,
        )
        assert node.language == "python"
        assert node.start_line == 10
        assert node.end_line == 12

    def test_node_with_children(self) -> None:
        child = StructNode(node_type=StructNodeType.LIST_ITEM, text="item")
        parent = StructNode(
            node_type=StructNodeType.DOCUMENT,
            children=(child,),
        )
        assert len(parent.children) == 1
        assert parent.children[0].text == "item"

    def test_is_frozen(self) -> None:
        node = StructNode()
        with pytest.raises(AttributeError):
            node.text = "changed"  # type: ignore[misc]


class TestParseMetadata:
    def test_default_creation(self) -> None:
        meta = ParseMetadata()
        assert meta.file_name == ""
        assert meta.file_size == 0
        assert meta.mime_type == ""
        assert meta.encoding == "utf-8"

    def test_custom_values(self) -> None:
        meta = ParseMetadata(
            file_name="test.md",
            file_size=1024,
            mime_type="text/markdown",
            encoding="utf-8",
        )
        assert meta.file_name == "test.md"
        assert meta.file_size == 1024

    def test_is_frozen(self) -> None:
        meta = ParseMetadata()
        with pytest.raises(AttributeError):
            meta.file_name = "changed"  # type: ignore[misc]


class TestParsedDocument:
    def test_default_creation(self) -> None:
        doc = ParsedDocument()
        assert isinstance(doc.metadata, ParseMetadata)
        assert doc.text == ""
        assert doc.structure == ()
        assert doc.total_lines == 0

    def test_custom_document(self) -> None:
        meta = ParseMetadata(file_name="doc.md")
        node = StructNode(
            node_type=StructNodeType.HEADING,
            text="Title",
            start_line=1,
            end_line=1,
        )
        doc = ParsedDocument(
            metadata=meta,
            text="Title\n\nContent",
            structure=(node,),
            total_lines=3,
        )
        assert doc.metadata.file_name == "doc.md"
        assert doc.text == "Title\n\nContent"
        assert len(doc.structure) == 1


class TestParseSuccess:
    def test_wraps_document(self) -> None:
        doc = ParsedDocument()
        result = ParseSuccess(document=doc)
        assert result.document is doc

    def test_is_not_error(self) -> None:
        result = ParseSuccess(document=ParsedDocument())
        assert not isinstance(result, ParseError)


class TestParseError:
    def test_unsupported_format(self) -> None:
        err = ParseError(
            code=ParseErrorCode.UNSUPPORTED_FORMAT,
            message="Unsupported file format: .docx",
            details={"extension": ".docx"},
        )
        assert err.code == ParseErrorCode.UNSUPPORTED_FORMAT
        assert "Unsupported" in err.message
        assert err.details["extension"] == ".docx"

    def test_type_mismatch(self) -> None:
        err = ParseError(
            code=ParseErrorCode.TYPE_MISMATCH,
            message="MIME does not match extension",
            details={
                "extension": ".md",
                "declared_mime": "application/pdf",
            },
        )
        assert err.code == ParseErrorCode.TYPE_MISMATCH

    def test_all_error_codes_have_unique_values(self) -> None:
        codes = {e.value for e in ParseErrorCode}
        assert len(codes) == len(ParseErrorCode)

    def test_default_creation(self) -> None:
        err = ParseError()
        assert err.code == ParseErrorCode.UNSUPPORTED_FORMAT
        assert err.message == ""
        assert err.details == {}


class TestStructNodeType:
    def test_members(self) -> None:
        assert StructNodeType.DOCUMENT.value == "document"
        assert StructNodeType.HEADING.value == "heading"
        assert StructNodeType.PARAGRAPH.value == "paragraph"
        assert StructNodeType.CODE_BLOCK.value == "code_block"
        assert StructNodeType.LIST_ITEM.value == "list_item"
        assert StructNodeType.QUOTE_BLOCK.value == "quote_block"
        assert StructNodeType.THEMATIC_BREAK.value == "thematic_break"
        assert StructNodeType.TABLE.value == "table"
        assert StructNodeType.RAW_TEXT.value == "raw_text"


class TestComputeBlobHash:
    def test_empty_bytes(self) -> None:
        result = compute_blob_hash(b"")
        assert result == hashlib.sha256(b"").hexdigest()

    def test_known_value(self) -> None:
        result = compute_blob_hash(b"hello")
        expected = hashlib.sha256(b"hello").hexdigest()
        assert result == expected
        assert result.islower()

    def test_different_inputs_differ(self) -> None:
        h1 = compute_blob_hash(b"content a")
        h2 = compute_blob_hash(b"content b")
        assert h1 != h2


class TestChunkTextByLines:
    def test_small_text(self) -> None:
        result = chunk_text_by_lines("line1\nline2", max_lines=10)
        assert len(result) == 1
        assert result[0] == "line1\nline2"

    def test_splits_at_max_lines(self) -> None:
        lines = "\n".join(f"line{i}" for i in range(10))
        result = chunk_text_by_lines(lines, max_lines=3)
        assert len(result) == 4
        assert result[0] == "line0\nline1\nline2"

    def test_empty_text(self) -> None:
        assert chunk_text_by_lines("") == []

    def test_single_line(self) -> None:
        assert chunk_text_by_lines("only") == ["only"]
