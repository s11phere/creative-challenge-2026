"""Integration checks against explicitly enabled local dependencies."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
import redis.asyncio as aioredis
from alembic.config import Config
from alembic.script import ScriptDirectory
from api.main import app
from httpx import ASGITransport, AsyncClient
from infrastructure.config import settings
from infrastructure.orm import Base
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

_ALEMBIC_CFG = Config("alembic.ini")
_EXPECTED_HEADS = ScriptDirectory.from_config(_ALEMBIC_CFG).get_heads()

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
    assert set(migration_heads) == set(_EXPECTED_HEADS)


async def test_orm_metadata_creates_cosine_vector_index_in_isolated_schema() -> None:
    engine = create_async_engine(settings.database_url)
    schema_name = f"orm_metadata_{uuid4().hex}"
    quoted_schema = engine.dialect.identifier_preparer.quote(schema_name)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"CREATE SCHEMA {quoted_schema}"))
            translated = await connection.execution_options(
                schema_translate_map={None: schema_name}
            )
            await translated.run_sync(Base.metadata.create_all)
            index_definition = await translated.scalar(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE schemaname = :schema_name AND indexname = 'idx_chunks_embedding'"
                ),
                {"schema_name": schema_name},
            )
    finally:
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE"))
        await engine.dispose()

    assert index_definition is not None
    assert "vector_cosine_ops" in index_definition


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
