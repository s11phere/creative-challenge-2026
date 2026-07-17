"""Tests for API request correlation headers."""

from __future__ import annotations

import re
from uuid import uuid4

from api.main import create_app
from httpx import ASGITransport, AsyncClient

TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


async def test_valid_trace_and_request_ids_are_returned() -> None:
    app = create_app()
    trace_id = uuid4()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/health/live",
            headers={"X-Trace-ID": str(trace_id), "X-Request-ID": "request-123"},
        )

    assert response.headers["X-Trace-ID"] == trace_id.hex
    assert response.headers["X-Request-ID"] == "request-123"


async def test_invalid_correlation_headers_are_replaced() -> None:
    app = create_app()
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/health/live",
            headers={"X-Trace-ID": "x" * 10_000, "X-Request-ID": "unsafe value"},
        )

    assert TRACE_ID_PATTERN.fullmatch(response.headers["X-Trace-ID"])
    assert response.headers["X-Request-ID"] != "unsafe value"


async def test_error_body_and_response_header_share_trace_id() -> None:
    app = create_app()
    trace_id = uuid4()
    transport = ASGITransport(app=app, raise_app_exceptions=False)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/missing",
            headers={"X-Trace-ID": str(trace_id)},
        )

    assert response.status_code == 404
    assert response.json()["trace_id"] == trace_id.hex
    assert response.headers["X-Trace-ID"] == trace_id.hex
