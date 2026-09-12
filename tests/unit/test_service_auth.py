from __future__ import annotations

import pytest
from api.main import create_app
from httpx import ASGITransport, AsyncClient
from infrastructure.config import settings
from model_gateway import FakeModelGateway


@pytest.mark.asyncio
async def test_service_auth_rejects_browser_requests_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "service_auth_required", True)
    monkeypatch.setattr(settings, "internal_service_token", "test-service-token")
    app = create_app(model_gateway=FakeModelGateway(), enable_qa_execution=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/config/limits")
    assert response.status_code == 401
    assert response.json()["code"] == "SERVICE_AUTH_REQUIRED"
    assert response.json()["trace_id"]


@pytest.mark.asyncio
async def test_service_auth_accepts_gateway_headers_and_health_is_public(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "service_auth_required", True)
    monkeypatch.setattr(settings, "internal_service_token", "test-service-token")
    app = create_app(model_gateway=FakeModelGateway(), enable_qa_execution=False)
    headers = {
        "X-Internal-Service-Token": "test-service-token",
        "X-App-Scoped-User-Id": "member:abc123",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        accepted = await client.get("/api/v1/config/limits", headers=headers)
        live = await client.get("/api/v1/health/live")
    assert accepted.status_code == 200
    assert live.status_code == 200


@pytest.mark.asyncio
async def test_public_mode_hides_local_agent_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "service_auth_required", False)
    monkeypatch.setattr(settings, "public_mode", True)
    app = create_app(model_gateway=FakeModelGateway(), enable_qa_execution=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/skills/personal")
    assert response.status_code == 404
    assert response.json()["code"] == "CAPABILITY_NOT_EXPOSED"
    assert response.json()["trace_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/v2/commands",
        "/api/v2/conversations/00000000-0000-0000-0000-000000000001/turns",
        "/api/v3/runs/00000000-0000-0000-0000-000000000001/events",
        "/api/v4/conversations/00000000-0000-0000-0000-000000000001/exam-interactions",
        "/api/v1/skills",
        "/api/v1/spaces/00000000-0000-0000-0000-000000000001/feedback",
    ],
)
async def test_public_mode_blocks_non_yizhi_capability_paths(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    monkeypatch.setattr(settings, "service_auth_required", False)
    monkeypatch.setattr(settings, "public_mode", True)
    app = create_app(model_gateway=FakeModelGateway(), enable_qa_execution=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(path)
    assert response.status_code == 404
    assert response.json()["code"] == "CAPABILITY_NOT_EXPOSED"
