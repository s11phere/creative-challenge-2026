"""SQLAlchemy implementations of the domain repository protocols."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from domain.models import (
    Chunk,
    Document,
    DocumentStatus,
    DocumentVersion,
    IngestionTask,
    RetrievalProfile,
    Source,
    SourceType,
    Space,
    TaskOperation,
    TaskStage,
    TaskStatus,
)
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .orm import (
    ChunkModel,
    DocumentModel,
    DocumentVersionModel,
    IngestionTaskModel,
    SourceModel,
    SpaceModel,
)

# ---------------------------------------------------------------------------
# Mapper helpers
# ---------------------------------------------------------------------------


def _space_to_domain(row: SpaceModel) -> Space:
    profile_dict: dict[str, Any] = row.retrieval_profile or {}
    return Space(
        id=row.id,
        name=row.name,
        owner_id=row.owner_id,
        retrieval_profile=RetrievalProfile(
            chunk_size=profile_dict.get("chunk_size", 512),
            chunk_overlap=profile_dict.get("chunk_overlap", 64),
            top_k=profile_dict.get("top_k", 5),
            rerank_k=profile_dict.get("rerank_k", 10),
            fusion_alpha=profile_dict.get("fusion_alpha", 0.35),
            extra=profile_dict.get("extra", RetrievalProfile().extra),
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _space_from_domain(space: Space) -> SpaceModel:
    return SpaceModel(
        id=space.id,
        name=space.name,
        owner_id=space.owner_id,
        retrieval_profile={
            "chunk_size": space.retrieval_profile.chunk_size,
            "chunk_overlap": space.retrieval_profile.chunk_overlap,
            "top_k": space.retrieval_profile.top_k,
            "rerank_k": space.retrieval_profile.rerank_k,
            "fusion_alpha": space.retrieval_profile.fusion_alpha,
            "extra": space.retrieval_profile.extra,
        },
        created_at=space.created_at,
        updated_at=space.updated_at,
    )


def _source_to_domain(row: SourceModel) -> Source:
    return Source(
        id=row.id,
        space_id=row.space_id,
        source_type=SourceType(row.source_type),
        uri=row.uri,
        name=row.name,
        sync_cursor=row.sync_cursor,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _source_from_domain(source: Source) -> SourceModel:
    return SourceModel(
        id=source.id,
        space_id=source.space_id,
        source_type=source.source_type.value,
        uri=source.uri,
        name=source.name,
        sync_cursor=source.sync_cursor,
        created_at=source.created_at,
        updated_at=source.updated_at,
    )


def _document_to_domain(row: DocumentModel) -> Document:
    return Document(
        id=row.id,
        source_id=row.source_id,
        stable_key=row.stable_key,
        current_version_id=row.current_version_id,
        deleted_at=row.deleted_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _document_from_domain(doc: Document) -> DocumentModel:
    return DocumentModel(
        id=doc.id,
        source_id=doc.source_id,
        stable_key=doc.stable_key,
        current_version_id=doc.current_version_id,
        deleted_at=doc.deleted_at,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


def _version_to_domain(row: DocumentVersionModel) -> DocumentVersion:
    return DocumentVersion(
        id=row.id,
        document_id=row.document_id,
        blob_hash=row.blob_hash,
        content_hash=row.content_hash,
        parser_version=row.parser_version,
        normalizer_version=row.normalizer_version,
        chunker_version=row.chunker_version,
        embedding_version=row.embedding_version,
        processing_config_hash=row.processing_config_hash,
        processing_config=dict(row.processing_config or {}),
        status=DocumentStatus(row.status),
        file_path=row.file_path,
        created_at=row.created_at,
    )


def _version_from_domain(version: DocumentVersion) -> DocumentVersionModel:
    return DocumentVersionModel(
        id=version.id,
        document_id=version.document_id,
        blob_hash=version.blob_hash,
        content_hash=version.content_hash,
        parser_version=version.parser_version,
        normalizer_version=version.normalizer_version,
        chunker_version=version.chunker_version,
        embedding_version=version.embedding_version,
        processing_config_hash=version.processing_config_hash,
        processing_config=dict(version.processing_config),
        status=version.status.value,
        file_path=version.file_path,
        created_at=version.created_at,
    )


def _chunk_to_domain(row: ChunkModel) -> Chunk:
    embedding: list[float] | None = None
    if row.embedding is not None:
        embedding = list(row.embedding)
    return Chunk(
        id=row.id,
        version_id=row.version_id,
        ordinal=row.ordinal,
        chunk_hash=row.chunk_hash,
        text=row.text,
        meta=dict(row.meta or {}),
        embedding=embedding,
        created_at=row.created_at,
    )


def _chunk_from_domain(chunk: Chunk) -> ChunkModel:
    return ChunkModel(
        id=chunk.id,
        version_id=chunk.version_id,
        ordinal=chunk.ordinal,
        chunk_hash=chunk.chunk_hash,
        text=chunk.text,
        meta=dict(chunk.meta),
        embedding=chunk.embedding,
        created_at=chunk.created_at,
    )


def _task_to_domain(row: IngestionTaskModel) -> IngestionTask:
    return IngestionTask(
        id=row.id,
        source_id=row.source_id,
        operation=TaskOperation(row.operation),
        status=TaskStatus(row.status),
        stage=TaskStage(row.stage),
        target_version_id=row.target_version_id,
        idempotency_key=row.idempotency_key,
        progress=row.progress,
        retry_count=row.retry_count,
        max_retries=row.max_retries,
        cancel_requested_at=row.cancel_requested_at,
        enqueued_at=row.enqueued_at,
        heartbeat_at=row.heartbeat_at,
        lease_expires_at=row.lease_expires_at,
        error_code=row.error_code,
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _task_from_domain(task: IngestionTask) -> IngestionTaskModel:
    return IngestionTaskModel(
        id=task.id,
        source_id=task.source_id,
        operation=task.operation.value,
        status=task.status.value,
        stage=task.stage.value,
        target_version_id=task.target_version_id,
        idempotency_key=task.idempotency_key,
        progress=task.progress,
        retry_count=task.retry_count,
        max_retries=task.max_retries,
        cancel_requested_at=task.cancel_requested_at,
        enqueued_at=task.enqueued_at,
        heartbeat_at=task.heartbeat_at,
        lease_expires_at=task.lease_expires_at,
        error_code=task.error_code,
        error=task.error,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------


class SpaceRepository:
    """Postgres-backed space repository."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, space: Space) -> Space:
        model = _space_from_domain(space)
        self._session.add(model)
        await self._session.flush()
        return _space_to_domain(model)

    async def get(self, space_id: uuid.UUID) -> Space | None:
        result = await self._session.get(SpaceModel, space_id)
        return _space_to_domain(result) if result else None

    async def list(self) -> list[Space]:
        result = await self._session.execute(select(SpaceModel))
        return [_space_to_domain(row) for row in result.scalars()]

    async def update(self, space: Space) -> Space:
        values: dict[str, Any] = {
            "name": space.name,
            "owner_id": space.owner_id,
            "retrieval_profile": {
                "chunk_size": space.retrieval_profile.chunk_size,
                "chunk_overlap": space.retrieval_profile.chunk_overlap,
                "top_k": space.retrieval_profile.top_k,
                "rerank_k": space.retrieval_profile.rerank_k,
                "fusion_alpha": space.retrieval_profile.fusion_alpha,
                "extra": space.retrieval_profile.extra,
            },
            "updated_at": datetime.now(UTC),
        }
        await self._session.execute(
            update(SpaceModel).where(SpaceModel.id == space.id).values(**values)
        )
        await self._session.flush()
        result = await self._session.get(SpaceModel, space.id)
        assert result is not None
        return _space_to_domain(result)

    async def delete(self, space_id: uuid.UUID) -> None:
        await self._session.execute(delete(SpaceModel).where(SpaceModel.id == space_id))
        await self._session.flush()


