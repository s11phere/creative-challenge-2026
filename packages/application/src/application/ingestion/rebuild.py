"""Prepare isolated candidate versions for a controlled embedding rebuild."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from domain.embedding import EmbeddingIdentity, compute_processing_config_hash
from domain.models import (
    DocumentStatus,
    DocumentVersion,
    IngestionTask,
    TaskOperation,
    TaskStatus,
)
from domain.repositories import (
    DocumentRepository,
    DocumentVersionRepository,
    IngestionTaskRepository,
    SourceRepository,
)

_ACTIVE_TASK_STATUSES = {TaskStatus.QUEUED, TaskStatus.RUNNING}


@dataclass(frozen=True)
class RebuildPlanItem:
    document_id: UUID
    current_version_id: UUID
    embedding_version: str
    processing_config_hash: str


@dataclass(frozen=True)
class RebuildPreparation:
    candidates: tuple[DocumentVersion, ...] = ()
    tasks: tuple[IngestionTask, ...] = ()
    skipped_document_ids: tuple[UUID, ...] = ()


class EmbeddingRebuildService:
    """Create candidate versions and durable tasks without changing publication."""

    def __init__(
        self,
        source_repo: SourceRepository,
        document_repo: DocumentRepository,
        version_repo: DocumentVersionRepository,
        task_repo: IngestionTaskRepository,
    ) -> None:
        self._source_repo = source_repo
        self._document_repo = document_repo
        self._version_repo = version_repo
        self._task_repo = task_repo

    async def plan_source(
        self,
        source_id: UUID,
        identity: EmbeddingIdentity,
    ) -> tuple[RebuildPlanItem, ...]:
        source = await self._source_repo.get(source_id)
        if source is None:
            raise ValueError("Source not found")

        planned: list[RebuildPlanItem] = []
        for document in await self._document_repo.get_by_source(source_id):
            if document.deleted_at is not None or document.current_version_id is None:
                continue
            current = await self._version_repo.get(document.current_version_id)
            if current is None or current.document_id != document.id:
                raise ValueError("Published document version is missing or has invalid ownership")
            processing_config = {
                **current.processing_config,
                **identity.processing_config(),
            }
            planned.append(
                RebuildPlanItem(
                    document_id=document.id,
                    current_version_id=current.id,
                    embedding_version=identity.version,
                    processing_config_hash=compute_processing_config_hash(processing_config),
                )
            )
        return tuple(planned)

    async def prepare_source(
        self,
        source_id: UUID,
        identity: EmbeddingIdentity,
    ) -> RebuildPreparation:
        plan = await self.plan_source(source_id, identity)
        existing_tasks = await self._task_repo.get_by_source(source_id)
        candidates: list[DocumentVersion] = []
        tasks: list[IngestionTask] = []
        skipped: list[UUID] = []

        for item in plan:
            current = await self._version_repo.get(item.current_version_id)
            assert current is not None
            if (
                current.embedding_version == item.embedding_version
                and current.processing_config_hash == item.processing_config_hash
            ):
                skipped.append(item.document_id)
                continue

            candidate = await self._find_candidate(
                item.document_id,
                item.embedding_version,
                item.processing_config_hash,
            )
            if candidate is None:
                processing_config = {
                    **current.processing_config,
                    **identity.processing_config(),
                }
                candidate = await self._version_repo.create(
                    replace(
                        current,
                        id=uuid4(),
                        embedding_version=item.embedding_version,
                        processing_config_hash=item.processing_config_hash,
                        processing_config=processing_config,
                        status=DocumentStatus.PENDING,
                        created_at=datetime.now(UTC),
                    )
                )
            candidates.append(candidate)

            active_task = next(
                (
                    task
                    for task in existing_tasks
                    if task.operation is TaskOperation.REBUILD
                    and task.target_version_id == candidate.id
                    and task.status in _ACTIVE_TASK_STATUSES
                ),
                None,
            )
            if active_task is None:
                active_task = await self._task_repo.create(
                    IngestionTask(
                        source_id=source_id,
                        operation=TaskOperation.REBUILD,
                        target_version_id=candidate.id,
                        idempotency_key=f"rebuild:{candidate.id}:{uuid4().hex}",
                    )
                )
                existing_tasks.append(active_task)
            tasks.append(active_task)

        return RebuildPreparation(
            candidates=tuple(candidates),
            tasks=tuple(tasks),
            skipped_document_ids=tuple(skipped),
        )

    async def _find_candidate(
        self,
        document_id: UUID,
        embedding_version: str,
        processing_config_hash: str,
    ) -> DocumentVersion | None:
        for version in await self._version_repo.get_by_document(document_id):
            if (
                version.embedding_version == embedding_version
                and version.processing_config_hash == processing_config_hash
            ):
                return version
        return None
