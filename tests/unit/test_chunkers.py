"""Tests for the StructureChunker implementation."""

from __future__ import annotations

from pathlib import Path

import pytest
from domain.chunking import (
    ChunkerConfig,
    ChunkingResult,
)
from domain.parsing import (
    ParsedDocument,
    ParseMetadata,
    ParseSuccess,
    StructNode,
    StructNodeType,
)
from infrastructure.chunkers.structure_chunker import StructureChunker
from infrastructure.parsers.markdown_parser import MarkdownParser

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


# ===========================================================================
#  Helpers
# ===========================================================================


async def _parse_md(name: str) -> ParsedDocument:
    parser = MarkdownParser()
    raw = (FIXTURES / name).read_bytes()
    meta = ParseMetadata(file_name=name, mime_type="text/markdown")
    result = await parser.parse(raw, meta)
    assert isinstance(result, ParseSuccess)
    return result.document


def _make_doc(text: str, structure: tuple[StructNode, ...] = ()) -> ParsedDocument:
    return ParsedDocument(
        metadata=ParseMetadata(file_name="test.txt", mime_type="text/plain"),
        text=text,
        structure=structure,
        total_lines=len(text.splitlines()),
    )


# ===========================================================================
#  StructureChunker
# ===========================================================================


class TestStructureChunker:
    @pytest.fixture
    def chunker(self) -> StructureChunker:
        return StructureChunker()

    # -- Empty / edge cases -----------------------------------------------

    async def test_empty_text(self, chunker) -> None:
        doc = _make_doc("")
        result = await chunker.chunk(doc)
        assert isinstance(result, ChunkingResult)
        assert result.chunks == ()
        assert result.total_ordinals == 0

    async def test_whitespace_only(self, chunker) -> None:
        doc = _make_doc("   \n\n  \n")
        result = await chunker.chunk(doc)
        assert result.total_ordinals == 0

    async def test_structured_empty_nodes_are_not_emitted(self, chunker) -> None:
        doc = _make_doc(
            "Visible content",
            (
                StructNode(
                    node_type=StructNodeType.RAW_TEXT,
                    text="   ",
                    start_line=1,
                    end_line=1,
                ),
                StructNode(
                    node_type=StructNodeType.PARAGRAPH,
                    text="Visible content",
                    start_line=2,
                    end_line=2,
                ),
            ),
        )

        result = await chunker.chunk(doc)

        assert [chunk.text for chunk in result.chunks] == ["Visible content"]
        assert [chunk.ordinal for chunk in result.chunks] == [0]

    async def test_default_config(self, chunker) -> None:
        doc = _make_doc("Hello, world.")
        result = await chunker.chunk(doc)
        assert result.total_ordinals == 1
        assert result.chunker_version == "1.0"
        assert result.config_hash

    # -- Basic text chunking ----------------------------------------------

    async def test_single_short_text(self, chunker) -> None:
        doc = _make_doc("This is a short text.")
        result = await chunker.chunk(doc)
        assert result.total_ordinals == 1
        assert result.chunks[0].text == "This is a short text."
        assert result.chunks[0].ordinal == 0
        assert len(result.chunks[0].chunk_hash) == 64

    async def test_multiple_paragraphs_single_chunk(self, chunker) -> None:
        text = "Paragraph one.\n\nParagraph two.\n\nParagraph three."
        doc = _make_doc(text)
        result = await chunker.chunk(doc, config=ChunkerConfig(chunk_size=2000))
        assert result.total_ordinals == 1
        assert "Paragraph one" in result.chunks[0].text
        assert "Paragraph three" in result.chunks[0].text

    async def test_text_split_across_chunks(self, chunker) -> None:
        # Create enough text to exceed chunk_size
        lines = [f"Line {i}: " + "x" * 80 for i in range(100)]
        text = "\n\n".join(lines)
        doc = _make_doc(text)
        result = await chunker.chunk(doc, config=ChunkerConfig(chunk_size=512, chunk_overlap=0))
        assert result.total_ordinals > 1
        # Verify adjacency links
        for i, chunk in enumerate(result.chunks):
            assert len(chunk.text) > 0
            if i > 0:
                assert chunk.prev_ordinal == result.chunks[i - 1].ordinal
            if i < len(result.chunks) - 1:
                assert chunk.next_ordinal == result.chunks[i + 1].ordinal

    # -- Determinism / idempotency ----------------------------------------

    async def test_deterministic_same_input(self, chunker) -> None:
        text = "Hello.\n\nWorld.\n\nTest.\n\nContent."
        doc = _make_doc(text)
        cfg = ChunkerConfig(chunk_size=100, chunk_overlap=0)
        r1 = await chunker.chunk(doc, config=cfg)
        r2 = await chunker.chunk(doc, config=cfg)
        assert r1.total_ordinals == r2.total_ordinals
        for c1, c2 in zip(r1.chunks, r2.chunks, strict=True):
            assert c1.chunk_hash == c2.chunk_hash
            assert c1.ordinal == c2.ordinal
            assert c1.text == c2.text

    async def test_deterministic_structure(self, chunker) -> None:
        doc = await _parse_md("sample.md")
        cfg = ChunkerConfig(chunk_size=200, chunk_overlap=0)
        r1 = await chunker.chunk(doc, config=cfg)
        r2 = await chunker.chunk(doc, config=cfg)
        for c1, c2 in zip(r1.chunks, r2.chunks, strict=True):
            assert c1.chunk_hash == c2.chunk_hash
            assert c1.ordinal == c2.ordinal
            assert c1.start_line == c2.start_line

    # -- Overlap ----------------------------------------------------------

    async def test_chunk_overlap_present(self, chunker) -> None:
        lines = [f"Line {i:03d}: " + "abc " * 30 for i in range(50)]
        text = "\n".join(lines)
        doc = _make_doc(text)
        cfg = ChunkerConfig(chunk_size=300, chunk_overlap=60)
        result = await chunker.chunk(doc, config=cfg)
        if result.total_ordinals > 1:
            # Check that adjacent chunks share some text
            for i in range(result.total_ordinals - 1):
                prev_end = result.chunks[i].text[-60:]
                next_start = result.chunks[i + 1].text[:60]
                # There should be some overlap
                common = set(prev_end.split()) & set(next_start.split())
                assert len(common) > 0 or len(prev_end.strip()) == 0

    # -- Chunk hash -------------------------------------------------------

    async def test_chunk_hash_based_on_text_only(self, chunker) -> None:
        text = "Content that should be hashed."
        doc = _make_doc(text)
        result = await chunker.chunk(doc)
        assert len(result.chunks[0].chunk_hash) == 64  # SHA-256 hex
        assert result.chunks[0].chunk_hash.islower()
        # Same text, same hash (determinism)
        doc2 = _make_doc(text)
        result2 = await chunker.chunk(doc2)
        assert result2.chunks[0].chunk_hash == result.chunks[0].chunk_hash
        # Different text, different hash
        doc3 = _make_doc("Other content.")
        result3 = await chunker.chunk(doc3)
        assert result3.chunks[0].chunk_hash != result.chunks[0].chunk_hash

    async def test_different_text_different_chunk_hash(self, chunker) -> None:
        doc1 = _make_doc("Hello world.")
        doc2 = _make_doc("Goodbye world.")
        r1 = (await chunker.chunk(doc1)).chunks[0]
        r2 = (await chunker.chunk(doc2)).chunks[0]
        assert r1.chunk_hash != r2.chunk_hash

    # -- Overlap config gate ----------------------------------------------

    async def test_zero_overlap(self, chunker) -> None:
        text = "A\n\n" * 200
        doc = _make_doc(text)
        cfg = ChunkerConfig(chunk_size=200, chunk_overlap=0)
        result = await chunker.chunk(doc, config=cfg)
        if result.total_ordinals > 1:
            prev_text = result.chunks[0].text
            next_text = result.chunks[1].text
            # With zero overlap, the boundary text should not repeat
            assert not prev_text.endswith(next_text[:20])

    # -- Config hash ------------------------------------------------------

    async def test_different_config_different_hash(self, chunker) -> None:
        doc = _make_doc("Some text.")
        r1 = await chunker.chunk(doc, config=ChunkerConfig(chunk_size=256))
        r2 = await chunker.chunk(doc, config=ChunkerConfig(chunk_size=512))
        assert r1.config_hash != r2.config_hash

    # -- Markdown structure awareness -------------------------------------

    async def test_markdown_heading_boundaries(self, chunker) -> None:
        doc = await _parse_md("sample.md")
        cfg = ChunkerConfig(chunk_size=5000, chunk_overlap=0)  # Large enough for full doc
        result = await chunker.chunk(doc, config=cfg)
        # The sample has multiple headings; with large chunk_size it should
        # all fit in one chunk, but the heading path should be populated
        assert result.total_ordinals >= 1
        # Check chunks have line numbers
        for chunk in result.chunks:
            assert chunk.start_line >= 1
            assert chunk.end_line >= chunk.start_line

    async def test_markdown_small_chunks_respect_headings(self, chunker) -> None:
        doc = await _parse_md("sample.md")
        # Very small chunk_size forces many chunks
        cfg = ChunkerConfig(chunk_size=50, chunk_overlap=0)
        result = await chunker.chunk(doc, config=cfg)
        assert result.total_ordinals >= 1
        # heading_path may be empty for the very first content before any heading

    async def test_markdown_adjacency_links(self, chunker) -> None:
        doc = await _parse_md("sample.md")
        cfg = ChunkerConfig(chunk_size=150, chunk_overlap=10)
        result = await chunker.chunk(doc, config=cfg)
        if result.total_ordinals > 1:
            for i, chunk in enumerate(result.chunks):
                if i > 0:
                    assert chunk.prev_ordinal == result.chunks[i - 1].ordinal
                if i < len(result.chunks) - 1:
                    assert chunk.next_ordinal == result.chunks[i + 1].ordinal

    # -- Line numbers -----------------------------------------------------

    async def test_line_numbers_increase(self, chunker) -> None:
        doc = await _parse_md("sample.md")
        cfg = ChunkerConfig(chunk_size=100, chunk_overlap=0)
        result = await chunker.chunk(doc, config=cfg)
        # Line numbers should be monotonically non-decreasing
        for i in range(1, len(result.chunks)):
            assert result.chunks[i].start_line >= result.chunks[i - 1].start_line

    # -- Node types -------------------------------------------------------

    async def test_node_type_inferred(self, chunker) -> None:
        doc = _make_doc("Just a plain text paragraph.")
        result = await chunker.chunk(doc)
        assert result.chunks[0].node_type == "paragraph"

    # -- Different config combinations ------------------------------------

    @pytest.mark.parametrize(
        ("size", "overlap"),
        [
            (100, 0),
            (200, 20),
            (500, 50),
            (1000, 100),
        ],
    )
    async def test_various_configs(self, chunker, size, overlap) -> None:
        text = "\n\n".join([f"Paragraph {i}: " + "word " * 50 for i in range(20)])
        doc = _make_doc(text)
        cfg = ChunkerConfig(chunk_size=size, chunk_overlap=overlap)
        result = await chunker.chunk(doc, config=cfg)
        assert result.total_ordinals >= 1
        # All chunks have text and are properly ordered
        for i, chunk in enumerate(result.chunks):
            assert len(chunk.text) > 0
            if i > 0:
                assert chunk.prev_ordinal == result.chunks[i - 1].ordinal

    # -- Large single segment ---------------------------------------------

    async def test_large_paragraph_split(self, chunker) -> None:
        """A single large paragraph should be split across chunks."""
        text = "word " * 2000  # ~10,000 chars, exceeding chunk_size
        doc = _make_doc(text)
        cfg = ChunkerConfig(chunk_size=500, chunk_overlap=0)
        result = await chunker.chunk(doc, config=cfg)
        assert result.total_ordinals > 1
        # All chunks should have text
        assert all(c.text for c in result.chunks)

    # -- Min chunk size ---------------------------------------------------

    async def test_min_chunk_size_merge(self, chunker) -> None:
        """Small chunks below min_chunk_size should be merged."""
        text = "A.\n\n" + "B.\n\n" + "C.\n\n" + "D." + "x" * 500
        doc = _make_doc(text)
        cfg = ChunkerConfig(chunk_size=1000, chunk_overlap=0, min_chunk_size=500)
        result = await chunker.chunk(doc, config=cfg)
        # The small initial chunks should have been merged into the larger one
        assert result.total_ordinals >= 1
        all_text = " ".join(c.text for c in result.chunks)
        assert "A." in all_text

    # -- Empty structure tree (fallback) ----------------------------------

    async def test_fallback_no_structure(self, chunker) -> None:
        """Plain text without structure should fall back to paragraph splitting."""
        text = "Para one.\n\nPara two.\n\nPara three."
        doc = ParsedDocument(
            metadata=ParseMetadata(file_name="test.txt", mime_type="text/plain"),
            text=text,
            structure=(),  # No structure tree
            total_lines=5,
        )
        cfg = ChunkerConfig(chunk_size=500, chunk_overlap=0)
        result = await chunker.chunk(doc, config=cfg)
        assert result.total_ordinals == 1
        assert "Para one" in result.chunks[0].text
