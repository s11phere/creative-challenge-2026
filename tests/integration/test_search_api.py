"""Search API integration tests against isolated PostgreSQL."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from api.main import create_app
from httpx import ASGITransport, AsyncClient
from infrastructure.config import settings
from infrastructure.database import Database
from infrastructure.orm import (
    Base,
    ChunkModel,
    DocumentModel,
    DocumentVersionModel,
    SourceModel,
    SpaceModel,
)
from model_gateway import FakeModelGateway, FakeScenario
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION") != "1",
        reason="set RUN_INTEGRATION=1 with isolated PostgreSQL and Redis services",
    ),
]


@pytest.fixture
async def search_database() -> AsyncIterator[Database]:
    database = Database(settings.database_url)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
async def search_session(search_database: Database) -> AsyncIterator[AsyncSession]:
    async with search_database.session() as session:
        yield session


@pytest.fixture
async def search_client(search_database: Database) -> AsyncIterator[AsyncClient]:
    app = create_app(model_gateway=FakeModelGateway(), database=search_database)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def _seed_document(
    session: AsyncSession,
    *,
    text: str,
) -> tuple[UUID, UUID, UUID]:
    space = SpaceModel(
        name=f"search-{uuid4().hex}",
        owner_id="integration",
        retrieval_profile={
            "top_k": 5,
            "rerank_k": 5,
            "fusion_alpha": 0.5,
            "extra": {"adjacent_window": "0"},
        },
    )
    session.add(space)
    await session.flush()
    source = SourceModel(
        space_id=space.id,
        source_type="upload",
        uri=f"fixture://{uuid4().hex}.md",
    )
    session.add(source)
    await session.flush()
    document = DocumentModel(source_id=source.id, stable_key=f"{uuid4().hex}.md")
    session.add(document)
    await session.flush()
    version = DocumentVersionModel(
        document_id=document.id,
        blob_hash=uuid4().hex,
        content_hash=uuid4().hex,
        embedding_version=settings.active_embedding_identity().version,
        processing_config_hash=uuid4().hex,
        processing_config={"fixture": "search-api"},
        status="published",
    )
    session.add(version)
    await session.flush()
    session.add(
        ChunkModel(
            version_id=version.id,
            ordinal=0,
            chunk_hash=uuid4().hex,
            text=text,
            meta={"start_line": "1", "end_line": "2"},
            embedding=[0.01] * 768,
        )
    )
    document.current_version_id = version.id
    await session.commit()
    return space.id, source.id, document.id


async def test_keyword_search_returns_bounded_hits_and_hides_debug_by_default(
    search_session: AsyncSession,
    search_client: AsyncClient,
) -> None:
    space_id, source_id, document_id = await _seed_document(
        search_session,
        text="searchapineedle public evidence",
    )

    response = await search_client.post(
        f"/api/v1/spaces/{space_id}/search",
        json={
            "query": "searchapineedle",
            "mode": "keyword",
            "filters": {"source_ids": [str(source_id)], "document_ids": [str(document_id)]},
        },
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["requested_mode"] == "keyword"
    assert payload["executed_mode"] == "keyword"
    assert payload["profile_version"] == "retrieval-profile-v1"
    assert payload["keyword_index_version"] == "postgres-fts-simple-v1"
    assert payload["degraded"] is False
    assert payload["diagnostics"] is None
    assert len(payload["hits"]) == 1
    assert payload["hits"][0]["document_id"] == str(document_id)
    assert payload["hits"][0]["locators"] == [{"kind": "lines", "start": 1, "end": 2}]

    empty = await search_client.post(
        f"/api/v1/spaces/{space_id}/search",
        json={"query": "definitelynomatch", "mode": "keyword"},
    )
    assert empty.status_code == 200
    assert empty.json()["hits"] == []


async def test_debug_diagnostics_require_development_flag_and_never_include_query(
    search_session: AsyncSession,
    search_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "diagnostic-private-marker"
    space_id, _, _ = await _seed_document(search_session, text=f"{marker} evidence")
    monkeypatch.setattr(settings, "retrieval_debug_diagnostics", True)
    monkeypatch.setattr(settings, "app_env", "development")

    response = await search_client.post(
        f"/api/v1/spaces/{space_id}/search",
        json={"query": marker, "mode": "keyword"},
    )

    assert response.status_code == 200
    diagnostics = response.json()["diagnostics"]
    assert diagnostics["candidate_counts"]["keyword"] == 1
    assert marker not in str(diagnostics)

    monkeypatch.setattr(settings, "app_env", "production")
    production_response = await search_client.post(
        f"/api/v1/spaces/{space_id}/search",
        json={"query": marker, "mode": "keyword"},
    )
    assert production_response.json()["diagnostics"] is None


async def test_search_rejects_cross_space_filter_and_missing_space(
    search_session: AsyncSession,
    search_client: AsyncClient,
) -> None:
    first_space, _, _ = await _seed_document(search_session, text="firstspaceonly")
    _, other_source, _ = await _seed_document(search_session, text="otherspaceonly")

    invalid_filter = await search_client.post(
        f"/api/v1/spaces/{first_space}/search",
        json={
            "query": "firstspaceonly",
            "mode": "keyword",
            "filters": {"source_ids": [str(other_source)]},
        },
    )
    assert invalid_filter.status_code == 400
    assert invalid_filter.json()["code"] == "RETRIEVAL_INVALID_FILTER"

    missing = await search_client.post(
        f"/api/v1/spaces/{uuid4()}/search",
        json={"query": "anything", "mode": "keyword"},
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "RETRIEVAL_SPACE_NOT_FOUND"


async def test_dense_provider_failure_and_disabled_reranker_have_stable_errors(
    search_database: Database,
    search_session: AsyncSession,
) -> None:
    space_id, _, _ = await _seed_document(search_session, text="providerfailure")
    unavailable_app = create_app(
        model_gateway=FakeModelGateway(scenario=FakeScenario.UNAVAILABLE),
        database=search_database,
    )
    async with AsyncClient(
        transport=ASGITransport(app=unavailable_app), base_url="http://test"
    ) as client:
        unavailable = await client.post(
            f"/api/v1/spaces/{space_id}/search",
            json={"query": "providerfailure", "mode": "dense"},
        )
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "RETRIEVAL_EMBEDDING_UNAVAILABLE"

    regular_app = create_app(model_gateway=FakeModelGateway(), database=search_database)
    async with AsyncClient(
        transport=ASGITransport(app=regular_app), base_url="http://test"
    ) as client:
        incompatible = await client.post(
            f"/api/v1/spaces/{space_id}/search",
            json={"query": "providerfailure", "mode": "hybrid_rerank"},
        )
    assert incompatible.status_code == 409
    assert incompatible.json()["code"] == "RETRIEVAL_PROFILE_INCOMPATIBLE"


async def test_request_validation_is_bounded_and_rejects_config_overrides(
    search_client: AsyncClient,
) -> None:
    space_id = uuid4()
    for query in ("   ", "x" * 513):
        response = await search_client.post(
            f"/api/v1/spaces/{space_id}/search",
            json={"query": query, "mode": "keyword"},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "VALIDATION_ERROR"
        assert query not in response.text

    override = await search_client.post(
        f"/api/v1/spaces/{space_id}/search",
        json={"query": "safe", "mode": "keyword", "model": "arbitrary"},
    )
    assert override.status_code == 422
    assert override.json()["code"] == "VALIDATION_ERROR"


async def test_search_completion_log_does_not_contain_query_or_chunk_text(
    search_session: AsyncSession,
    search_client: AsyncClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    query_marker = "log-private-query-marker"
    chunk_marker = "log-private-chunk-marker"
    space_id, _, _ = await _seed_document(search_session, text=f"{query_marker} {chunk_marker}")

    with caplog.at_level(logging.INFO, logger="api.routers.search"):
        response = await search_client.post(
            f"/api/v1/spaces/{space_id}/search",
            json={"query": query_marker, "mode": "keyword"},
        )
    assert response.status_code == 200
    records = [
        record for record in caplog.records if record.getMessage() == "retrieval_search_completed"
    ]
    assert len(records) == 1
    serialized = repr(records[0].__dict__)
    assert query_marker not in serialized
    assert chunk_marker not in serialized
