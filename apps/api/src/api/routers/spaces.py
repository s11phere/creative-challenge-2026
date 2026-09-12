"""Tenant-scoped Space lifecycle for the website's first release."""

from __future__ import annotations

from uuid import UUID

from application.ingestion import SpaceDeletionService
from domain.models import Space
from fastapi import APIRouter, HTTPException, Request
from infrastructure.blob_store import LocalFileBlobStore
from infrastructure.repositories import (
    DocumentRepository,
    DocumentVersionRepository,
    SourceRepository,
    SpaceRepository,
)
from pydantic import BaseModel, Field

from ..authz import request_owner_id, require_space_access
from ..service_auth import authenticated_app_user_id

router = APIRouter(prefix="/api/v1/spaces", tags=["spaces"])


class SpaceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class SpaceResponse(BaseModel):
    id: UUID
    name: str
    owner_id: str


@router.post("", response_model=SpaceResponse, status_code=201)
async def create_space(body: SpaceCreateRequest, request: Request) -> SpaceResponse:
    owner_id = request_owner_id(request)
    async with request.app.state.database.session() as session:
        space = await SpaceRepository(session).create(
            Space(name=body.name.strip(), owner_id=owner_id)
        )
        await session.commit()
    return SpaceResponse(id=space.id, name=space.name, owner_id=space.owner_id)


@router.get("", response_model=list[SpaceResponse])
async def list_spaces(request: Request) -> list[SpaceResponse]:
    owner_id = authenticated_app_user_id(request.scope)
    async with request.app.state.database.session() as session:
        repo = SpaceRepository(session)
        # Filter in SQL so one tenant's request never loads every other
        # tenant's rows.  Only the unauthenticated local development client
        # still lists the whole table.
        values = await repo.list() if owner_id is None else await repo.list_by_owner(owner_id)
    return [
        SpaceResponse(id=space.id, name=space.name, owner_id=space.owner_id) for space in values
    ]


@router.delete("/{space_id}", status_code=204)
async def delete_space(space_id: UUID, request: Request) -> None:
    await require_space_access(request, space_id)
    async with request.app.state.database.session() as session:
        repo = SpaceRepository(session)
        if await repo.get(space_id) is None:
            raise HTTPException(status_code=404, detail="Space not found")
        deletion = SpaceDeletionService(
            source_repo=SourceRepository(session),
            document_repo=DocumentRepository(session),
            version_repo=DocumentVersionRepository(session),
            blob_store=LocalFileBlobStore(),
        )
        # Collect before the cascade removes the rows, then purge only after
        # the transaction commits so a failed delete cannot destroy bytes that
        # the database still references.
        blob_keys = await deletion.collect_blob_keys(space_id)
        await repo.delete(space_id)
        await session.commit()
    await deletion.purge_blobs(blob_keys)
