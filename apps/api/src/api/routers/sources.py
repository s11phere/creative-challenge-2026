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
from pathlib import PurePosixPath
from uuid import UUID

from application.ingestion.source_registration import SourceRegistrationService
from domain.models import DocumentStatus, IngestionTask, SourceType, TaskOperation, TaskStatus
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
    display_name: str
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


def _task_response(task: IngestionTask) -> TaskStatusResponse:
    """Map a domain task to the stable public task schema."""
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


def _display_filename(filename: str | None) -> str:
    """Return a safe, user-facing basename while preserving Unicode."""
    if not filename:
        return "未命名文档"
    decoded = filename
    # Some multipart clients send raw UTF-8 filename bytes in a header parsed
    # as a mixture of Latin-1 and CP1252. Native Windows command invocation can
    # apply that conversion multiple times. Continue only while each reversible
    # repair shortens the string, with a hard cap for hostile input.
    for _ in range(8):
        raw = bytearray()
        for character in decoded:
            codepoint = ord(character)
            if codepoint <= 0xFF:
                raw.append(codepoint)
                continue
            try:
                encoded = character.encode("cp1252")
            except UnicodeEncodeError:
                raw.clear()
                break
            if len(encoded) != 1:
                raw.clear()
                break
            raw.extend(encoded)
        if not raw:
            break
        try:
            repaired = raw.decode("utf-8")
        except UnicodeDecodeError:
            break
        if len(repaired) >= len(decoded):
            break
        decoded = repaired
    return PurePosixPath(decoded.replace("\\", "/")).name or "未命名文档"


