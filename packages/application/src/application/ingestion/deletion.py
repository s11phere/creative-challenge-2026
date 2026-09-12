"""Document deletion use case shared by the API and ingestion worker."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import UUID, uuid4

from domain.blob_store import BlobStore
from domain.fingerprinting import compute_storage_key
from domain.models import Document, IngestionTask, TaskOperation, TaskStage, TaskStatus
from domain.repositories import (
    DocumentRepository,
    DocumentVersionRepository,
    IngestionTaskRepository,
    SourceRepository,
)

logger = logging.getLogger(__name__)


class DocumentDeletionService:
    """Tombstone a document and schedule cleanup of its derived artifacts."""

    def __init__(
        self,
        document_repo: DocumentRepository,
        version_repo: DocumentVersionRepository,
        task_repo: IngestionTaskRepository,
    ) -> None:
        self._document_repo = document_repo
        self._version_repo = version_repo
        self._task_repo = task_repo

    async def delete_document(
        self,
        document: Document,
        *,
        idempotency_key: str | None = None,
    ) -> IngestionTask | None:
        """Unpublish *document* and create an idempotent cleanup task."""
        if document.deleted_at is not None:
            return None

        cleanup_version_id = document.current_version_id
        if cleanup_version_id is None:
            latest_version = await self._version_repo.get_latest(document.id)
            cleanup_version_id = latest_version.id if latest_version is not None else None

        now = datetime.now(UTC)
        await self._document_repo.update(
            Document(
                id=document.id,
                source_id=document.source_id,
                stable_key=document.stable_key,
                current_version_id=None,
                deleted_at=now,
                created_at=document.created_at,
                updated_at=now,
            )
        )
        return await self._task_repo.create(
            IngestionTask(
                source_id=document.source_id,
                operation=TaskOperation.DELETE,
                status=TaskStatus.QUEUED,
                stage=TaskStage.DISCOVER,
                target_version_id=cleanup_version_id,
                idempotency_key=idempotency_key or uuid4().hex,
            )
        )


class SpaceDeletionService:
    """Delete a Space and purge the raw blobs that only it referenced.

    The database cascades Space deletion to sources, documents, versions,
    chunks, conversations, runs and citations.  Raw uploads live in the
    BlobStore instead, so they must be collected **before** the rows disappear
    and purged **after** the surrounding transaction commits: purging first
    would destroy bytes that a rolled-back transaction still references.
    """

    def __init__(
        self,
        *,
        source_repo: SourceRepository,
        document_repo: DocumentRepository,
        version_repo: DocumentVersionRepository,
        blob_store: BlobStore,
    ) -> None:
        self._source_repo = source_repo
        self._document_repo = document_repo
        self._version_repo = version_repo
        self._blob_store = blob_store

    async def collect_blob_keys(self, space_id: UUID) -> tuple[str, ...]:
        """Return every distinct blob key reachable from *space_id*.

        Tombstoned documents are included on purpose: their cleanup task may
        have failed, and ``BlobStore.delete`` is idempotent, so deleting a Space
        must not leave already-orphaned bytes behind.
        """

        keys: list[str] = []
        for source in await self._source_repo.get_by_space(space_id):
            for document in await self._document_repo.get_by_source(source.id):
                for version in await self._version_repo.get_by_document(document.id):
                    if version.blob_hash:
                        keys.append(compute_storage_key(source.id, version.blob_hash))
        return tuple(dict.fromkeys(keys))

    async def purge_blobs(self, keys: Iterable[str]) -> int:
        """Best-effort removal of *keys*; returns how many were processed.

        A missing or already-removed file is not an error.  Filesystem failures
        are logged and skipped so a partially purged Space cannot fail the
        caller's already-committed delete.
        """

        processed = 0
        for key in keys:
            try:
                await self._blob_store.delete(key)
            except OSError:
                logger.warning("space_blob_purge_failed", extra={"blob_key": key})
                continue
            processed += 1
        return processed
