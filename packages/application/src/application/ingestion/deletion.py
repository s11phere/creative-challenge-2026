"""Document deletion use case shared by the API and ingestion worker."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from domain.models import Document, IngestionTask, TaskOperation, TaskStage, TaskStatus
from domain.repositories import (
    DocumentRepository,
    DocumentVersionRepository,
    IngestionTaskRepository,
)


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