def _document_display_name(stable_key: str, file_path: str | None) -> str:
    """Choose a useful title when older uploads stored a placeholder name."""
    candidate = file_path
    placeholders = {"未命名文档", "未命名文件"}
    if candidate in {None, ""} or _display_filename(candidate) in placeholders:
        candidate = stable_key
    return _display_filename(candidate)


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
        version_repo = DocumentVersionRepository(session)

        source = await source_repo.get(source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Source not found")
        if source.space_id != space_id:
            raise HTTPException(status_code=404, detail="Source not found")

        docs = await doc_repo.get_by_source(source_id)

        document_items: list[DocumentItem] = []
        for document in docs:
            latest_version = await version_repo.get_latest(document.id)
            current_version = (
                await version_repo.get(document.current_version_id)
                if document.current_version_id is not None
                else None
            )
            if document.deleted_at is not None:
                document_status = "deleted"
            elif current_version is not None and current_version.status is DocumentStatus.PUBLISHED:
                document_status = "available"
            elif latest_version is not None and latest_version.status is DocumentStatus.FAILED:
                document_status = "failed"
            else:
                document_status = "unavailable"
            document_items.append(
                DocumentItem(
                    id=str(document.id),
                    stable_key=document.stable_key,
                    display_name=_document_display_name(
                        document.stable_key,
                        latest_version.file_path if latest_version else None,
                    ),
                    current_version_id=(
                        str(document.current_version_id) if document.current_version_id else None
                    ),
                    status=document_status,
                    created_at=document.created_at.isoformat(),
                )
            )

        return SourceDetailResponse(
            source=SourceItem(
                id=str(source.id),
                space_id=str(source.space_id),
                source_type=source.source_type.value,
                uri=source.uri,
                created_at=source.created_at.isoformat(),
            ),
            documents=document_items,
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

        original_filename = _display_filename(file.filename)
        registration = SourceRegistrationService(
            source_repo=source_repo,
            document_repo=DocumentRepository(session),
            version_repo=DocumentVersionRepository(session),
        )
        result = await registration.register_file(
            source=source,
            raw_bytes=raw_bytes,
            blob_store=blob_store,
            file_stable_key=original_filename,
            file_path=original_filename,
        )

        # A published version that is still current already represents these
        # bytes. Avoid creating a duplicate task; failed or non-current
        # candidates still need a retryable task.
        unchanged_published = (
            result.existing_version is not None
            and result.existing_version.status == DocumentStatus.PUBLISHED
            and result.document.current_version_id == result.existing_version.id
        )
        task_id: UUID | None = None
        if not unchanged_published:
            task_repo = IngestionTaskRepository(session)
            task = await task_repo.create(
                IngestionTask(
                    source_id=source_id,
                    operation=TaskOperation.INGEST,
                    target_version_id=result.version_id,
                )
            )
            task_id = task.id
        await session.commit()

    if task_id is not None:
        await _enqueue_ingestion_task(task_id, db)

    return UploadResponse(
        source_id=str(result.source.id),
        document_id=str(result.document.id),
        blob_hash=result.blob_hash,
        is_new_document=result.is_new_document,
        is_unchanged=result.existing_version is not None,
        task_id=str(task_id) if task_id is not None else None,
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
        target_version_id = latest_version.id if latest_version else None

        active_tasks = await task_repo.get_by_source(source_id)
        existing = next(
            (
                candidate
                for candidate in active_tasks
                if _is_reusable_active_task(candidate)
                and candidate.target_version_id == target_version_id
            ),
            None,
        )
        if existing is not None:
            return IngestResponse(task_id=str(existing.id))

        task = IngestionTask(
            source_id=source_id,
            operation=TaskOperation.INGEST,
            target_version_id=target_version_id,
        )
        task = await task_repo.create(task)
        await session.commit()

    await _enqueue_ingestion_task(task.id, db)

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

        return _task_response(task)


TERMINAL_STATES = {
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
    TaskStatus.DEAD_LETTER,
}

ACTIVE_STATES = {
    TaskStatus.QUEUED,
    TaskStatus.RUNNING,
}


def _is_reusable_active_task(task: IngestionTask) -> bool:
    """Return whether an active task still has a worker attempt to reuse."""
    if task.status not in ACTIVE_STATES:
        return False
    # A running task with a recorded error has already lost its worker attempt
    # or is waiting on a broker retry; do not pin new UI actions to it.
    return task.status != TaskStatus.RUNNING or task.error_code is None


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

        # Cancellation is idempotent. The worker can finish between the UI's
        # last poll and this request; returning the authoritative terminal
        # state lets clients converge without surfacing a false conflict.
        if task.status in TERMINAL_STATES:
            return _task_response(task)

        # Persist cancellation as a terminal user-visible state immediately.
        # The worker still observes cancel_requested_at and exits cooperatively;
        # this also converges safely when the broker message was already lost.
        cancel_status = TaskStatus.CANCELLED

        updated = await task_repo.update(
            IngestionTask(
                id=task.id,
                source_id=task.source_id,
                operation=task.operation,
                status=cancel_status,
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

        return _task_response(updated)


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

        if old.status not in {TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.DEAD_LETTER}:
            raise HTTPException(
                status_code=409,
                detail=f"Task cannot be retried from state {old.status.value}",
            )

        active_tasks = await task_repo.get_by_source(old.source_id)
        existing = next(
            (
                candidate
                for candidate in active_tasks
                if _is_reusable_active_task(candidate)
                and candidate.target_version_id == old.target_version_id
            ),
            None,
        )
        if existing is not None:
            return IngestResponse(task_id=str(existing.id))

        new_task = IngestionTask(
            source_id=old.source_id,
            operation=old.operation,
            target_version_id=old.target_version_id,
        )
        new_task = await task_repo.create(new_task)
        await session.commit()

    await _enqueue_ingestion_task(new_task.id, db)

    return IngestResponse(task_id=str(new_task.id))


# ---------------------------------------------------------------------------
# Helper: enqueue via Dramatiq
# ---------------------------------------------------------------------------


async def _enqueue_ingestion_task(task_id: UUID, database: Database) -> None:
    """Send an ingestion task to the Dramatiq queue.

    On enqueue failure the task is marked ``FAILED`` in the database so
    it does not remain silently stuck in ``QUEUED``.  Users can retry
    from the UI.
    """
    try:
        from worker.ingestion_tasks import enqueue_ingestion_task as _enq

        _enq(task_id=str(task_id), trace_id=normalize_request_id(None))
    except Exception:
        logger.exception("Failed to enqueue ingestion task %s", task_id)
        async with database.session() as session:
            repo = IngestionTaskRepository(session)
            task = await repo.get(task_id)
            if task is not None:
                await repo.update(
                    IngestionTask(
                        id=task.id,
                        source_id=task.source_id,
                        operation=task.operation,
                        status=TaskStatus.FAILED,
                        stage=task.stage,
                        target_version_id=task.target_version_id,
                        idempotency_key=task.idempotency_key,
                        progress=task.progress,
                        retry_count=task.retry_count,
                        max_retries=task.max_retries,
                        cancel_requested_at=task.cancel_requested_at,
                        enqueued_at=task.enqueued_at,
                        heartbeat_at=task.heartbeat_at,
                        lease_expires_at=task.lease_expires_at,
                        error_code="ENQUEUE_FAILED",
                        error="Failed to enqueue task to Redis: task stays in QUEUED",
                        created_at=task.created_at,
                    )
                )
                await session.commit()
