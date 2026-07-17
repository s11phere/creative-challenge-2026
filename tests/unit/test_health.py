"""Tests for health check endpoints."""

from typing import Any

import redis.asyncio as aioredis
from api import main
from api.main import app
from httpx import ASGITransport, AsyncClient
from pytest import MonkeyPatch


async def test_live_returns_alive() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "alive"}


async def test_ready_returns_degraded_without_dependencies() -> None:
    """Without PostgreSQL/Redis, ready must return 503 with stable machine codes."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/health/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert "checks" in body
    pg = body["checks"]["postgresql"]
    redis = body["checks"]["redis"]
    # Both checks ran (concurrent), both report failure with machine codes
    assert pg["healthy"] is False
    assert pg["code"] == "POSTGRESQL_UNREACHABLE"
    assert redis["healthy"] is False
    assert redis["code"] == "REDIS_UNREACHABLE"
    # Must not leak host or port in failure details
    assert "localhost" not in str(body["checks"]).lower()


async def test_ready_returns_success_when_dependencies_are_available(
    monkeypatch: MonkeyPatch,
) -> None:
    class FakePostgresConnection:
        async def close(self) -> None:
            pass

    class FakeRedisConnection:
        async def ping(self) -> bool:
            return True

        async def aclose(self) -> None:
            pass

    async def fake_postgres_connect(**_kwargs: Any) -> FakePostgresConnection:
        return FakePostgresConnection()

    def fake_redis_from_url(*_args: Any, **_kwargs: Any) -> FakeRedisConnection:
        return FakeRedisConnection()

    monkeypatch.setattr(main.asyncpg, "connect", fake_postgres_connect)
    monkeypatch.setattr(aioredis, "from_url", fake_redis_from_url)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/health/ready")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["checks"]["postgresql"] == {"healthy": True, "code": "POSTGRESQL_OK"}
    assert body["checks"]["redis"] == {"healthy": True, "code": "REDIS_OK"}
