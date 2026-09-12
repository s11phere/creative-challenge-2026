"""Unit tests for Space deletion blob collection and purge ordering.

The database cascade removes every Space row, but raw uploads live in the
BlobStore.  These tests pin the collection rules (dedupe, tombstoned
documents) and the best-effort purge contract without needing PostgreSQL.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from application.ingestion import SpaceDeletionService
from domain.fingerprinting import compute_storage_key
from domain.models import Document, DocumentVersion, Source, SourceType


class _FakeSourceRepository:
    def __init__(self, sources: list[Source]) -> None:
        self._sources = sources

    async def get_by_space(self, space_id: UUID) -> list[Source]:
        return [source for source in self._sources if source.space_id == space_id]


class _FakeDocumentRepository:
    def __init__(self, documents: list[Document]) -> None:
        self._documents = documents

    async def get_by_source(self, source_id: UUID) -> list[Document]:
        return [document for document in self._documents if document.source_id == source_id]


class _FakeVersionRepository:
    def __init__(self, versions: list[DocumentVersion]) -> None:
        self._versions = versions

    async def get_by_document(self, document_id: UUID) -> list[DocumentVersion]:
        return [version for version in self._versions if version.document_id == document_id]


class _RecordingBlobStore:
    def __init__(self, *, failing: frozenset[str] = frozenset()) -> None:
        self.deleted: list[str] = []
        self._failing = set(failing)

    def fail(self, key: str) -> None:
        self._failing.add(key)

    async def delete(self, key: str) -> None:
        if key in self._failing:
            raise OSError("disk on fire")
        self.deleted.append(key)


def _build(
    *, blob_hashes: list[str], failing: frozenset[str] = frozenset()
) -> tuple[SpaceDeletionService, _RecordingBlobStore, UUID, UUID]:
    source = Source(source_type=SourceType.UPLOAD, uri="notes.md")
    space_id = source.space_id
    documents: list[Document] = []
    versions: list[DocumentVersion] = []
    for index, blob_hash in enumerate(blob_hashes):
        document = Document(source_id=source.id, stable_key=f"notes-{index}.md")
        documents.append(document)
        if blob_hash:
            versions.append(DocumentVersion(document_id=document.id, blob_hash=blob_hash))
    # A tombstoned document must still contribute its blob: its cleanup task may
    # never have run.
    tombstoned = Document(
        source_id=source.id,
        stable_key="removed.md",
        deleted_at=datetime.now(UTC),
    )
    documents.append(tombstoned)
    versions.append(DocumentVersion(document_id=tombstoned.id, blob_hash="deadbeef"))

    store = _RecordingBlobStore(failing=failing)
    service = SpaceDeletionService(
        source_repo=_FakeSourceRepository([source]),
        document_repo=_FakeDocumentRepository(documents),
        version_repo=_FakeVersionRepository(versions),
        blob_store=store,
    )
    return service, store, space_id, source.id


@pytest.mark.asyncio
async def test_collect_blob_keys_dedupes_shared_blobs_and_keeps_scope() -> None:
    service, _store, space_id, source_id = _build(blob_hashes=["aaaa", "bbbb", "aaaa"])

    keys = await service.collect_blob_keys(space_id)

    assert keys == (
        compute_storage_key(source_id, "aaaa"),
        compute_storage_key(source_id, "bbbb"),
        compute_storage_key(source_id, "deadbeef"),
    )


@pytest.mark.asyncio
async def test_collect_blob_keys_ignores_other_spaces() -> None:
    service, _store, _space_id, _source_id = _build(blob_hashes=["aaaa"])

    assert await service.collect_blob_keys(UUID(int=999)) == ()


@pytest.mark.asyncio
async def test_collect_blob_keys_skips_versions_without_blob_hash() -> None:
    service, _store, space_id, _source_id = _build(blob_hashes=["", "aaaa"])

    keys = await service.collect_blob_keys(space_id)

    assert len(keys) == 2  # the empty hash is skipped, "deadbeef" still collected


@pytest.mark.asyncio
async def test_purge_blobs_is_best_effort_and_keeps_going() -> None:
    service, store, space_id, source_id = _build(blob_hashes=["aaaa", "bbbb"])
    failing_key = compute_storage_key(source_id, "aaaa")
    store.fail(failing_key)
    keys = await service.collect_blob_keys(space_id)

    processed = await service.purge_blobs(keys)

    assert processed == len(keys) - 1
    assert store.deleted == [key for key in keys if key != failing_key]


@pytest.mark.asyncio
async def test_purge_blobs_is_idempotent_for_missing_keys() -> None:
    service, store, _space_id, _source_id = _build(blob_hashes=["aaaa"])

    await service.purge_blobs(())
    first = await service.purge_blobs(await service.collect_blob_keys(UUID(int=1)))

    assert first == 0
    assert store.deleted == []
