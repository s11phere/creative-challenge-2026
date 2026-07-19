"""Tests for domain chunking types: ChunkerConfig, ChunkOutput, ChunkingResult, etc."""

from __future__ import annotations

import pytest
from domain.chunking import (
    ChunkerConfig,
    ChunkingResult,
    ChunkOutput,
    compute_chunk_hash,
    compute_chunker_config_hash,
)


class TestComputeChunkHash:
    def test_deterministic(self) -> None:
        text = "Hello, world!"
        assert compute_chunk_hash(text) == compute_chunk_hash(text)

    def test_different_text_different_hash(self) -> None:
        assert compute_chunk_hash("abc") != compute_chunk_hash("xyz")

    def test_empty_string(self) -> None:
        h = compute_chunk_hash("")
        assert isinstance(h, str)
        assert len(h) == 64  # SHA-256 hex length

    def test_utf8_preserved(self) -> None:
        zh = compute_chunk_hash("你好世界")
        assert len(zh) == 64


class TestComputeChunkerConfigHash:
    def test_deterministic(self) -> None:
        cfg = ChunkerConfig(chunk_size=512, chunk_overlap=64, min_chunk_size=100)
        assert compute_chunker_config_hash(cfg) == compute_chunker_config_hash(cfg)

    def test_different_size_different_hash(self) -> None:
        cfg1 = ChunkerConfig(chunk_size=256)
        cfg2 = ChunkerConfig(chunk_size=512)
        assert compute_chunker_config_hash(cfg1) != compute_chunker_config_hash(cfg2)

    def test_different_overlap_different_hash(self) -> None:
        cfg1 = ChunkerConfig(chunk_overlap=0)
        cfg2 = ChunkerConfig(chunk_overlap=64)
        assert compute_chunker_config_hash(cfg1) != compute_chunker_config_hash(cfg2)


class TestChunkerConfig:
    def test_defaults(self) -> None:
        cfg = ChunkerConfig()
        assert cfg.chunk_size == 512
        assert cfg.chunk_overlap == 64
        assert cfg.min_chunk_size == 100

    def test_custom_values(self) -> None:
        cfg = ChunkerConfig(chunk_size=1024, chunk_overlap=128, min_chunk_size=200)
        assert cfg.chunk_size == 1024
        assert cfg.chunk_overlap == 128
        assert cfg.min_chunk_size == 200

    def test_frozen(self) -> None:
        cfg = ChunkerConfig()
        with pytest.raises(AttributeError):  # noqa: PT012
            cfg.chunk_size = 999  # type: ignore[misc]


class TestChunkOutput:
    def test_defaults(self) -> None:
        co = ChunkOutput(ordinal=0, text="test", chunk_hash="abc123")
        assert co.ordinal == 0
        assert co.text == "test"
        assert co.chunk_hash == "abc123"
        assert co.heading_path == ""
        assert co.start_line == 0
        assert co.end_line == 0
        assert co.start_page is None
        assert co.end_page is None
        assert co.parent_ordinal is None
        assert co.prev_ordinal is None
        assert co.next_ordinal is None
        assert co.node_type == "text"

    def test_full_construction(self) -> None:
        co = ChunkOutput(
            ordinal=1,
            text="Some content",
            chunk_hash="def456",
            heading_path="Introduction.Background",
            start_line=10,
            end_line=20,
            start_page=1,
            end_page=1,
            parent_ordinal=0,
            prev_ordinal=0,
            next_ordinal=2,
            node_type="paragraph",
        )
        assert co.ordinal == 1
        assert co.heading_path == "Introduction.Background"
        assert co.parent_ordinal == 0
        assert co.next_ordinal == 2

    def test_frozen(self) -> None:
        co = ChunkOutput(ordinal=0, text="t", chunk_hash="h")
        with pytest.raises(AttributeError):  # noqa: PT012
            co.text = "changed"  # type: ignore[misc]


class TestChunkingResult:
    def test_defaults(self) -> None:
        result = ChunkingResult()
        assert result.chunks == ()
        assert result.chunker_version == "1.0"
        assert result.config_hash == ""
        assert result.total_ordinals == 0

    def test_with_chunks(self) -> None:
        chunks = (
            ChunkOutput(ordinal=0, text="First", chunk_hash="h1"),
            ChunkOutput(ordinal=1, text="Second", chunk_hash="h2"),
        )
        result = ChunkingResult(
            chunks=chunks,
            chunker_version="1.0",
            config_hash="cfg_hash",
            total_ordinals=2,
        )
        assert len(result.chunks) == 2
        assert result.total_ordinals == 2
        assert result.config_hash == "cfg_hash"

    def test_frozen(self) -> None:
        result = ChunkingResult()
        with pytest.raises(AttributeError):  # noqa: PT012
            result.total_ordinals = 10  # type: ignore[misc]
