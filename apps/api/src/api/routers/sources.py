"""Source and ingestion task API endpoints.

Implements the Step 8 API surface:
- ``POST   /api/v1/spaces/{space_id}/sources`` — create a new source
- ``GET    /api/v1/spaces/{space_id}/sources`` — list sources
- ``DELETE /api/v1/spaces/{space_id}/sources/{source_id}`` — delete an empty
  source (rollback for failed uploads)
- ``GET    /api/v1/spaces/{space_id}/sources/{source_id}/detail`` — get source detail
- ``POST   /api/v1/spaces/{space_id}/sources/{source_id}/upload`` — upload a file
- ``POST   /api/v1/spaces/{space_id}/sources/{source_id}/ingest`` — trigger ingestion
- ``GET    /api/v1/tasks/{task_id}`` — get task status
- ``POST   /api/v1/tasks/{task_id}/cancel`` — cancel a running task
- ``POST   /api/v1/tasks/{task_id}/retry`` — retry a failed task
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Literal
from uuid import UUID

from application.ingestion import DocumentDeletionService, IngestionConfig
from application.ingestion.source_registration import SourceRegistrationService
from domain.fingerprinting import compute_storage_key
from domain.models import (
    Document,
    DocumentStatus,
    DocumentVersion,
    IngestionTask,
    SourceType,
    TaskOperation,
    TaskStatus,
)
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

from ..authz import require_space_access

logger = logging.getLogger(__name__)

# Multipart framing (boundary, part headers, filename) rides along in the
# request Content-Length, so the early 413 guard allows this much envelope
# before declaring the request too large.  The exact per-file limit is still
# enforced on the decoded bytes below.
_MULTIPART_ENVELOPE_SLACK_BYTES = 64 * 1024


def _declared_upload_exceeds_limit(content_length: str | None, max_bytes: int) -> bool:
    """Whether the declared request body is too large to even buffer.

    Multipart framing (boundary, part headers, filename) rides along in the
    request ``Content-Length``, so the guard allows
    ``_MULTIPART_ENVELOPE_SLACK_BYTES`` of envelope before refusing.  The exact
    per-file limit is still enforced on the decoded bytes.
    """

    if content_length is None:
        return False
    try:
        declared_bytes = int(content_length)
    except ValueError:
        return False
    return declared_bytes > max_bytes + _MULTIPART_ENVELOPE_SLACK_BYTES


router = APIRouter(prefix="/api/v1")


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------


class CreateSourceRequest(BaseModel):
    # Constrained at the request boundary so an unknown value is a 422 field
    # error instead of a ValueError escaping from `SourceType(...)`.
    source_type: Literal["upload", "folder"] = "upload"
    uri: str = ""
    name: str = ""


class CreateSourceResponse(BaseModel):
    source_id: str
    space_id: str
    source_type: str
    uri: str
    name: str = ""
    is_new: bool


class SourceItem(BaseModel):
    id: str
    space_id: str
    source_type: str
    uri: str
    name: str = ""
    created_at: str
    doc_count: int = 0
    available_count: int = 0
    failed_count: int = 0
    primary_document_name: str | None = None


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


class DeleteDocumentResponse(BaseModel):
    document_id: str
    status: Literal["deleted", "already_deleted"]
    task_id: str | None = None


class DeleteSourceResponse(BaseModel):
    source_id: str
    status: Literal["deleted"]


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


class IngestBatchResponse(BaseModel):
    task_ids: list[str]


class RenameSourceRequest(BaseModel):
    name: str


class RenameSourceResponse(BaseModel):
    source_id: str
    name: str
    status: Literal["renamed"]


class ClearSourceResponse(BaseModel):
    source_id: str
    status: Literal["deleted"]
    documents_cleared: int = 0


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


def _document_status(
    document: Document,
    latest_version: DocumentVersion | None,
    current_version: DocumentVersion | None,
) -> str:
    """Stable user-facing status for a document, shared by detail and list."""
    if document.deleted_at is not None:
        return "deleted"
    if current_version is not None and current_version.status is DocumentStatus.PUBLISHED:
        return "available"
    if latest_version is not None and latest_version.status is DocumentStatus.FAILED:
        return "failed"
    return "unavailable"


def _version_indexes(
    versions: list[DocumentVersion],
) -> tuple[dict[uuid.UUID, DocumentVersion | None], dict[uuid.UUID, DocumentVersion]]:
    """Return (latest version per document, all versions by id)."""
    latest_by_doc: dict[uuid.UUID, DocumentVersion | None] = {}
    by_id: dict[uuid.UUID, DocumentVersion] = {}
    for version in versions:
        by_id[version.id] = version
        current = latest_by_doc.get(version.document_id)
        if current is None or version.created_at > current.created_at:
            latest_by_doc[version.document_id] = version
    return latest_by_doc, by_id


def _source_stats(
    documents: list[Document],
    latest_by_doc: dict[uuid.UUID, DocumentVersion | None],
    versions_by_id: dict[uuid.UUID, DocumentVersion],
) -> tuple[int, int, int, str | None]:
    """Aggregate a source's document counts and a primary display name.

    ``available_count`` counts live documents whose current version is
    PUBLISHED; ``failed_count`` counts live documents whose latest version is
    FAILED. The primary name prefers a live, published document so a source
    card shows a meaningful title instead of the shared upload URI.
    """
    doc_count = available_count = failed_count = 0
    live: list[Document] = []
    for document in documents:
        if document.deleted_at is not None:
            continue
        live.append(document)
        doc_count += 1
        current = (
            versions_by_id.get(document.current_version_id)
            if document.current_version_id is not None
            else None
        )
        latest = latest_by_doc.get(document.id)
        if current is not None and current.status is DocumentStatus.PUBLISHED:
            available_count += 1
        elif latest is not None and latest.status is DocumentStatus.FAILED:
            failed_count += 1

    primary_name: str | None = None
    if live:
        preferred = next(
            (
                document
                for document in live
                if document.current_version_id is not None
                and versions_by_id.get(document.current_version_id) is not None
                and versions_by_id[document.current_version_id].status is DocumentStatus.PUBLISHED
            ),
            None,
        )
        chosen = preferred or min(live, key=lambda document: document.created_at)
        latest = latest_by_doc.get(chosen.id)
        primary_name = _document_display_name(
            chosen.stable_key,
            latest.file_path if latest is not None else None,
        )

    return doc_count, available_count, failed_count, primary_name


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
    """Register a new data source under *space_id*.

    Folder sources (``source_type="folder"``) require a non-empty, space-unique
    ``name`` — the folder label the user chose.
    """
    await require_space_access(request, space_id)
    db = _db(request)

    name = body.name.strip()
    if body.source_type == "folder":
        if not name:
            raise HTTPException(status_code=422, detail="文件夹名称不能为空")
        if len(name) > 255:
            raise HTTPException(status_code=422, detail="文件夹名称过长（最多 255 字）")

    async with db.session() as session:
        if body.source_type == "folder":
            siblings = await SourceRepository(session).get_by_space(space_id)
            if any(sibling.name == name for sibling in siblings):
                raise HTTPException(status_code=409, detail="同名文件夹已存在")

        service = SourceRegistrationService(
            source_repo=SourceRepository(session),
            document_repo=DocumentRepository(session),
            version_repo=DocumentVersionRepository(session),
        )
        result = await service.create_source(
            space_id=space_id,
            source_type=SourceType(body.source_type),
            uri=body.uri,
            name=name,
        )
        await session.commit()

        return CreateSourceResponse(
            source_id=str(result.source.id),
            space_id=str(space_id),
            source_type=result.source.source_type.value,
            uri=result.source.uri,
            name=result.source.name,
            is_new=result.is_new,
        )


@router.delete(
    "/spaces/{space_id}/sources/{source_id}",
    response_model=DeleteSourceResponse,
)
async def delete_source(
    space_id: UUID,
    source_id: UUID,
    request: Request,
) -> DeleteSourceResponse:
    """Delete an empty source (no documents) and its derived rows.

    Intended as rollback for a source created by a failed browser upload.
    Sources that already hold documents are refused so content is never
    removed through this endpoint; callers must delete documents first.
    """
    await require_space_access(request, space_id)
    db = _db(request)

    async with db.session() as session:
        source_repo = SourceRepository(session)
        source = await source_repo.get(source_id)
        if source is None or source.space_id != space_id:
            raise HTTPException(status_code=404, detail="Source not found")

        docs = await DocumentRepository(session).get_by_source(source_id)
        if docs:
            raise HTTPException(
                status_code=409,
                detail="Source contains documents and cannot be deleted",
            )

        await source_repo.delete(source_id)
        await session.commit()

    return DeleteSourceResponse(source_id=str(source_id), status="deleted")


@router.patch(
    "/spaces/{space_id}/sources/{source_id}",
    response_model=RenameSourceResponse,
)
async def rename_source(
    space_id: UUID,
    source_id: UUID,
    body: RenameSourceRequest,
    request: Request,
) -> RenameSourceResponse:
    """Rename a source (a user-facing folder label)."""
    await require_space_access(request, space_id)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="文件夹名称不能为空")
    if len(name) > 255:
        raise HTTPException(status_code=422, detail="文件夹名称过长（最多 255 字）")

    db = _db(request)
    async with db.session() as session:
        repo = SourceRepository(session)
        source = await repo.get(source_id)
        if source is None or source.space_id != space_id:
            raise HTTPException(status_code=404, detail="Source not found")

        siblings = await repo.get_by_space(space_id)
        if any(sibling.name == name and sibling.id != source_id for sibling in siblings):
            raise HTTPException(status_code=409, detail="同名文件夹已存在")

        updated = await repo.update(replace(source, name=name))
        await session.commit()

    return RenameSourceResponse(source_id=str(source_id), name=updated.name, status="renamed")


@router.delete(
    "/spaces/{space_id}/sources/{source_id}/contents",
    response_model=ClearSourceResponse,
)
async def clear_source(
    space_id: UUID,
    source_id: UUID,
    request: Request,
) -> ClearSourceResponse:
    """Delete a source together with all its documents.

    The source row removal cascades through documents, versions, chunks and
    ingestion tasks (all FKs are ON DELETE CASCADE), so no cleanup tasks are
    enqueued — the worker would refuse to run them without the source. Blob
    files on disk are removed inline; their storage keys embed the source id
    so they are never shared with another source.
    """
    await require_space_access(request, space_id)
    db = _db(request)
    async with db.session() as session:
        source_repo = SourceRepository(session)
        source = await source_repo.get(source_id)
        if source is None or source.space_id != space_id:
            raise HTTPException(status_code=404, detail="Source not found")

        doc_repo = DocumentRepository(session)
        version_repo = DocumentVersionRepository(session)
        docs = await doc_repo.get_by_source(source_id)
        live_count = sum(1 for document in docs if document.deleted_at is None)

        versions = await version_repo.get_by_documents([document.id for document in docs])
        blob_hashes = {version.blob_hash for version in versions if version.blob_hash}

        await source_repo.delete(source_id)
        await session.commit()

    for blob_hash in blob_hashes:
        try:
            await LocalFileBlobStore().delete(compute_storage_key(source_id, blob_hash))
        except Exception:  # noqa: BLE001
            logger.debug("folder-delete blob cleanup skipped for %s", blob_hash)

    return ClearSourceResponse(
        source_id=str(source_id),
        status="deleted",
        documents_cleared=live_count,
    )


@router.get("/spaces/{space_id}/sources", response_model=SourceListResponse)
async def list_sources(space_id: UUID, request: Request) -> SourceListResponse:
    """List all sources for a space with per-source document stats.

    Documents and versions are fetched in two batch queries (not N+1) so the
    UI can render counts and a primary document name without expanding a
    source first.
    """
    await require_space_access(request, space_id)
    db = _db(request)

    async with db.session() as session:
        source_repo = SourceRepository(session)
        doc_repo = DocumentRepository(session)
        version_repo = DocumentVersionRepository(session)

        sources = await source_repo.get_by_space(space_id)
        source_ids = [source.id for source in sources]
        documents = await doc_repo.get_by_sources(source_ids)
        document_ids = [document.id for document in documents]
        versions = await version_repo.get_by_documents(document_ids)
        latest_by_doc, versions_by_id = _version_indexes(versions)

        docs_by_source: dict[uuid.UUID, list[Document]] = {}
        for document in documents:
            docs_by_source.setdefault(document.source_id, []).append(document)

        items: list[SourceItem] = []
        for source in sources:
            doc_count, available_count, failed_count, primary_name = _source_stats(
                docs_by_source.get(source.id, []),
                latest_by_doc,
                versions_by_id,
            )
            items.append(
                SourceItem(
                    id=str(source.id),
                    space_id=str(source.space_id),
                    source_type=source.source_type.value,
                    uri=source.uri,
                    name=source.name,
                    created_at=source.created_at.isoformat(),
                    doc_count=doc_count,
                    available_count=available_count,
                    failed_count=failed_count,
                    primary_document_name=primary_name,
                )
            )

        return SourceListResponse(sources=items)


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
    await require_space_access(request, space_id)
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
        source_versions: list[DocumentVersion] = []
        for document in docs:
            latest_version = await version_repo.get_latest(document.id)
            current_version = (
                await version_repo.get(document.current_version_id)
                if document.current_version_id is not None
                else None
            )
            if latest_version is not None:
                source_versions.append(latest_version)
            if current_version is not None:
                source_versions.append(current_version)
            document_status = _document_status(document, latest_version, current_version)
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

        latest_by_doc, versions_by_id = _version_indexes(source_versions)
        doc_count, available_count, failed_count, primary_name = _source_stats(
            docs, latest_by_doc, versions_by_id
        )

    return SourceDetailResponse(
        source=SourceItem(
            id=str(source.id),
            space_id=str(source.space_id),
            source_type=source.source_type.value,
            uri=source.uri,
            name=source.name,
            created_at=source.created_at.isoformat(),
            doc_count=doc_count,
            available_count=available_count,
            failed_count=failed_count,
            primary_document_name=primary_name,
        ),
        documents=document_items,
    )


@router.delete(
    "/spaces/{space_id}/sources/{source_id}/documents/{document_id}",
    response_model=DeleteDocumentResponse,
)
async def delete_document(
    space_id: UUID,
    source_id: UUID,
    document_id: UUID,
    request: Request,
) -> DeleteDocumentResponse:
    """Tombstone one document and enqueue cleanup of its derived artifacts."""
    await require_space_access(request, space_id)
    db = _db(request)

    async with db.session() as session:
        source = await SourceRepository(session).get(source_id)
        if source is None or source.space_id != space_id:
            raise HTTPException(status_code=404, detail="Source not found")

        document_repo = DocumentRepository(session)
        document = await document_repo.get(document_id)
        if document is None or document.source_id != source_id:
            raise HTTPException(status_code=404, detail="Document not found")

        already_deleted = document.deleted_at is not None
        task = await DocumentDeletionService(
            document_repo=document_repo,
            version_repo=DocumentVersionRepository(session),
            task_repo=IngestionTaskRepository(session),
        ).delete_document(document)
        await session.commit()

    if task is not None:
        await _enqueue_ingestion_task(task.id, db)

    return DeleteDocumentResponse(
        document_id=str(document_id),
        status="already_deleted" if already_deleted else "deleted",
        task_id=str(task.id) if task is not None else None,
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
    await require_space_access(request, space_id)
    db = _db(request)
    blob_store = LocalFileBlobStore()

    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    # Reject an oversized body before buffering it: `file.read()` loads the
    # whole upload into memory, so the Content-Length check is what keeps a
    # declared-oversize request from consuming the worker's memory.
    if _declared_upload_exceeds_limit(request.headers.get("content-length"), max_bytes):
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds maximum size of {settings.max_upload_size_mb} MB",
        )

    raw_bytes = await file.read()
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
        # Pin the candidate to the same processing identity used by online
        # retrieval.  A missing model configuration still permits upload; the
        # worker will report the model failure through the task state.
        identity = settings.active_embedding_identity(allow_unconfigured=True)
        processing_config = IngestionConfig(embedding_identity=identity).processing_config()
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
            embedding_version=identity.version,
            processing_config=processing_config,
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
    response_model=IngestBatchResponse,
)
async def trigger_ingestion(
    space_id: UUID,
    source_id: UUID,
    request: Request,
) -> IngestBatchResponse:
    """Trigger an ingestion task for every live document in the source.

    Previously this only pinned the first document's latest version, which
    silently ignored every other file when the button was used on a
    multi-document source. It now enqueues one task per live document, reusing
    any reusable active task already targeting the same version.
    """
    await require_space_access(request, space_id)
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
        live_docs = [document for document in docs if document.deleted_at is None]
        if not live_docs:
            raise HTTPException(
                status_code=400,
                detail="No document found for this source. Upload a file first.",
            )

        active_tasks = await task_repo.get_by_source(source_id)
        seen_versions: set[uuid.UUID] = set()
        task_ids: list[uuid.UUID] = []
        for document in live_docs:
            latest_version = await version_repo.get_latest(document.id)
            if latest_version is None:
                continue
            target_version_id = latest_version.id
            if target_version_id in seen_versions:
                continue
            seen_versions.add(target_version_id)
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
                task_ids.append(existing.id)
                continue
            task = IngestionTask(
                source_id=source_id,
                operation=TaskOperation.INGEST,
                target_version_id=target_version_id,
            )
            task = await task_repo.create(task)
            task_ids.append(task.id)
        await session.commit()

    for task_id in task_ids:
        await _enqueue_ingestion_task(task_id, db)

    return IngestBatchResponse(task_ids=[str(task_id) for task_id in task_ids])


@router.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(task_id: UUID, request: Request) -> TaskStatusResponse:
    """Get the current status of an ingestion task."""
    db = _db(request)

    async with db.session() as session:
        repo = IngestionTaskRepository(session)
        task = await repo.get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        source = await SourceRepository(session).get(task.source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Task not found")
        await require_space_access(request, source.space_id)

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
        source = await SourceRepository(session).get(task.source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Task not found")
        await require_space_access(request, source.space_id)

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
        source = await SourceRepository(session).get(old.source_id)
        if source is None:
            raise HTTPException(status_code=404, detail="Task not found")
        await require_space_access(request, source.space_id)

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
