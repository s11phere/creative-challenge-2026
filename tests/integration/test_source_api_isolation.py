"""Integration tests for Source API cross-Space isolation and Redis resilience.

Requires ``RUN_INTEGRATION=1`` and isolated PostgreSQL and Redis services.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from api.main import app
from domain.models import (
    IngestionTask,
    Source,
    SourceType,
    Space,
    TaskOperation,
    TaskStatus,
)
from httpx import ASGITransport, AsyncClient
from infrastructure.config import settings
from infrastructure.orm import Base
from infrastructure.repositories import (
    IngestionTaskRepository,
    SourceRepository,
    SpaceRepository,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION") != "1",
        reason="set RUN_INTEGRATION=1 with isolated PostgreSQL and Redis services",
    ),
]


@pytest.fixture
async def session() -> AsyncSession:
    """Create a clean schema for each test and tear it down afterwards."""
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with maker() as sess:
        yield sess
    await engine.dispose()


@pytest.fixture
async def app_client() -> AsyncClient:
    """Return an ASGI client for the FastAPI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ============================================================
# Cross-Space isolation (PR review item #3)
# ============================================================


class TestCrossSpaceIsolation:
    """API endpoints must reject requests where the path space_id does not
    match the Source's actual space_id (P0, see PR review item #3).

    Each test seeds a source under a known space, commits, then calls the
    API with a wrong space_id — the app uses its own database connection so
    the seed must be visible to it via commit().
    """

    async def _seed_source(self, session: AsyncSession) -> tuple[Space, Source]:
        """Create a Space + Source, commit, and return both."""
        space_repo = SpaceRepository(session)
        space = await space_repo.create(Space(name="Isolation Test Space"))
        source_repo = SourceRepository(session)
        source = await source_repo.create(
            Source(space_id=space.id, source_type=SourceType.UPLOAD, uri="test.txt")
        )
        await session.commit()
        return space, source

    @pytest.mark.asyncio
    async def test_detail_wrong_space_returns_404(
        self, session: AsyncSession, app_client: AsyncClient
    ) -> None:
        """GET /spaces/{wrong}/sources/{id}/detail returns 404."""
        _, source = await self._seed_source(session)
        wrong = uuid4()

        resp = await app_client.get(f"/api/v1/spaces/{wrong}/sources/{source.id}/detail")
        assert resp.status_code == 404, (
            f"Expected 404 for wrong space_id, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_upload_wrong_space_returns_404(
        self, session: AsyncSession, app_client: AsyncClient
    ) -> None:
        """POST /spaces/{wrong}/sources/{id}/upload returns 404."""
        _, source = await self._seed_source(session)
        wrong = uuid4()

        resp = await app_client.post(
            f"/api/v1/spaces/{wrong}/sources/{source.id}/upload",
            files={"file": ("test.txt", b"hello world")},
        )
        assert resp.status_code == 404, (
            f"Expected 404 for wrong space_id, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_ingest_wrong_space_returns_404(
        self, session: AsyncSession, app_client: AsyncClient
    ) -> None:
        """POST /spaces/{wrong}/sources/{id}/ingest returns 404."""
        _, source = await self._seed_source(session)
        wrong = uuid4()

        resp = await app_client.post(f"/api/v1/spaces/{wrong}/sources/{source.id}/ingest")
        assert resp.status_code == 404, (
            f"Expected 404 for wrong space_id, got {resp.status_code}: {resp.text}"
        )

    @pytest.mark.asyncio
    async def test_right_space_returns_200(
        self, session: AsyncSession, app_client: AsyncClient
    ) -> None:
        """Same request with the correct space_id succeeds (positive control)."""
        space, source = await self._seed_source(session)

        resp = await app_client.get(f"/api/v1/spaces/{space.id}/sources/{source.id}/detail")
        assert resp.status_code == 200, (
            f"Expected 200 for correct space_id, got {resp.status_code}: {resp.text}"
        )


# ============================================================
# Redis enqueue resilience (PR review item #6)
# ============================================================


class TestRedisEnqueueResilience:
    """When Redis is unreachable, the API must mark the task FAILED in the
    database rather than leaving it permanently stuck in QUEUED."""

    @pytest.mark.asyncio
    async def test_enqueue_failure_marks_task_failed(self, session: AsyncSession) -> None:
        """Mock Dramatiq actor to raise, then verify task transitions to FAILED."""
        from unittest.mock import patch

        from api.routers.sources import _enqueue_ingestion_task

        task_repo = IngestionTaskRepository(session)
        source_repo = SourceRepository(session)
        space_repo = SpaceRepository(session)

        space = await space_repo.create(Space(name="Redis Test"))
        source = await source_repo.create(Source(space_id=space.id, source_type=SourceType.UPLOAD))

        task = IngestionTask(
            source_id=source.id,
            operation=TaskOperation.INGEST,
        )
        await task_repo.create(task)
        await session.commit()

        # Mock the Dramatiq actor to raise a connection error
        with patch(
            "worker.ingestion_tasks.enqueue_ingestion_task",
            side_effect=ConnectionError("Redis is down"),
        ):
            await _enqueue_ingestion_task(task.id, app.state.database)

        # Task must be FAILED, not stuck in QUEUED (the fix for P1-6)
        updated = await task_repo.get(task.id)
        assert updated is not None
        assert updated.status == TaskStatus.FAILED, (
            f"Expected FAILED after enqueue error, got {updated.status}"
        )

    @pytest.mark.asyncio
    async def test_enqueue_success_leaves_task_queued(
        self, session: AsyncSession, app_client: AsyncClient
    ) -> None:
        """With a healthy Redis, the upload flow leaves the task QUEUED
        (enqueued by Dramatiq, not FAILED)."""
        space_repo = SpaceRepository(session)
        source_repo = SourceRepository(session)

        space = await space_repo.create(Space(name="Redis Health Test"))
        source = await source_repo.create(Source(space_id=space.id, source_type=SourceType.UPLOAD))
        await session.commit()

        # Upload via the API — this hits the real DB + real Redis
        resp = await app_client.post(
            f"/api/v1/spaces/{space.id}/sources/{source.id}/upload",
            files={"file": ("test.txt", b"hello world")},
        )
        assert resp.status_code == 200, f"Upload failed: {resp.status_code} {resp.text}"

        task_id = resp.json()["task_id"]

        # The task should be QUEUED (enqueued by Dramatiq, never FAILED)
        task_repo = IngestionTaskRepository(session)
        updated = await task_repo.get(task_id)
        assert updated is not None
        assert updated.status == TaskStatus.QUEUED, (
            f"Expected QUEUED after successful enqueue, got {updated.status}"
        )
