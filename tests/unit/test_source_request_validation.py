"""Request-boundary tests for the source API.

These cover transport-level validation that must not depend on PostgreSQL:
an unsupported ``source_type`` has to be a field-level 422 instead of a
``ValueError`` escaping from ``SourceType(...)``, and an oversized upload must
be rejected before it is buffered in memory.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from api.main import create_app
from httpx import ASGITransport, AsyncClient
from infrastructure.config import settings
from model_gateway import FakeModelGateway


def _client() -> AsyncClient:
    app = create_app(model_gateway=FakeModelGateway(), enable_qa_execution=False)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_unknown_source_type_is_a_validation_error() -> None:
    async with _client() as client:
        response = await client.post(
            f"/api/v1/spaces/{uuid4()}/sources",
            json={"source_type": "bogus", "uri": "", "name": "x"},
        )

    assert response.status_code == 422, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_folder_source_without_name_still_returns_422() -> None:
    async with _client() as client:
        response = await client.post(
            f"/api/v1/spaces/{uuid4()}/sources",
            json={"source_type": "folder", "uri": "", "name": "  "},
        )

    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_oversized_upload_is_rejected_before_buffering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "max_upload_size_mb", 1)
    payload = b"x" * (2 * 1024 * 1024)

    async with _client() as client:
        response = await client.post(
            f"/api/v1/spaces/{uuid4()}/sources/{uuid4()}/upload",
            files={"file": ("big.md", payload)},
        )

    assert response.status_code == 413, response.text
    assert "maximum size" in response.json()["message"]


@pytest.mark.asyncio
async def test_upload_below_the_limit_is_not_rejected_by_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The multipart envelope must not push an allowed file over the limit."""

    monkeypatch.setattr(settings, "max_upload_size_mb", 1)
    payload = b"y" * (1024 * 1024 - 4096)

    async with _client() as client:
        response = await client.post(
            f"/api/v1/spaces/{uuid4()}/sources/{uuid4()}/upload",
            files={"file": ("ok.md", payload)},
        )

    # The guard passes; the request then fails on the missing Space instead.
    assert response.status_code != 413, response.text
