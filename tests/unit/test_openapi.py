"""Tests for OpenAPI schema."""

from api.main import app
from httpx import ASGITransport, AsyncClient


async def test_openapi_includes_health_endpoints() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/openapi.json")

    assert resp.status_code == 200
    schema = resp.json()
    assert schema["info"]["title"] == "Agent Knowledge Repository"
    assert schema["info"]["version"] == "0.1.0"

    paths = schema["paths"]
    assert "/api/v1/health/live" in paths
    assert "/api/v1/health/ready" in paths
    assert "/api/v1/spaces/{space_id}/search" in paths
    assert "/api/v1/skills" in paths
    assert "/api/v1/skills/{skill_name}/versions" in paths
    assert "/api/v2/conversations/{conversation_id}/turns" in paths
    assert "/api/v2/runs/{run_id}" in paths
    assert "/api/v2/runs/{run_id}/events" in paths
    assert "/api/v2/runs/{run_id}/cancel" in paths

    schemas = schema["components"]["schemas"]
    assert "LiveResponse" in schemas
    assert "ReadyResponse" in schemas
    assert "ReadinessChecks" in schemas
    assert "ErrorResponse" in schemas
    assert "SearchApiRequest" in schemas
    assert "SearchApiResponse" in schemas
    assert "AssistantTurnRequest" in schemas
    assert "ConversationRunResponse" in schemas
    assert "model" in schemas["ReadinessChecks"]["properties"]
    assert "model" in schemas["ReadinessChecks"]["required"]

    live_responses = paths["/api/v1/health/live"]["get"]["responses"]
    assert live_responses["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/LiveResponse"
    }
    assert live_responses["500"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }

    ready_responses = paths["/api/v1/health/ready"]["get"]["responses"]
    assert ready_responses["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ReadyResponse"
    }
    assert ready_responses["503"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ReadyResponse"
    }

    search_operation = paths["/api/v1/spaces/{space_id}/search"]["post"]
    assert schemas["SearchApiRequest"]["properties"]["mode"]["default"] == "dense_rerank"
    assert search_operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SearchApiRequest"
    }
    assert search_operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/SearchApiResponse"
    }
    assert search_operation["responses"]["422"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }

    turn_operation = paths["/api/v2/conversations/{conversation_id}/turns"]["post"]
    assert turn_operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/AssistantTurnRequest"
    }
    assert turn_operation["responses"]["202"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ConversationRunResponse"
    }