class SourceRepository:
    """Postgres-backed source repository."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, source: Source) -> Source:
        model = _source_from_domain(source)
        self._session.add(model)
        await self._session.flush()
        return _source_to_domain(model)

    async def get(self, source_id: uuid.UUID) -> Source | None:
        result = await self._session.get(SourceModel, source_id)
        return _source_to_domain(result) if result else None

    async def get_by_space(self, space_id: uuid.UUID) -> list[Source]:
        result = await self._session.execute(
            select(SourceModel).where(SourceModel.space_id == space_id)
        )
        return [_source_to_domain(row) for row in result.scalars()]

    async def update(self, source: Source) -> Source:
        values: dict[str, Any] = {
            "source_type": source.source_type.value,
            "uri": source.uri,
            "name": source.name,
            "sync_cursor": source.sync_cursor,
            "updated_at": datetime.now(UTC),
        }
        await self._session.execute(
            update(SourceModel).where(SourceModel.id == source.id).values(**values)
        )
        await self._session.flush()
        result = await self._session.get(SourceModel, source.id)
        assert result is not None
        return _source_to_domain(result)

    async def delete(self, source_id: uuid.UUID) -> None:
        await self._session.execute(delete(SourceModel).where(SourceModel.id == source_id))
        await self._session.flush()


class DocumentRepository:
    """Postgres-backed document repository."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, document: Document) -> Document:
        model = _document_from_domain(document)
        self._session.add(model)
        await self._session.flush()
        return _document_to_domain(model)

    async def get(self, document_id: uuid.UUID) -> Document | None:
        result = await self._session.get(DocumentModel, document_id)
        return _document_to_domain(result) if result else None

    async def get_by_source(self, source_id: uuid.UUID) -> list[Document]:
        result = await self._session.execute(
            select(DocumentModel).where(DocumentModel.source_id == source_id)
        )
        return [_document_to_domain(row) for row in result.scalars()]

    async def get_by_sources(self, source_ids: list[uuid.UUID]) -> list[Document]:
        """Batch-fetch documents across sources (avoids N+1 in list endpoints)."""
        if not source_ids:
            return []
        result = await self._session.execute(
            select(DocumentModel).where(DocumentModel.source_id.in_(source_ids))
        )
        return [_document_to_domain(row) for row in result.scalars()]

    async def get_by_stable_key(self, source_id: uuid.UUID, stable_key: str) -> Document | None:
        result = await self._session.execute(
            select(DocumentModel).where(
                DocumentModel.source_id == source_id,
                DocumentModel.stable_key == stable_key,
            )
        )
        row = result.scalar_one_or_none()
        return _document_to_domain(row) if row else None

    async def update(self, document: Document) -> Document:
        values: dict[str, Any] = {
            "stable_key": document.stable_key,
            "current_version_id": document.current_version_id,
            "deleted_at": document.deleted_at,
            "updated_at": datetime.now(UTC),
        }
        await self._session.execute(
            update(DocumentModel).where(DocumentModel.id == document.id).values(**values)
        )
        await self._session.flush()
        result = await self._session.get(DocumentModel, document.id)
        assert result is not None
        return _document_to_domain(result)


