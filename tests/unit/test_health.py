"""Tests for health check endpoints."""

from typing import Any

import redis.asyncio as aioredis
from api.main import app, create_app
from httpx import ASGITransport, AsyncClient
from model_gateway import GatewayConfig, ModelProvider, create_model_gateway
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
    assert body["checks"]["model"] == {"healthy": True, "code": "MODEL_FAKE_READY"}
    # Must not leak host or port in failure details
    assert "localhost" not in str(body["checks"]).lower()


async def test_ready_returns_success_when_dependencies_are_available(
    monkeypatch: MonkeyPatch,
) -> None:
    class FakeRedisConnection:
        async def ping(self) -> bool:
            return True

        async def aclose(self) -> None:
            pass

    async def fake_postgres_available(*, timeout_seconds: float) -> bool:
        return timeout_seconds == 3

    def fake_redis_from_url(*_args: Any, **_kwargs: Any) -> FakeRedisConnection:
        return FakeRedisConnection()

    monkeypatch.setattr(app.state.database, "is_available", fake_postgres_available)
    monkeypatch.setattr(aioredis, "from_url", fake_redis_from_url)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/health/ready")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["checks"]["postgresql"] == {"healthy": True, "code": "POSTGRESQL_OK"}
    assert body["checks"]["redis"] == {"healthy": True, "code": "REDIS_OK"}
    assert body["checks"]["model"] == {"healthy": True, "code": "MODEL_FAKE_READY"}


async def test_model_unavailable_does_not_degrade_local_readiness(
    monkeypatch: MonkeyPatch,
) -> None:
    unavailable_gateway = create_model_gateway(GatewayConfig(provider=ModelProvider.DISABLED))
    test_app = create_app(unavailable_gateway)

    class FakeRedisConnection:
        async def ping(self) -> bool:
            return True

        async def aclose(self) -> None:
            pass

    async def fake_postgres_available(*, timeout_seconds: float) -> bool:
        return timeout_seconds == 3

    def fake_redis_from_url(*_args: Any, **_kwargs: Any) -> FakeRedisConnection:
        return FakeRedisConnection()

    monkeypatch.setattr(test_app.state.database, "is_available", fake_postgres_available)
    monkeypatch.setattr(aioredis, "from_url", fake_redis_from_url)

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    assert response.json()["checks"]["model"] == {
        "healthy": False,
        "code": "MODEL_DISABLED",
    }
