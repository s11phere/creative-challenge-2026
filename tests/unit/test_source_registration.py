"""Tests for SourceRegistrationService (application/ingestion/source_registration.py)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from application.ingestion.source_registration import (
    SourceRegistrationService,
)
from domain.embedding import EmbeddingIdentity, compute_processing_config_hash
from domain.fingerprinting import compute_storage_key
from domain.models import (
    Document,
    DocumentStatus,
    DocumentVersion,
    Source,
    SourceType,
)
from domain.parsing import compute_blob_hash
from domain.repositories import (
    DocumentRepository,
    DocumentVersionRepository,
    SourceRepository,
)

# ---------------------------------------------------------------------------
# In-memory fake repositories
# ---------------------------------------------------------------------------


@dataclass
class _FakeSourceRepo(SourceRepository):
    _sources: dict[UUID, Source] = field(default_factory=dict)

    async def create(self, source: Source) -> Source:
        self._sources[source.id] = source
        return source

    async def get(self, source_id: UUID) -> Source | None:
        return self._sources.get(source_id)

    async def get_by_space(self, space_id: UUID) -> list[Source]:
        return [s for s in self._sources.values() if s.space_id == space_id]

    async def update(self, source: Source) -> Source:
        self._sources[source.id] = source
        return source

    async def delete(self, source_id: UUID) -> None:
        self._sources.pop(source_id, None)


@dataclass
class _FakeDocumentRepo(DocumentRepository):
    _documents: dict[UUID, Document] = field(default_factory=dict)
    _by_source_key: dict[tuple[UUID, str], Document] = field(default_factory=dict)

    async def create(self, document: Document) -> Document:
        self._documents[document.id] = document
        self._by_source_key[(document.source_id, document.stable_key)] = document
        return document

    async def get(self, document_id: UUID) -> Document | None:
        return self._documents.get(document_id)

    async def get_by_source(self, source_id: UUID) -> list[Document]:
        return [d for d in self._documents.values() if d.source_id == source_id]

    async def get_by_stable_key(self, source_id: UUID, stable_key: str) -> Document | None:
        return self._by_source_key.get((source_id, stable_key))

    async def update(self, document: Document) -> Document:
        previous = self._documents.get(document.id)
        if previous is not None:
            self._by_source_key.pop((previous.source_id, previous.stable_key), None)
        self._documents[document.id] = document
        key = (document.source_id, document.stable_key)
        self._by_source_key[key] = document
        return document


@dataclass
class _FakeVersionRepo(DocumentVersionRepository):
    _versions: dict[UUID, DocumentVersion] = field(default_factory=dict)
    _by_document: dict[UUID, list[UUID]] = field(default_factory=dict)

    async def create(self, version: DocumentVersion) -> DocumentVersion:
        self._versions[version.id] = version
        self._by_document.setdefault(version.document_id, []).append(version.id)
        return version

    async def get(self, version_id: UUID) -> DocumentVersion | None:
        return self._versions.get(version_id)

    async def get_by_document(self, document_id: UUID) -> list[DocumentVersion]:
        vids = self._by_document.get(document_id, [])
        return [self._versions[vid] for vid in vids]

    async def get_latest(self, document_id: UUID) -> DocumentVersion | None:
        vids = self._by_document.get(document_id, [])
        if not vids:
            return None
        # latest by insertion order
        return self._versions[vids[-1]]

    async def update(self, version: DocumentVersion) -> DocumentVersion:
        self._versions[version.id] = version
        return version


# ---------------------------------------------------------------------------
# In-memory fake BlobStore
# ---------------------------------------------------------------------------


@dataclass
class _FakeBlobStore:
    _blobs: dict[str, bytes] = field(default_factory=dict)

    async def store(self, key: str, data: bytes) -> None:
        self._blobs[key] = data

    async def store_and_verify(self, key: str, data: bytes, expected_hash: str) -> None:
        actual = compute_blob_hash(data)  # type: ignore[attr-defined]
        if actual != expected_hash:
            raise ValueError(f"Hash mismatch: expected {expected_hash}, got {actual}")
        await self.store(key, data)

    async def retrieve(self, key: str) -> bytes | None:
        return self._blobs.get(key)

    async def delete(self, key: str) -> None:
        self._blobs.pop(key, None)

    async def exists(self, key: str) -> bool:
        return key in self._blobs


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def space_id() -> UUID:
    return uuid4()


@pytest.fixture
def source_repo() -> _FakeSourceRepo:
    return _FakeSourceRepo()


@pytest.fixture
def doc_repo() -> _FakeDocumentRepo:
    return _FakeDocumentRepo()


@pytest.fixture
def version_repo() -> _FakeVersionRepo:
    return _FakeVersionRepo()


@pytest.fixture
def blob_store() -> _FakeBlobStore:
    return _FakeBlobStore()


@pytest.fixture
def service(
    source_repo: _FakeSourceRepo,
    doc_repo: _FakeDocumentRepo,
    version_repo: _FakeVersionRepo,
) -> SourceRegistrationService:
    return SourceRegistrationService(
        source_repo=source_repo,
        document_repo=doc_repo,
        version_repo=version_repo,
    )


# ---------------------------------------------------------------------------
# create_source
# ---------------------------------------------------------------------------


class TestCreateSource:
    async def test_creates_new_source(
        self,
        service: SourceRegistrationService,
        space_id: UUID,
    ) -> None:
        result = await service.create_source(
            space_id, source_type=SourceType.UPLOAD, uri="test://file"
        )
        assert result.source.space_id == space_id
        assert result.source.source_type == SourceType.UPLOAD
        assert result.source.uri == "test://file"
        assert result.is_new is True

    async def test_returns_existing_source_by_id(
        self,
        service: SourceRegistrationService,
        source_repo: _FakeSourceRepo,
        space_id: UUID,
    ) -> None:
        existing = Source(space_id=space_id, source_type=SourceType.UPLOAD)
        await source_repo.create(existing)

        result = await service.create_source(space_id, source_id=existing.id)
        assert result.source.id == existing.id
        assert result.is_new is False

    async def test_raises_on_space_mismatch(
        self,
        service: SourceRegistrationService,
        source_repo: _FakeSourceRepo,
        space_id: UUID,
    ) -> None:
        other_space = uuid4()
        existing = Source(space_id=other_space, source_type=SourceType.UPLOAD)
        await source_repo.create(existing)

        with pytest.raises(ValueError, match="belongs to space"):
            await service.create_source(space_id, source_id=existing.id)

    async def test_source_type_default(
        self,
        service: SourceRegistrationService,
        space_id: UUID,
    ) -> None:
        result = await service.create_source(space_id)
        assert result.source.source_type == SourceType.UPLOAD


# ---------------------------------------------------------------------------
# register_file
# ---------------------------------------------------------------------------


class TestRegisterFile:
    async def test_registers_new_file(
        self,
        service: SourceRegistrationService,
        source_repo: _FakeSourceRepo,
        blob_store: _FakeBlobStore,
        space_id: UUID,
    ) -> None:
        source = Source(space_id=space_id, source_type=SourceType.UPLOAD)
        source = await source_repo.create(source)
        raw = b"# Hello\n\nThis is a test."

        result = await service.register_file(
            source,
            raw,
            blob_store,
            file_stable_key="upload/test.md",
        )

        assert result.source.id == source.id
        assert result.is_new_document is True
        assert result.blob_hash == compute_blob_hash(raw)
        assert result.storage_key == compute_storage_key(source.id, compute_blob_hash(raw))
        assert result.existing_version is None

        # Verify blob was stored
        assert await blob_store.exists(result.storage_key) is True
        stored = await blob_store.retrieve(result.storage_key)
        assert stored == raw

    async def test_dedup_by_stable_key(
        self,
        service: SourceRegistrationService,
        source_repo: _FakeSourceRepo,
        blob_store: _FakeBlobStore,
        space_id: UUID,
    ) -> None:
        source = Source(space_id=space_id, source_type=SourceType.UPLOAD)
        source = await source_repo.create(source)
        stable_key = "upload/test.md"

        # Register the same file twice
        raw = b"some content"
        first = await service.register_file(source, raw, blob_store, file_stable_key=stable_key)
        second = await service.register_file(source, raw, blob_store, file_stable_key=stable_key)

        # Same document should be reused
        assert first.document.id == second.document.id
        assert first.is_new_document is True
        assert second.is_new_document is False

    async def test_different_stable_key_creates_new_document(
        self,
        service: SourceRegistrationService,
        source_repo: _FakeSourceRepo,
        version_repo: _FakeVersionRepo,
        blob_store: _FakeBlobStore,
        space_id: UUID,
    ) -> None:
        source = Source(space_id=space_id, source_type=SourceType.UPLOAD)
        source = await source_repo.create(source)
        raw = b"same bytes"

        doc1 = await service.register_file(source, raw, blob_store, file_stable_key="file1.md")
        doc2 = await service.register_file(source, raw, blob_store, file_stable_key="file2.md")

        assert doc1.document.id != doc2.document.id
        assert doc1.version_id != doc2.version_id
        assert doc1.version_id is not None
        assert doc2.version_id is not None
        version1 = await version_repo.get(doc1.version_id)
        version2 = await version_repo.get(doc2.version_id)
        assert version1 is not None
        assert version2 is not None
        assert version1.document_id == doc1.document.id
        assert version2.document_id == doc2.document.id

    async def test_finds_existing_version_by_blob_hash(
        self,
        service: SourceRegistrationService,
        source_repo: _FakeSourceRepo,
        blob_store: _FakeBlobStore,
        space_id: UUID,
    ) -> None:
        source = Source(space_id=space_id, source_type=SourceType.UPLOAD)
        source = await source_repo.create(source)
        raw = b"some content"
        blob_hash = compute_blob_hash(raw)

        # Re-registering the same logical document finds its existing version.
        first = await service.register_file(source, raw, blob_store, file_stable_key="test.md")
        result = await service.register_file(source, raw, blob_store, file_stable_key="test.md")
        assert result.existing_version is not None
        assert result.existing_version.blob_hash == blob_hash
        assert result.existing_version.document_id == first.document.id
        assert result.version_id == first.version_id

    async def test_changed_processing_identity_creates_candidate_version(
        self,
        service: SourceRegistrationService,
        source_repo: _FakeSourceRepo,
        doc_repo: _FakeDocumentRepo,
        version_repo: _FakeVersionRepo,
        blob_store: _FakeBlobStore,
        space_id: UUID,
    ) -> None:
        source = await source_repo.create(Source(space_id=space_id, source_type=SourceType.UPLOAD))
        first = await service.register_file(
            source,
            b"same content",
            blob_store,
            file_stable_key="test.md",
        )
        old = await version_repo.get(first.version_id)
        assert old is not None
        await version_repo.update(replace(old, status=DocumentStatus.PUBLISHED))
        document = await doc_repo.get(first.document.id)
        assert document is not None
        await doc_repo.update(replace(document, current_version_id=old.id))

        identity = EmbeddingIdentity(model_revision="model@next", normalization="l2")
        processing_config = {
            "chunk_overlap": "64",
            "chunk_size": "512",
            "min_chunk_size": "100",
            "max_segment_size": "4096",
            **identity.processing_config(),
        }
        result = await service.register_file(
            source,
            b"same content",
            blob_store,
            file_stable_key="test.md",
            embedding_version=identity.version,
            processing_config=processing_config,
        )

        assert result.version_id != old.id
        unchanged_old = await version_repo.get(old.id)
        candidate = await version_repo.get(result.version_id)
        assert unchanged_old is not None
        assert unchanged_old.status is DocumentStatus.PUBLISHED
        assert unchanged_old.embedding_version == "1.0"
        assert candidate is not None
        assert candidate.status is DocumentStatus.PENDING
        assert candidate.embedding_version == identity.version
        assert candidate.processing_config_hash == compute_processing_config_hash(processing_config)

    async def test_uses_file_path_when_no_stable_key(
        self,
        service: SourceRegistrationService,
        source_repo: _FakeSourceRepo,
        blob_store: _FakeBlobStore,
        space_id: UUID,
    ) -> None:
        source = Source(space_id=space_id, source_type=SourceType.UPLOAD)
        source = await source_repo.create(source)
        raw = b"content"

        result = await service.register_file(source, raw, blob_store, file_path="/path/to/file.md")
        # The stable key should be derived from the path
        assert result.document.stable_key is not None

    async def test_preserves_unicode_filename_as_version_metadata(
        self,
        service: SourceRegistrationService,
        source_repo: _FakeSourceRepo,
        version_repo: _FakeVersionRepo,
        blob_store: _FakeBlobStore,
        space_id: UUID,
    ) -> None:
        source = await source_repo.create(Source(space_id=space_id, source_type=SourceType.UPLOAD))

        result = await service.register_file(
            source,
            b"content",
            blob_store,
            file_stable_key="测试文档.txt",
            file_path="测试文档.txt",
        )

        assert result.version_id is not None
        version = await version_repo.get(result.version_id)
        assert version is not None
        assert version.file_path == "测试文档.txt"
        assert result.document.stable_key == "测试文档.txt"

    async def test_upgrades_matching_legacy_unicode_key_without_new_document(
        self,
        service: SourceRegistrationService,
        source_repo: _FakeSourceRepo,
        doc_repo: _FakeDocumentRepo,
        version_repo: _FakeVersionRepo,
        blob_store: _FakeBlobStore,
        space_id: UUID,
    ) -> None:
        source = await source_repo.create(Source(space_id=space_id, source_type=SourceType.UPLOAD))
        legacy_document = await doc_repo.create(Document(source_id=source.id, stable_key=".txt"))
        raw = b"legacy content"
        legacy_version = await version_repo.create(
            DocumentVersion(
                document_id=legacy_document.id,
                blob_hash=compute_blob_hash(raw),
            )
        )

        result = await service.register_file(
            source,
            raw,
            blob_store,
            file_stable_key="中文资料.txt",
            file_path="中文资料.txt",
        )

        assert result.document.id == legacy_document.id
        assert result.document.stable_key == "中文资料.txt"
        assert result.version_id == legacy_version.id
        assert result.is_new_document is False
        assert await doc_repo.get_by_stable_key(source.id, ".txt") is None

    async def test_backfills_missing_filename_on_identical_reupload(
        self,
        service: SourceRegistrationService,
        source_repo: _FakeSourceRepo,
        version_repo: _FakeVersionRepo,
        blob_store: _FakeBlobStore,
        space_id: UUID,
    ) -> None:
        source = await source_repo.create(Source(space_id=space_id, source_type=SourceType.UPLOAD))
        first = await service.register_file(
            source,
            b"same content",
            blob_store,
            file_stable_key=".txt",
        )

        second = await service.register_file(
            source,
            b"same content",
            blob_store,
            file_stable_key=".txt",
            file_path="中文资料.txt",
        )

        assert second.version_id == first.version_id
        assert second.existing_version is not None
        assert second.existing_version.file_path == "中文资料.txt"
        assert len(await version_repo.get_by_document(first.document.id)) == 1

    async def test_reactivates_deleted_document_on_reupload(
        self,
        service: SourceRegistrationService,
        source_repo: _FakeSourceRepo,
        doc_repo: _FakeDocumentRepo,
        blob_store: _FakeBlobStore,
        space_id: UUID,
    ) -> None:
        source = await source_repo.create(Source(space_id=space_id, source_type=SourceType.UPLOAD))
        deleted_document = await doc_repo.create(
            Document(
                source_id=source.id,
                stable_key="deleted.md",
                current_version_id=uuid4(),
                deleted_at=datetime.now(UTC),
            )
        )

        result = await service.register_file(
            source,
            b"restored content",
            blob_store,
            file_stable_key="deleted.md",
        )

        restored = await doc_repo.get(deleted_document.id)
        assert restored is not None
        assert result.document.id == deleted_document.id
        assert restored.deleted_at is None
        assert restored.current_version_id is None

    async def test_blob_hash_integrity(
        self,
        service: SourceRegistrationService,
        source_repo: _FakeSourceRepo,
        blob_store: _FakeBlobStore,
        space_id: UUID,
    ) -> None:
        source = Source(space_id=space_id, source_type=SourceType.UPLOAD)
        source = await source_repo.create(source)
        raw = b"content"

        result = await service.register_file(source, raw, blob_store, file_stable_key="test.md")
        assert result.blob_hash == compute_blob_hash(raw)

        # Verify stored blob hash matches
        stored = await blob_store.retrieve(result.storage_key)
        assert stored is not None
        assert compute_blob_hash(stored) == result.blob_hash

    async def test_different_bytes_different_blob_hash(
        self,
        service: SourceRegistrationService,
        source_repo: _FakeSourceRepo,
        blob_store: _FakeBlobStore,
        space_id: UUID,
    ) -> None:
        source = Source(space_id=space_id, source_type=SourceType.UPLOAD)
        source = await source_repo.create(source)

        r1 = await service.register_file(source, b"version a", blob_store, file_stable_key="f.md")
        r2 = await service.register_file(source, b"version b", blob_store, file_stable_key="f.md")

        assert r1.blob_hash != r2.blob_hash
        # Same stable key but different content → same document, new version needed
        assert r1.document.id == r2.document.id
