"""Source and ingestion task API endpoints.

Implements the Step 8 API surface:
- ``POST   /api/v1/spaces/{space_id}/sources`` — create a new source
- ``GET    /api/v1/spaces/{space_id}/sources`` — list sources
- ``GET    /api/v1/spaces/{space_id}/sources/{source_id}/detail`` — get source detail
- ``POST   /api/v1/spaces/{space_id}/sources/{source_id}/upload`` — upload a file
- ``POST   /api/v1/spaces/{space_id}/sources/{source_id}/ingest`` — trigger ingestion
- ``GET    /api/v1/tasks/{task_id}`` — get task status
- ``POST   /api/v1/tasks/{task_id}/cancel`` — cancel a running task
- ``POST   /api/v1/tasks/{task_id}/retry`` — retry a failed task
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from application.ingestion.source_registration import SourceRegistrationService
from domain.models import IngestionTask, SourceType, TaskOperation, TaskStatus
from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from infrastructure.blob_store import LocalFileBlobStore
from infrastructure.config import settings
from infrastructure.database import Database
from infrastructure.repositories import (
    DocumentRepository,
    DocumentVersionRepository,
    IngestionTaskRepository,
    SourceRepository,
)
from infrastructure.telemetry_context import normalize_request_id
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class CreateSourceRequest(BaseModel):
    source_type: str = "upload"
    uri: str = ""


class CreateSourceResponse(BaseModel):
    source_id: str
    space_id: str
    source_type: str
    uri: str
    is_new: bool


class SourceItem(BaseModel):
    id: str
    space_id: str
    source_type: str
    uri: str
    created_at: str


class SourceListResponse(BaseModel):
    sources: list[SourceItem]


class UploadResponse(BaseModel):
    source_id: str
    document_id: str
    blob_hash: str
    is_new_document: bool
    is_unchanged: bool
    task_id: str | None = None


class DocumentItem(BaseModel):
    id: str
    stable_key: str
    current_version_id: str | None = None
    status: str
    created_at: str


class SourceDetailResponse(BaseModel):
    source: SourceItem
    documents: list[DocumentItem]


class TaskStatusResponse(BaseModel):
    task_id: str
    source_id: str
    operation: str
    status: str
    stage: str
    progress: float
    retry_count: int
    max_retries: int
    error_code: str | None = None
    error: str | None = None
    created_at: str


class IngestResponse(BaseModel):
    task_id: str


# ---------------------------------------------------------------------------
# Helper: extract database from app state
# ---------------------------------------------------------------------------


def _db(req: Request) -> Database:
    db: Database = req.app.state.database
    return db


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/spaces/{space_id}/sources", response_model=CreateSourceResponse)
async def create_source(
    space_id: UUID,
    body: CreateSourceRequest,
    request: Request,
) -> CreateSourceResponse:
    """Register a new data source under *space_id*."""
    db = _db(request)

    async with db.session() as session:
        service = SourceRegistrationService(
            source_repo=SourceRepository(session),
            document_repo=DocumentRepository(session),
            version_repo=DocumentVersionRepository(session),
        )
        result = await service.create_source(
            space_id=space_id,
            source_type=SourceType(body.source_type),
            uri=body.uri,
        )
        await session.commit()

        return CreateSourceResponse(
            source_id=str(result.source.id),
            space_id=str(space_id),
            source_type=result.source.source_type.value,
            uri=result.source.uri,
            is_new=result.is_new,
        )


@router.get("/spaces/{space_id}/sources", response_model=SourceListResponse)
async def list_sources(space_id: UUID, request: Request) -> SourceListResponse:
    """List all sources for a space."""
    db = _db(request)

    async with db.session() as session:
        repo = SourceRepository(session)
        sources = await repo.get_by_space(space_id)
        return SourceListResponse(
            sources=[
                SourceItem(
                    id=str(s.id),
                    space_id=str(s.space_id),
                    source_type=s.source_type.value,
                    uri=s.uri,
                    created_at=s.created_at.isoformat(),
                )
                for s in sources
            ]
        )


@router.get(
    "/spaces/{space_id}/sources/{source_id}/detail",
    response_model=SourceDetailResponse,
)
async def get_source_detail(
    space_id: UUID,
    source_id: UUID,
    request: Request,
) -> SourceDetailResponse:
    """Get a source with its documents."""
    db = _db(request)

    async with db.session() as session:
        source_repo = SourceRepository(session)
        doc_repo = DocumentRepository(session)

        source = await source_repo.get(source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Source not found")
        if source.space_id != space_id:
            raise HTTPException(status_code=404, detail="Source not found")

        docs = await doc_repo.get_by_source(source_id)

        return SourceDetailResponse(
            source=SourceItem(
                id=str(source.id),
                space_id=str(source.space_id),
                source_type=source.source_type.value,
                uri=source.uri,
                created_at=source.created_at.isoformat(),
            ),
            documents=[
                DocumentItem(
                    id=str(d.id),
                    stable_key=d.stable_key,
                    current_version_id=str(d.current_version_id) if d.current_version_id else None,
                    status="deleted" if d.deleted_at else "active",
                    created_at=d.created_at.isoformat(),
                )
                for d in docs
            ],
        )


@router.post(
    "/spaces/{space_id}/sources/{source_id}/upload",
    response_model=UploadResponse,
)
async def upload_file(
    space_id: UUID,
    source_id: UUID,
    request: Request,
    file: UploadFile = File(...),  # noqa: B008
) -> UploadResponse:
    """Upload a file, register it under *source_id*, and return the result."""
    db = _db(request)
    blob_store = LocalFileBlobStore()

    raw_bytes = await file.read()
    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    if len(raw_bytes) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds maximum size of {settings.max_upload_size_mb} MB",
        )

    async with db.session() as session:
        source_repo = SourceRepository(session)

        source = await source_repo.get(source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Source not found")
        if source.space_id != space_id:
            raise HTTPException(status_code=404, detail="Source not found")

        registration = SourceRegistrationService(
            source_repo=source_repo,
            document_repo=DocumentRepository(session),
            version_repo=DocumentVersionRepository(session),
        )
        result = await registration.register_file(
            source=source,
            raw_bytes=raw_bytes,
            blob_store=blob_store,
            file_stable_key=file.filename,
            file_path=file.filename,
        )

        # Create and enqueue ingestion task, pinned to the resolved version
        task_repo = IngestionTaskRepository(session)
        task = IngestionTask(
            source_id=source_id,
            operation=TaskOperation.INGEST,
            target_version_id=result.version_id,
        )
        task = await task_repo.create(task)
        await session.commit()

    _enqueue_ingestion_task(task.id)

    return UploadResponse(
        source_id=str(result.source.id),
        document_id=str(result.document.id),
        blob_hash=result.blob_hash,
        is_new_document=result.is_new_document,
        is_unchanged=result.existing_version is not None,
        task_id=str(task.id),
    )


@router.post(
    "/spaces/{space_id}/sources/{source_id}/ingest",
    response_model=IngestResponse,
)
async def trigger_ingestion(
    space_id: UUID,
    source_id: UUID,
    request: Request,
) -> IngestResponse:
    """Trigger an ingestion task for the source's document."""
    db = _db(request)

    async with db.session() as session:
        source_repo = SourceRepository(session)
        doc_repo = DocumentRepository(session)
        version_repo = DocumentVersionRepository(session)
        task_repo = IngestionTaskRepository(session)

        source = await source_repo.get(source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Source not found")
        if source.space_id != space_id:
            raise HTTPException(status_code=404, detail="Source not found")

        docs = await doc_repo.get_by_source(source_id)
        if not docs:
            raise HTTPException(
                status_code=400,
                detail="No document found for this source. Upload a file first.",
            )

        # Pin task to the latest version of the first document
        latest_version = await version_repo.get_latest(docs[0].id)

        task = IngestionTask(
            source_id=source_id,
            operation=TaskOperation.INGEST,
            target_version_id=latest_version.id if latest_version else None,
        )
        task = await task_repo.create(task)
        await session.commit()

    _enqueue_ingestion_task(task.id)

    return IngestResponse(task_id=str(task.id))


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(task_id: UUID, request: Request) -> TaskStatusResponse:
    """Get the current status of an ingestion task."""
    db = _db(request)

    async with db.session() as session:
        repo = IngestionTaskRepository(session)
        task = await repo.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")

        return TaskStatusResponse(
            task_id=str(task.id),
            source_id=str(task.source_id),
            operation=task.operation.value,
            status=task.status.value,
            stage=task.stage.value,
            progress=task.progress,
            retry_count=task.retry_count,
            max_retries=task.max_retries,
            error_code=task.error_code,
            error=task.error,
            created_at=task.created_at.isoformat(),
        )


TERMINAL_STATES = {
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.DEAD_LETTER,
}


@router.post("/tasks/{task_id}/cancel", response_model=TaskStatusResponse)
async def cancel_task_endpoint(
    task_id: UUID,
    request: Request,
) -> TaskStatusResponse:
    """Request cancellation of a running ingestion task."""
    db = _db(request)

    async with db.session() as session:
        task_repo = IngestionTaskRepository(session)
        task = await task_repo.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")

        if task.status in TERMINAL_STATES:
            raise HTTPException(
                status_code=409,
                detail=f"Task is already in terminal state {task.status.value}",
            )

        updated = await task_repo.update(
            IngestionTask(
                id=task.id,
                source_id=task.source_id,
                operation=task.operation,
                status=TaskStatus.CANCEL_REQUESTED,
                stage=task.stage,
                target_version_id=task.target_version_id,
                idempotency_key=task.idempotency_key,
                progress=task.progress,
                retry_count=task.retry_count,
                max_retries=task.max_retries,
                cancel_requested_at=datetime.now(UTC),
                enqueued_at=task.enqueued_at,
                heartbeat_at=task.heartbeat_at,
                lease_expires_at=task.lease_expires_at,
                error_code=task.error_code,
                error=task.error,
                created_at=task.created_at,
            )
        )
        await session.commit()

        return TaskStatusResponse(
            task_id=str(updated.id),
            source_id=str(updated.source_id),
            operation=updated.operation.value,
            status=updated.status.value,
            stage=updated.stage.value,
            progress=updated.progress,
            retry_count=updated.retry_count,
            max_retries=updated.max_retries,
            error_code=updated.error_code,
            error=updated.error,
            created_at=updated.created_at.isoformat(),
        )


@router.post("/tasks/{task_id}/retry", response_model=IngestResponse)
async def retry_task_endpoint(
    task_id: UUID,
    request: Request,
) -> IngestResponse:
    """Retry a failed or cancelled task by creating a new one."""
    db = _db(request)

    async with db.session() as session:
        task_repo = IngestionTaskRepository(session)
        old = await task_repo.get(task_id)
        if old is None:
            raise HTTPException(status_code=404, detail="Task not found")

        new_task = IngestionTask(
            source_id=old.source_id,
            operation=old.operation,
            target_version_id=old.target_version_id,
        )
        new_task = await task_repo.create(new_task)
        await session.commit()

    _enqueue_ingestion_task(new_task.id)

    return IngestResponse(task_id=str(new_task.id))


# ---------------------------------------------------------------------------
# Helper: enqueue via Dramatiq
# ---------------------------------------------------------------------------


def _enqueue_ingestion_task(task_id: UUID) -> None:
    """Send an ingestion task to the Dramatiq queue.

    Failures are logged but not raised — the task is already persisted
    in the database and can be replayed manually.
    """
    try:
        from worker.ingestion_tasks import enqueue_ingestion_task as _enq

        _enq(task_id=str(task_id), trace_id=normalize_request_id(None))
    except Exception:
        logger.exception(
            "Failed to enqueue ingestion task %s — persisted in DB",
            task_id,
        )
