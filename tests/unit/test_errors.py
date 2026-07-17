"""Tests for error schema and exception handlers."""

import json

from api.errors import ErrorResponse, register_error_handlers
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


def test_error_response_is_pydantic_model() -> None:
    err = ErrorResponse(code="TEST_ERROR", message="Test", trace_id="abc-123")
    assert err.code == "TEST_ERROR"
    d = err.model_dump(exclude_none=True)
    assert d == {"code": "TEST_ERROR", "message": "Test", "trace_id": "abc-123"}


def test_error_response_with_details() -> None:
    err = ErrorResponse(
        code="VALIDATION_ERROR",
        message="Invalid input",
        trace_id="x",
        details={"field": "name"},
    )
    d = err.model_dump(exclude_none=True)
    assert d["details"] == {"field": "name"}


async def test_http_404_returns_unified_schema() -> None:
    """404 must return the unified ErrorResponse format, not plain {"detail": ...}."""
    app = FastAPI()
    register_error_handlers(app)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/nonexistent")

    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "HTTP_404"
    assert isinstance(body["trace_id"], str)
    assert "detail" not in body  # must not use Starlette default format


async def test_validation_error_returns_unified_schema_without_input() -> None:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/items")
    async def items(limit: int) -> dict[str, int]:
        return {"limit": limit}

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/items", params={"limit": "private-invalid-value"})

    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert "trace_id" in body
    assert "private-invalid-value" not in json.dumps(body)


async def test_app_error_returns_structured_response() -> None:
    from api.errors import AppError

    app = FastAPI()
    register_error_handlers(app)

    @app.get("/fail")
    async def fail() -> None:
        raise AppError(code="NOT_FOUND", message="Item not found", status_code=404)

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/fail")

    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "NOT_FOUND"
    assert body["message"] == "Item not found"
    assert "trace_id" in body


async def test_unhandled_exception_returns_internal_error() -> None:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/crash")
    async def crash() -> None:
        msg = "secret-db-password"
        raise RuntimeError(f"DB connection failed: {msg}")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/crash")

    assert resp.status_code == 500
    body = resp.json()
    assert body["code"] == "INTERNAL_ERROR"
    assert body["message"] == "An unexpected error occurred."
    assert "trace_id" in body
    assert "secret-db-password" not in json.dumps(body)
