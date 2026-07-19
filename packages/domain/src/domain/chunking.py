"""Chunker port, chunk output schema, and related types.

Defines the pure-domain types that chunkers must produce and consume,
with zero external dependencies.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from domain.parsing import ParsedDocument

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkerConfig:
    """Configuration for the chunking process.

    These values typically come from a ``RetrievalProfile`` but are passed
    explicitly so the chunker remains a pure function of its inputs.
    """

    chunk_size: int = 512
    """Target chunk size in characters (approximate proxy for tokens)."""

    chunk_overlap: int = 64
    """Number of characters of overlap between adjacent chunks."""

    min_chunk_size: int = 100
    """Minimum chunk size in characters.

    Chunks below this threshold are merged into the preceding chunk
    rather than standing alone.
    """


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ChunkOutput:
    """A single chunk produced by the chunking process.

    This is the **output schema** of the chunker — it is not the same as
    the ``Chunk`` ORM entity (which adds DB-level fields such as ``id``,
    ``version_id``, and ``embedding``).
    """

    ordinal: int
    """Zero-based position of this chunk within the document version."""

    text: str
    """The chunk text (normalized, ready for embedding)."""

    chunk_hash: str
    """SHA-256 of ``text`` (the embedding input).

    This hash is content-derived and does **not** include ``version_id``,
    ``ordinal``, or any other context — identical text always produces the
    same hash, enabling cross-version embedding reuse.
    """

    heading_path: str = ""
    """Dot-separated chain of headings leading to this chunk, e.g.
    ``"Introduction.Background"``.  Empty string if no heading context."""

    start_line: int = 0
    """1-based start line in the original document."""

    end_line: int = 0
    """1-based end line (inclusive) in the original document."""

    start_page: int | None = None
    """1-based start page, or ``None`` when unknown."""

    end_page: int | None = None
    """1-based end page (inclusive), or ``None`` when unknown."""

    parent_ordinal: int | None = None
    """Ordinal of the parent chunk (e.g., a heading-only chunk that this
    content belongs to).  ``None`` for top-level chunks."""

    prev_ordinal: int | None = None
    """Ordinal of the immediately preceding chunk in document order."""

    next_ordinal: int | None = None
    """Ordinal of the immediately following chunk in document order."""

    node_type: str = "text"
    """Primary content type of the chunk: ``heading_section``,
    ``paragraph``, ``code_block``, ``list``, ``table``, ``continuation``."""


@dataclass(frozen=True)
class ChunkingResult:
    """The complete result of chunking one document."""

    chunks: tuple[ChunkOutput, ...] = ()
    chunker_version: str = "1.0"
    config_hash: str = ""
    total_ordinals: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def compute_chunk_hash(text: str) -> str:
    """Return the SHA-256 hex digest of *text* (the embedding input).

    The hash is purely content-derived — identical texts always produce
    the same hash, supporting cross-version embedding reuse.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_chunker_config_hash(config: ChunkerConfig) -> str:
    """Return a canonical SHA-256 hash of the chunker configuration.

    Two chunker runs with the same config hash are guaranteed to produce
    the same output for the same input text.
    """
    raw = f"{config.chunk_size}:{config.chunk_overlap}:{config.min_chunk_size}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Chunker Protocol
# ---------------------------------------------------------------------------


class Chunker(Protocol):
    """Protocol that every chunker adapter must satisfy.

    Implementations are stateless: all context is passed through
    ``chunk()`` arguments.
    """

    async def chunk(
        self,
        document: ParsedDocument,
        *,
        config: ChunkerConfig | None = None,
    ) -> ChunkingResult:
        """Chunk a parsed document into a sequence of ``ChunkOutput``.

        Parameters
        ----------
        document:
            A successfully parsed document with extracted text and
            structural node tree.  The chunker uses the structure to
            place chunk boundaries at semantic breakpoints (headings,
            code blocks, page breaks).
        config:
            Chunking parameters.  ``None`` means use defaults.

        Returns
        -------
        ChunkingResult
            Ordered sequence of chunks with positioning metadata,
            adjacency links, and content-based ``chunk_hash`` values.
        """
        ...
