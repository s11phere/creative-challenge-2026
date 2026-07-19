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
    @pytest.mark.parametrize(
        ("kwargs", "checks"),
        [
            ({}, [("node_type", StructNodeType.RAW_TEXT), ("level", 0), ("text", "")]),
            (
                {
                    "node_type": StructNodeType.HEADING,
                    "text": "Intro",
                    "level": 1,
                    "start_line": 1,
                    "end_line": 1,
                },
                [("node_type", StructNodeType.HEADING), ("level", 1)],
            ),
            (
                {
                    "node_type": StructNodeType.CODE_BLOCK,
                    "text": "code",
                    "language": "python",
                    "start_line": 10,
                    "end_line": 12,
                },
                [("language", "python"), ("start_line", 10)],
            ),
            (
                {
                    "node_type": StructNodeType.DOCUMENT,
                    "children": (StructNode(node_type=StructNodeType.LIST_ITEM, text="item"),),
                },
                [("children", ...)],
            ),
        ],
    )
    def test_creation(self, kwargs, checks) -> None:
        node = StructNode(**kwargs)
        for attr, value in checks:
            if value is ...:
                assert len(getattr(node, attr)) == len(kwargs.get(attr, ()))
                continue
            assert getattr(node, attr) == value

    def test_frozen(self) -> None:
        with pytest.raises(AttributeError):
            StructNode().text = "changed"  # type: ignore[misc]


class TestParseMetadata:
    def test_custom_values(self) -> None:
        m = ParseMetadata(file_name="test.md", file_size=1024, mime_type="text/markdown")
        assert m.file_name == "test.md"
        assert m.file_size == 1024

    def test_frozen(self) -> None:
        with pytest.raises(AttributeError):
            ParseMetadata().file_name = "changed"  # type: ignore[misc]


class TestParsedDocument:
    def test_custom_document(self) -> None:
        meta = ParseMetadata(file_name="doc.md")
        node = StructNode(node_type=StructNodeType.HEADING, text="Title", start_line=1, end_line=1)
        doc = ParsedDocument(metadata=meta, text="Title\nContent", structure=(node,), total_lines=2)
        assert doc.metadata.file_name == "doc.md"
        assert doc.text == "Title\nContent"
        assert len(doc.structure) == 1

    def test_default_is_empty(self) -> None:
        doc = ParsedDocument()
        assert doc.text == ""
        assert doc.structure == ()


class TestParseResult:
    def test_success_is_not_error(self) -> None:
        assert not isinstance(ParseSuccess(document=ParsedDocument()), ParseError)

    def test_error_defaults(self) -> None:
        err = ParseError()
        assert err.code == ParseErrorCode.UNSUPPORTED_FORMAT

    @pytest.mark.parametrize(
        ("code", "msg"),
        [
            (ParseErrorCode.UNSUPPORTED_FORMAT, ".docx"),
            (ParseErrorCode.TYPE_MISMATCH, "MIME does not match"),
        ],
    )
    def test_error_codes(self, code: ParseErrorCode, msg: str) -> None:
        err = ParseError(code=code, message=msg, details={"ext": ".docx"})
        assert err.code == code
        assert msg in err.message

    def test_all_error_codes_unique(self) -> None:
        values = {e.value for e in ParseErrorCode}
        assert len(values) == len(ParseErrorCode)


class TestStructNodeType:
    def test_members(self) -> None:
        assert StructNodeType.DOCUMENT.value == "document"
        assert StructNodeType.HEADING.value == "heading"
        assert StructNodeType.PARAGRAPH.value == "paragraph"
        assert StructNodeType.CODE_BLOCK.value == "code_block"
        assert StructNodeType.RAW_TEXT.value == "raw_text"


class TestComputeBlobHash:
    def test_empty(self) -> None:
        assert compute_blob_hash(b"") == hashlib.sha256(b"").hexdigest()

    def test_known_value(self) -> None:
        assert compute_blob_hash(b"hello") == hashlib.sha256(b"hello").hexdigest()

    def test_different_inputs_differ(self) -> None:
        assert compute_blob_hash(b"a") != compute_blob_hash(b"b")


class TestChunkTextByLines:
    def test_small_text(self) -> None:
        assert chunk_text_by_lines("a\nb", max_lines=10) == ["a\nb"]

    def test_splits_at_max_lines(self) -> None:
        lines = "\n".join(f"l{i}" for i in range(10))
        assert len(chunk_text_by_lines(lines, max_lines=3)) == 4

    def test_empty(self) -> None:
        assert chunk_text_by_lines("") == []
