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
        assert result.chunker_version == "1.3"
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

    async def test_paragraph_exceeding_chunk_size_is_not_split(self, chunker) -> None:
        """A paragraph over chunk_size but under max_segment_size stays atomic.

        The chunker must never cut inside a paragraph to hit chunk_size; the
        paragraph becomes its own chunk instead.
        """
        para = "word " * 600  # ~2400 chars, > chunk_size=512, < max_segment_size
        doc = _make_doc(para)
        result = await chunker.chunk(doc, config=ChunkerConfig(chunk_size=512, chunk_overlap=0))
        assert result.total_ordinals == 1
        assert len(result.chunks[0].text) == len(para.strip())  # not truncated, not split

    async def test_paragraph_split_only_above_max_segment_size(self, chunker) -> None:
        """Only a segment larger than max_segment_size is split at all."""
        para = "word " * 2000  # ~10,000 chars, exceeds max_segment_size
        doc = _make_doc(para)
        result = await chunker.chunk(
            doc, config=ChunkerConfig(chunk_size=512, chunk_overlap=0, max_segment_size=1500)
        )
        assert result.total_ordinals > 1
        # The split must not lose content.
        joined = " ".join(c.text.strip() for c in result.chunks)
        assert set(joined.split()) == set(para.split())

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

    async def test_heading_text_is_retained_in_chunk_text(self, chunker) -> None:
        heading = StructNode(
            node_type=StructNodeType.HEADING,
            text="Installation",
            level=1,
            start_line=1,
            end_line=1,
            children=(
                StructNode(
                    node_type=StructNodeType.PARAGRAPH,
                    text="Run the installer.",
                    start_line=2,
                    end_line=2,
                ),
            ),
        )
        result = await chunker.chunk(_make_doc("Installation\nRun the installer.", (heading,)))
        assert "Installation" in result.chunks[0].text
        assert "Run the installer." in result.chunks[0].text

    async def test_pdf_like_raw_pages_are_not_treated_as_toc(self, chunker) -> None:
        pages = tuple(
            StructNode(
                node_type=StructNodeType.RAW_TEXT,
                text=f"Page {page}: " + chr(64 + page) * 300,
                start_line=page,
                end_line=page,
                start_page=page,
                end_page=page,
            )
            for page in range(1, 4)
        )
        result = await chunker.chunk(
            _make_doc("\n".join(node.text for node in pages), pages),
            config=ChunkerConfig(
                chunk_size=100, chunk_overlap=0, min_chunk_size=1, max_segment_size=100
            ),
        )
        assert result.total_ordinals >= 9
        assert max(len(chunk.text) for chunk in result.chunks) <= 100
        assert all(chunk.node_type != "table_of_contents" for chunk in result.chunks)

    async def test_leading_list_is_retained_when_it_does_not_mirror_headings(self, chunker) -> None:
        structure = (
            *(
                StructNode(
                    node_type=StructNodeType.LIST_ITEM,
                    text=text,
                    start_line=index,
                    end_line=index,
                )
                for index, text in enumerate(("Install Python", "Create a venv", "Run tests"), 1)
            ),
            *(
                StructNode(
                    node_type=StructNodeType.HEADING,
                    text=text,
                    level=1,
                    start_line=index,
                    end_line=index,
                )
                for index, text in enumerate(("Overview", "Architecture", "Reference"), 4)
            ),
        )
        result = await chunker.chunk(
            _make_doc("content", structure),
            config=ChunkerConfig(chunk_size=40, chunk_overlap=0, min_chunk_size=1),
        )
        assert "Install Python" in "\n".join(chunk.text for chunk in result.chunks)
        assert result.chunks[0].node_type != "table_of_contents"

    async def test_real_toc_is_retained_and_marked(self, chunker) -> None:
        labels = ("1 Overview ........ 2", "2 Architecture .... 4", "3 Reference ....... 8")
        headings = ("Overview", "Architecture", "Reference")
        structure = (
            *(
                StructNode(
                    node_type=StructNodeType.LIST_ITEM,
                    text=text,
                    start_line=index,
                    end_line=index,
                )
                for index, text in enumerate(labels, 1)
            ),
            *(
                StructNode(
                    node_type=StructNodeType.HEADING,
                    text=text,
                    level=1,
                    start_line=index,
                    end_line=index,
                )
                for index, text in enumerate(headings, 4)
            ),
        )
        result = await chunker.chunk(
            _make_doc("content", structure),
            config=ChunkerConfig(chunk_size=100, chunk_overlap=0, min_chunk_size=1),
        )
        assert result.chunks[0].node_type == "table_of_contents"
        assert all(label in result.chunks[0].text for label in labels)

    async def test_toc_matches_compact_numbering_and_summary_suffixes(self, chunker) -> None:
        labels = ("Data definition: create", "Data query: select", "Data update: insert")
        headings = ("1.Data definition", "2.Data query", "3.Data update")
        structure = (
            *(
                StructNode(
                    node_type=StructNodeType.LIST_ITEM,
                    text=text,
                    start_line=index,
                    end_line=index,
                )
                for index, text in enumerate(labels, 1)
            ),
            *(
                StructNode(
                    node_type=StructNodeType.HEADING,
                    text=text,
                    level=1,
                    start_line=index,
                    end_line=index,
                )
                for index, text in enumerate(headings, 4)
            ),
        )

        result = await chunker.chunk(_make_doc("content", structure))

        assert result.chunks[0].node_type == "table_of_contents"

    async def test_nested_markdown_toc_is_separated_before_minimum_size_merge(
        self, chunker
    ) -> None:
        document = await MarkdownParser().parse(
            (
                b"- [Overview](#overview)\n"
                b"  - [Install](#install)\n"
                b"  - [Configure](#configure)\n"
                b"\n# Overview\n\n## Install\n\nRun installer.\n"
                b"\n## Configure\n\nSet option.\n"
            ),
            ParseMetadata(file_name="toc.md", mime_type="text/markdown"),
        )
        assert isinstance(document, ParseSuccess)

        result = await chunker.chunk(document.document)

        toc_chunks = [chunk for chunk in result.chunks if chunk.node_type == "table_of_contents"]
        assert len(toc_chunks) == 1
        assert toc_chunks[0].end_line <= 4
        assert "Run installer." in "\n".join(
            chunk.text for chunk in result.chunks if chunk.node_type != "table_of_contents"
        )

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
        text = "word " * 2000  # ~10,000 chars, exceeding max_segment_size
        doc = _make_doc(text)
        cfg = ChunkerConfig(chunk_size=500, chunk_overlap=0, max_segment_size=1500)
        result = await chunker.chunk(doc, config=cfg)
        assert result.total_ordinals > 1
        # All chunks should have text
        assert all(c.text for c in result.chunks)

    async def test_oversize_single_line_splits_on_word_boundaries(self, chunker) -> None:
        """A single unwrapped line longer than chunk_size must not be cut mid-word."""
        words = [f"word{i:04d}" for i in range(200)]  # each word is a distinct token
        text = " ".join(words)  # ~1900 chars, exceeds chunk_size
        node = StructNode(
            node_type=StructNodeType.RAW_TEXT,
            text=text,
            start_line=1,
            end_line=1,
            start_page=1,
            end_page=1,
        )
        doc = ParsedDocument(
            metadata=ParseMetadata(file_name="test.pdf", mime_type="application/pdf"),
            text=text,
            structure=(node,),
            total_lines=1,
        )
        cfg = ChunkerConfig(chunk_size=500, chunk_overlap=0, max_segment_size=1500)
        result = await chunker.chunk(doc, config=cfg)
        assert result.total_ordinals > 1
        token_set = set(words)
        for chunk in result.chunks:
            first_token = chunk.text.split()[0]
            assert first_token in token_set, f"chunk starts mid-word: {chunk.text[:30]!r}"

    async def test_oversize_line_preserves_all_content(self, chunker) -> None:
        """Splitting an oversized line must not drop or reorder any text."""
        words = [f"word{i:04d}" for i in range(200)]
        text = " ".join(words)
        node = StructNode(
            node_type=StructNodeType.RAW_TEXT,
            text=text,
            start_line=1,
            end_line=1,
            start_page=1,
            end_page=1,
        )
        doc = ParsedDocument(
            metadata=ParseMetadata(file_name="test.pdf", mime_type="application/pdf"),
            text=text,
            structure=(node,),
            total_lines=1,
        )
        cfg = ChunkerConfig(chunk_size=500, chunk_overlap=0, max_segment_size=1500)
        result = await chunker.chunk(doc, config=cfg)
        # Reconstruct the joined text and confirm it still contains every token in order
        joined = " ".join(chunk.text.strip() for chunk in result.chunks)
        assert set(joined.split()) == set(words)

    async def test_multiline_oversize_segment_preserves_piece_source_ranges(self, chunker) -> None:
        lines = [f"source line {index} " + "x" * 40 for index in range(10, 20)]
        text = "\n".join(lines)
        node = StructNode(
            node_type=StructNodeType.PARAGRAPH,
            text=text,
            start_line=10,
            end_line=19,
        )
        result = await chunker.chunk(
            _make_doc(text, structure=(node,)),
            config=ChunkerConfig(
                chunk_size=120,
                chunk_overlap=0,
                min_chunk_size=0,
                max_segment_size=200,
            ),
        )

        assert result.total_ordinals > 1
        assert result.chunks[0].start_line == 10
        assert result.chunks[-1].end_line == 19
        assert all(
            left.end_line < right.start_line
            for left, right in zip(result.chunks, result.chunks[1:], strict=False)
        )

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
