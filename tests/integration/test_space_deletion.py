"""Integration tests for Space deletion and raw blob purge.

Deleting a Space cascades through the database, but uploaded bytes live in the
BlobStore.  Without an explicit purge they stay on disk forever, so this test
drives the real API against an isolated PostgreSQL and a temporary blob root.

Requires ``RUN_INTEGRATION=1`` and an isolated PostgreSQL service.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from api.main import create_app
from domain.fingerprinting import compute_storage_key
from domain.models import (
    Document,
    DocumentStatus,
    DocumentVersion,
    Source,
    SourceType,
    Space,
)
from httpx import ASGITransport, AsyncClient
from infrastructure.blob_store import LocalFileBlobStore
from infrastructure.config import settings
from infrastructure.database import Database
from infrastructure.orm import Base
from infrastructure.repositories import (
    DocumentRepository,
    DocumentVersionRepository,
    SourceRepository,
    SpaceRepository,
)
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION") != "1",
        reason="set RUN_INTEGRATION=1 with an isolated PostgreSQL service",
    ),
]


@pytest.fixture
async def api_database() -> AsyncIterator[Database]:
    database = Database(settings.database_url)
    async with database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
async def session(api_database: Database) -> AsyncIterator[AsyncSession]:
    async with api_database.session() as sess:
        yield sess


@pytest.fixture
async def app_client(
    api_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[AsyncClient]:
    monkeypatch.setattr(settings, "blob_store_path", str(tmp_path / "blobs"))
    test_app = create_app(database=api_database)
    async with AsyncClient(transport=ASGITransport(app=test_app), base_url="http://test") as client:
        yield client


async def _seed_space_with_blob(session: AsyncSession) -> tuple[Space, str]:
    """Create a Space whose document version owns one stored blob."""

    space = await SpaceRepository(session).create(Space(name="Purge Test Space"))
    source = await SourceRepository(session).create(
        Source(space_id=space.id, source_type=SourceType.UPLOAD, uri="notes.md")
    )
    document = await DocumentRepository(session).create(
        Document(source_id=source.id, stable_key="notes.md")
    )
    blob_hash = "a" * 64
    await DocumentVersionRepository(session).create(
        DocumentVersion(
            document_id=document.id,
            blob_hash=blob_hash,
            status=DocumentStatus.PUBLISHED,
        )
    )
    await session.commit()

    key = compute_storage_key(source.id, blob_hash)
    store = LocalFileBlobStore()
    await store.store(key, b"# notes\n\nbody\n")
    assert await store.exists(key)
    return space, key


@pytest.mark.asyncio
async def test_deleting_a_space_purges_its_blobs(
    session: AsyncSession,
    app_client: AsyncClient,
) -> None:
    space, key = await _seed_space_with_blob(session)
    store = LocalFileBlobStore()

    response = await app_client.delete(f"/api/v1/spaces/{space.id}")

    assert response.status_code == 204, response.text
    assert not await store.exists(key), "Space deletion must remove raw uploads"
    session.expire_all()
    assert await SpaceRepository(session).get(space.id) is None


@pytest.mark.asyncio
async def test_deleting_a_space_leaves_other_spaces_blobs_alone(
    session: AsyncSession,
    app_client: AsyncClient,
) -> None:
    keep_space, keep_key = await _seed_space_with_blob(session)
    doomed_space, doomed_key = await _seed_space_with_blob(session)
    store = LocalFileBlobStore()

    response = await app_client.delete(f"/api/v1/spaces/{doomed_space.id}")

    assert response.status_code == 204, response.text
    assert not await store.exists(doomed_key)
    assert await store.exists(keep_key), "another Space's bytes must survive"
    session.expire_all()
    assert await SpaceRepository(session).get(keep_space.id) is not None


@pytest.mark.asyncio
async def test_deleting_an_unknown_space_is_404(
    app_client: AsyncClient,
) -> None:
    response = await app_client.delete(f"/api/v1/spaces/{uuid4()}")

    assert response.status_code == 404, response.text
