"""Tests for EmbeddingService (application/ingestion/embedding.py)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from uuid import UUID, uuid4

import pytest
from application.ingestion.embedding import (
    EmbeddingConfig,
    EmbeddingPipelineResult,
    EmbeddingService,
)
from domain.chunking import ChunkOutput
from domain.embedding import EmbeddingIdentity
from domain.models import (
    Chunk,
    Document,
    DocumentStatus,
    DocumentVersion,
)
from domain.repositories import (
    ChunkRepository,
    DocumentRepository,
    DocumentVersionRepository,
)

# ---------------------------------------------------------------------------
# Fake TextEmbedder (deterministic, matches fake ModelGateway pattern)
# ---------------------------------------------------------------------------


class _FakeEmbedder:
    """Deterministic fake embedder producing 768-dim vectors."""

    def __init__(self, dims: int = 768) -> None:
        self._dims = dims
        self.call_count = 0

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        self.call_count += 1
        vectors: list[tuple[float, ...]] = []
        for text in texts:
            seed = hashlib.sha256(text.encode()).digest()
            vec = tuple(round((seed[i % len(seed)] / 127.5) - 1, 6) for i in range(self._dims))
            vectors.append(vec)
        return tuple(vectors)


# ---------------------------------------------------------------------------
# In-memory fake repositories
# ---------------------------------------------------------------------------


@dataclass
class _FakeChunkRepo(ChunkRepository):
    _chunks: dict[UUID, Chunk] = field(default_factory=dict)
    _by_version: dict[UUID, list[UUID]] = field(default_factory=dict)

    async def create(self, chunk: Chunk) -> Chunk:
        self._chunks[chunk.id] = chunk
        self._by_version.setdefault(chunk.version_id, []).append(chunk.id)
        return chunk

    async def create_batch(self, chunks: list[Chunk]) -> list[Chunk]:
        for c in chunks:
            self._chunks[c.id] = c
            self._by_version.setdefault(c.version_id, []).append(c.id)
        return chunks

    async def get(self, chunk_id: UUID) -> Chunk | None:
        return self._chunks.get(chunk_id)

    async def get_by_version(self, version_id: UUID) -> list[Chunk]:
        ids = self._by_version.get(version_id, [])
        return [self._chunks[cid] for cid in sorted(ids)]

    async def delete_by_version(self, version_id: UUID) -> int:
        ids = self._by_version.pop(version_id, [])
        for cid in ids:
            self._chunks.pop(cid, None)
        return len(ids)


@dataclass
class _FakeVersionRepo(DocumentVersionRepository):
    _versions: dict[UUID, DocumentVersion] = field(default_factory=dict)

    async def create(self, version: DocumentVersion) -> DocumentVersion:
        self._versions[version.id] = version
        return version

    async def get(self, version_id: UUID) -> DocumentVersion | None:
        return self._versions.get(version_id)

    async def get_by_document(self, document_id: UUID) -> list[DocumentVersion]:
        return [v for v in self._versions.values() if v.document_id == document_id]

    async def get_latest(self, document_id: UUID) -> DocumentVersion | None:
        versions = [v for v in self._versions.values() if v.document_id == document_id]
        return max(versions, key=lambda v: v.created_at) if versions else None

    async def update(self, version: DocumentVersion) -> DocumentVersion:
        self._versions[version.id] = version
        return version


@dataclass
class _FakeDocumentRepo(DocumentRepository):
    _docs: dict[UUID, Document] = field(default_factory=dict)
    _by_source_key: dict[tuple[UUID, str], Document] = field(default_factory=dict)

    async def create(self, document: Document) -> Document:
        self._docs[document.id] = document
        self._by_source_key[(document.source_id, document.stable_key)] = document
        return document

    async def get(self, document_id: UUID) -> Document | None:
        return self._docs.get(document_id)

    async def get_by_source(self, source_id: UUID) -> list[Document]:
        return [d for d in self._docs.values() if d.source_id == source_id]

    async def get_by_stable_key(self, source_id: UUID, stable_key: str) -> Document | None:
        return self._by_source_key.get((source_id, stable_key))

    async def update(self, document: Document) -> Document:
        self._docs[document.id] = document
        key = (document.source_id, document.stable_key)
        self._by_source_key[key] = document
        return document

    async def delete(self, document_id: UUID) -> None:
        doc = self._docs.pop(document_id, None)
        if doc:
            self._by_source_key.pop((doc.source_id, doc.stable_key), None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def embedder() -> _FakeEmbedder:
    return _FakeEmbedder()


@pytest.fixture
def chunk_repo() -> _FakeChunkRepo:
    return _FakeChunkRepo()


@pytest.fixture
def version_repo() -> _FakeVersionRepo:
    return _FakeVersionRepo()


@pytest.fixture
def doc_repo() -> _FakeDocumentRepo:
    return _FakeDocumentRepo()


@pytest.fixture
def service(embedder, chunk_repo, version_repo, doc_repo) -> EmbeddingService:
    return EmbeddingService(
        text_embedder=embedder,
        chunk_repo=chunk_repo,
        version_repo=version_repo,
        document_repo=doc_repo,
    )


def _make_version(**overrides: object) -> DocumentVersion:
    kwargs: dict = dict(
        id=uuid4(),
        document_id=uuid4(),
        blob_hash="",
        content_hash="",
        status=DocumentStatus.PARSED,
    )
    kwargs.update(overrides)
    return DocumentVersion(**kwargs)


def _make_doc(**overrides: object) -> Document:
    kwargs: dict = dict(
        id=uuid4(),
        source_id=uuid4(),
        stable_key="test/doc",
    )
    kwargs.update(overrides)
    return Document(**kwargs)


def _make_chunk_outputs(count: int, base_hash: str = "h") -> tuple[ChunkOutput, ...]:
    return tuple(
        ChunkOutput(
            ordinal=i,
            text=f"Chunk {i} content. " + "x" * 50,
            chunk_hash=f"{base_hash}{i}",
            start_line=i * 10 + 1,
            end_line=(i + 1) * 10,
        )
        for i in range(count)
    )


# ===========================================================================
#  Tests
# ===========================================================================


class TestEmbeddingService:
    async def test_happy_path(self, service, chunk_repo, version_repo, doc_repo) -> None:
        doc = _make_doc()
        version = _make_version(document_id=doc.id)
        outputs = _make_chunk_outputs(3)

        result = await service.embed_and_publish(doc, version, outputs)

        assert isinstance(result, EmbeddingPipelineResult)
        assert result.chunk_count == 3
        assert result.version.status == DocumentStatus.PUBLISHED

        # Chunks were written
        saved = await chunk_repo.get_by_version(version.id)
        assert len(saved) == 3
        for chunk in saved:
            assert chunk.embedding is not None
            assert len(chunk.embedding) == 768

        # Document's current_version_id was updated
        updated_doc = await doc_repo.get(doc.id)
        assert updated_doc is not None
        assert updated_doc.current_version_id == version.id

        # Version status is PUBLISHED
        updated_version = await version_repo.get(version.id)
        assert updated_version is not None
        assert updated_version.status == DocumentStatus.PUBLISHED

    async def test_batch_processing(self, service, embedder) -> None:
        """Chunks exceeding batch_size are split across multiple embed calls."""
        doc = _make_doc()
        version = _make_version(document_id=doc.id)
        outputs = _make_chunk_outputs(10)

        cfg = EmbeddingConfig(batch_size=3)
        result = await service.embed_and_publish(doc, version, outputs, config=cfg)

        assert result.chunk_count == 10
        # With batch_size=3 and 10 chunks, we need 4 embed calls
        assert embedder.call_count == 4

    async def test_single_chunk(self, service, chunk_repo) -> None:
        doc = _make_doc()
        version = _make_version(document_id=doc.id)
        outputs = _make_chunk_outputs(1)

        result = await service.embed_and_publish(doc, version, outputs)
        assert result.chunk_count == 1

        saved = await chunk_repo.get_by_version(version.id)
        assert len(saved) == 1
        assert saved[0].embedding is not None

    async def test_empty_chunks_raises_validation_error(self, service, chunk_repo) -> None:
        doc = _make_doc()
        version = _make_version(document_id=doc.id)
        # All empty text
        outputs = (
            ChunkOutput(ordinal=0, text="", chunk_hash="empty1"),
            ChunkOutput(ordinal=1, text="   ", chunk_hash="empty2"),
            ChunkOutput(ordinal=2, text="Not empty", chunk_hash="ok1"),
        )

        cfg = EmbeddingConfig(max_empty_text_ratio=0.1)
        with pytest.raises(ValueError, match="Empty-text chunk ratio"):
            await service.embed_and_publish(doc, version, outputs, config=cfg)

        # Validation happens before candidate artifacts are written.
        saved = await chunk_repo.get_by_version(version.id)
        assert saved == []

    async def test_empty_text_allowed_with_high_threshold(self, service) -> None:
        doc = _make_doc()
        version = _make_version(document_id=doc.id)
        outputs = (
            ChunkOutput(ordinal=0, text="", chunk_hash="e1"),
            ChunkOutput(ordinal=1, text="valid", chunk_hash="v1"),
        )

        cfg = EmbeddingConfig(max_empty_text_ratio=0.6)  # 50% empty is OK
        result = await service.embed_and_publish(doc, version, outputs, config=cfg)
        assert result.chunk_count == 2

    async def test_zero_chunks(self, service, version_repo) -> None:
        doc = _make_doc()
        version = _make_version(document_id=doc.id)

        result = await service.embed_and_publish(doc, version, ())
        assert result.chunk_count == 0

        # Version should still be published
        updated = await version_repo.get(version.id)
        assert updated is not None
        assert updated.status == DocumentStatus.PUBLISHED

    async def test_idempotent_rerun(self, service, chunk_repo) -> None:
        """Re-running with same inputs overwrites previous chunks."""
        doc = _make_doc()
        version = _make_version(document_id=doc.id)
        outputs = _make_chunk_outputs(3)

        # First run
        r1 = await service.embed_and_publish(doc, version, outputs)
        assert r1.chunk_count == 3

        # Second run (same version, same outputs)
        r2 = await service.embed_and_publish(doc, version, outputs)
        assert r2.chunk_count == 3

        # Only 3 chunks exist (delete+reinsert was idempotent)
        saved = await chunk_repo.get_by_version(version.id)
        assert len(saved) == 3

    async def test_different_config(self, service) -> None:
        doc = _make_doc()
        version = _make_version(document_id=doc.id)
        outputs = _make_chunk_outputs(5)

        cfg = EmbeddingConfig(
            batch_size=10,
            embedding_identity=EmbeddingIdentity(model_revision="synthetic-model-r2"),
        )
        result = await service.embed_and_publish(doc, version, outputs, config=cfg)
        assert result.chunk_count == 5
        assert result.version.embedding_version == cfg.embedding_identity.version
        assert result.version.processing_config["embedding_model_revision"] == "synthetic-model-r2"

    async def test_l2_normalization_is_persisted(self, service, chunk_repo) -> None:
        doc = _make_doc()
        version = _make_version(document_id=doc.id)
        cfg = EmbeddingConfig(
            embedding_identity=EmbeddingIdentity(
                model_revision="synthetic-model-l2",
                normalization="l2",
            )
        )

        await service.embed_and_publish(doc, version, _make_chunk_outputs(1), config=cfg)

        saved = await chunk_repo.get_by_version(version.id)
        assert saved[0].embedding is not None
        norm = sum(value * value for value in saved[0].embedding) ** 0.5
        assert norm == pytest.approx(1.0)

    async def test_chunk_meta_preserved(self, service, chunk_repo) -> None:
        doc = _make_doc()
        version = _make_version(document_id=doc.id)
        outputs = (
            ChunkOutput(
                ordinal=0,
                text="Heading content",
                chunk_hash="h1",
                heading_path="Introduction",
                start_line=1,
                end_line=5,
                start_page=1,
                end_page=1,
                node_type="heading_section",
                parent_ordinal=0,
                prev_ordinal=0,
                next_ordinal=2,
            ),
        )

        await service.embed_and_publish(doc, version, outputs)
        saved = await chunk_repo.get_by_version(version.id)
        assert len(saved) == 1
        chunk = saved[0]
        assert chunk.meta.get("heading_path") == "Introduction"
        assert chunk.meta.get("start_line") == "1"
        assert chunk.meta.get("node_type") == "heading_section"
        assert chunk.meta.get("parent_ordinal") == "0"
        assert chunk.meta.get("prev_ordinal") == "0"
        assert chunk.meta.get("next_ordinal") == "2"