class DocumentVersionRepository:
    """Postgres-backed document version repository."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, version: DocumentVersion) -> DocumentVersion:
        model = _version_from_domain(version)
        self._session.add(model)
        await self._session.flush()
        return _version_to_domain(model)

    async def get(self, version_id: uuid.UUID) -> DocumentVersion | None:
        result = await self._session.get(DocumentVersionModel, version_id)
        return _version_to_domain(result) if result else None

    async def get_by_document(self, document_id: uuid.UUID) -> list[DocumentVersion]:
        result = await self._session.execute(
            select(DocumentVersionModel).where(DocumentVersionModel.document_id == document_id)
        )
        return [_version_to_domain(row) for row in result.scalars()]

    async def get_by_documents(self, document_ids: list[uuid.UUID]) -> list[DocumentVersion]:
        """Batch-fetch versions across documents (avoids N+1 in list endpoints)."""
        if not document_ids:
            return []
        result = await self._session.execute(
            select(DocumentVersionModel).where(DocumentVersionModel.document_id.in_(document_ids))
        )
        return [_version_to_domain(row) for row in result.scalars()]

    async def get_latest(self, document_id: uuid.UUID) -> DocumentVersion | None:
        result = await self._session.execute(
            select(DocumentVersionModel)
            .where(DocumentVersionModel.document_id == document_id)
            .order_by(DocumentVersionModel.created_at.desc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        return _version_to_domain(row) if row else None

    async def update(self, version: DocumentVersion) -> DocumentVersion:
        values: dict[str, Any] = {
            "blob_hash": version.blob_hash,
            "content_hash": version.content_hash,
            "status": version.status.value,
            "parser_version": version.parser_version,
            "normalizer_version": version.normalizer_version,
            "chunker_version": version.chunker_version,
            "embedding_version": version.embedding_version,
            "processing_config_hash": version.processing_config_hash,
            "processing_config": dict(version.processing_config),
            "file_path": version.file_path,
        }
        await self._session.execute(
            update(DocumentVersionModel)
            .where(DocumentVersionModel.id == version.id)
            .values(**values)
        )
        await self._session.flush()
        result = await self._session.get(DocumentVersionModel, version.id)
        assert result is not None
        return _version_to_domain(result)


class ChunkRepository:
    """Postgres-backed chunk repository."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_batch(self, chunks: list[Chunk]) -> list[Chunk]:
        models = [_chunk_from_domain(c) for c in chunks]
        self._session.add_all(models)
        await self._session.flush()
        return [_chunk_to_domain(m) for m in models]

    async def get_by_version(self, version_id: uuid.UUID) -> list[Chunk]:
        result = await self._session.execute(
            select(ChunkModel)
            .where(ChunkModel.version_id == version_id)
            .order_by(ChunkModel.ordinal)
        )
        return [_chunk_to_domain(row) for row in result.scalars()]

    async def delete_by_version(self, version_id: uuid.UUID) -> int:
        from sqlalchemy import func as sa_func  # noqa: PLC0415

        # Count before deletion
        count_result = await self._session.execute(
            select(sa_func.count())
            .select_from(ChunkModel)
            .where(ChunkModel.version_id == version_id)
        )
        total: int = count_result.scalar_one()
        await self._session.execute(delete(ChunkModel).where(ChunkModel.version_id == version_id))
        await self._session.flush()
        return total


