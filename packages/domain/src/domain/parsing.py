"""Parsed document schema, parser error types, and Parser protocol.

Defines the pure-domain types that parsers must produce and consume,
with zero external dependencies.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

# ---------------------------------------------------------------------------
# Structural node types
# ---------------------------------------------------------------------------


class StructNodeType(StrEnum):
    """Kind of structural element in a parsed document."""

    DOCUMENT = "document"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    CODE_BLOCK = "code_block"
    LIST_ITEM = "list_item"
    QUOTE_BLOCK = "quote_block"
    THEMATIC_BREAK = "thematic_break"
    TABLE = "table"
    RAW_TEXT = "raw_text"  # fallback for unstructured content


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


class ParseErrorCode(StrEnum):
    """Canonical error codes for document parsing failures."""

    UNSUPPORTED_FORMAT = "unsupported_format"
    TYPE_MISMATCH = "type_mismatch"
    CONTENT_CORRUPT = "content_corrupt"
    ENCODING_FAILURE = "encoding_failure"
    EMPTY_DOCUMENT = "empty_document"
    OVERSIZED_FILE = "oversized_file"
    RESOURCE_EXCEEDED = "resource_exceeded"
    SCANNED_PDF = "scanned_pdf"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StructNode:
    """A single structural element within a parsed document.

    Nodes form a tree via *children*.  Leaf nodes such as paragraphs and
    code blocks carry their text directly; container nodes (DOCUMENT,
    QUOTE_BLOCK) carry text indirectly through children.
    """

    node_type: StructNodeType = StructNodeType.RAW_TEXT
    text: str = ""
    level: int = 0
    start_line: int = 0
    end_line: int = 0
    start_page: int | None = None
    end_page: int | None = None
    language: str | None = None
    children: tuple[StructNode, ...] = ()


@dataclass(frozen=True)
class ParseMetadata:
    """Metadata about the source file being parsed."""

    file_name: str = ""
    file_size: int = 0
    mime_type: str = ""
    encoding: str = "utf-8"


@dataclass(frozen=True)
class ParsedDocument:
    """A successfully parsed document with full structure and raw text.

    ``text`` holds the complete extracted text (the concatenation of all
    leaf-node text).  ``structure`` is the tree of structural nodes that
    map back to the original file via 1-based line numbers and optional
    page numbers.
    """

    metadata: ParseMetadata = field(default_factory=ParseMetadata)
    text: str = ""
    structure: tuple[StructNode, ...] = ()
    total_lines: int = 0


@dataclass(frozen=True)
class ParseError:
    """A structured parse failure with a canonical error code."""

    code: ParseErrorCode = ParseErrorCode.UNSUPPORTED_FORMAT
    message: str = ""
    details: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParseSuccess:
    """Wraps a successful parse result."""

    document: ParsedDocument


# Union type alias for result of a parse attempt.
ParseResult = ParseSuccess | ParseError


# ---------------------------------------------------------------------------
# Parser Protocol
# ---------------------------------------------------------------------------


class Parser(Protocol):
    """Protocol that every document parser adapter must satisfy.

    Implementations are stateless and thread-safe: all context is passed
    through ``parse()`` arguments.
    """

    async def parse(self, raw: bytes, metadata: ParseMetadata) -> ParseResult:
        """Parse *raw* file bytes into a ``ParsedDocument``.

        Parameters
        ----------
        raw:
            The exact file bytes as read from disk or uploaded.
        metadata:
            Source-level metadata (file name, declared MIME type, etc.).

        Returns
        -------
        ParseSuccess | ParseError
        """
        ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def compute_blob_hash(raw: bytes) -> str:
    """Return the lowercase hex SHA-256 of *raw* bytes."""
    return hashlib.sha256(raw).hexdigest()


def chunk_text_by_lines(text: str, max_lines: int = 512) -> list[str]:
    """Split *text* into chunks of at most *max_lines* lines.

    Used by parsers to produce line-separated paragraphs when the source
    format has no richer structure.
    """
    lines = text.splitlines(keepends=False)
    chunks: list[str] = []
    for i in range(0, len(lines), max_lines):
        chunk = "\n".join(lines[i : i + max_lines])
        if chunk:
            chunks.append(chunk)
    return chunks
