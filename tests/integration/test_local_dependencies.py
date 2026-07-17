"""Integration checks against explicitly enabled local dependencies."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
import redis.asyncio as aioredis
from api.main import app
from httpx import ASGITransport, AsyncClient
from infrastructure.config import settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION") != "1",
        reason="set RUN_INTEGRATION=1 with isolated PostgreSQL and Redis services",
    ),
]


async def test_migrated_postgresql_has_pgvector_and_single_head() -> None:
    engine = create_async_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            vector_version = await connection.scalar(
                text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            )
            migration_heads = (
                (await connection.execute(text("SELECT version_num FROM alembic_version")))
                .scalars()
                .all()
            )
    finally:
        await engine.dispose()

    assert vector_version
    assert migration_heads == ["328a3caa2960"]


async def test_redis_round_trip_uses_ephemeral_control_metadata() -> None:
    client = aioredis.from_url(settings.redis_url, decode_responses=True, socket_timeout=3)
    key = f"stage1:integration:{uuid4()}"
    try:
        assert await client.set(key, "ready", ex=30)
        assert await client.get(key) == "ready"
    finally:
        await client.delete(key)
        await client.aclose()


async def test_readiness_reports_real_local_dependencies() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "checks": {
            "postgresql": {"healthy": True, "code": "POSTGRESQL_OK"},
            "redis": {"healthy": True, "code": "REDIS_OK"},
            "model": {"healthy": True, "code": "MODEL_FAKE_READY"},
        },
    }
    assert response.headers["x-trace-id"]
    assert response.headers["x-request-id"]