class IngestionTaskRepository:
    """Postgres-backed ingestion task repository."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, task: IngestionTask) -> IngestionTask:
        model = _task_from_domain(task)
        self._session.add(model)
        await self._session.flush()
        return _task_to_domain(model)

    async def get(self, task_id: uuid.UUID) -> IngestionTask | None:
        result = await self._session.get(IngestionTaskModel, task_id)
        return _task_to_domain(result) if result else None

    async def get_by_source(self, source_id: uuid.UUID) -> list[IngestionTask]:
        result = await self._session.execute(
            select(IngestionTaskModel).where(IngestionTaskModel.source_id == source_id)
        )
        return [_task_to_domain(row) for row in result.scalars()]

    async def checkpoint(self) -> None:
        """Commit the current transaction — persists stage progress and
        releases row locks so external cancel requests can proceed."""
        await self._session.commit()

    async def update(self, task: IngestionTask) -> IngestionTask:
        # Serialize task writers so a cancellation request cannot be overwritten
        # by a stale worker checkpoint or error update.
        current = await self._session.get(IngestionTaskModel, task.id, with_for_update=True)
        assert current is not None
        cancellation_in_flight = (
            current.cancel_requested_at is not None and task.status != TaskStatus.CANCELLED
        )
        values: dict[str, Any] = {
            "operation": task.operation.value,
            "status": current.status if cancellation_in_flight else task.status.value,
            "stage": task.stage.value,
            "target_version_id": task.target_version_id,
            "idempotency_key": task.idempotency_key,
            "progress": task.progress,
            "retry_count": task.retry_count,
            "max_retries": task.max_retries,
            "cancel_requested_at": (
                current.cancel_requested_at if cancellation_in_flight else task.cancel_requested_at
            ),
            "enqueued_at": task.enqueued_at,
            "heartbeat_at": task.heartbeat_at,
            "lease_expires_at": task.lease_expires_at,
            "error_code": task.error_code,
            "error": task.error,
            "updated_at": datetime.now(UTC),
        }
        await self._session.execute(
            update(IngestionTaskModel).where(IngestionTaskModel.id == task.id).values(**values)
        )
        await self._session.flush()
        result = await self._session.get(IngestionTaskModel, task.id)
        assert result is not None
        return _task_to_domain(result)
