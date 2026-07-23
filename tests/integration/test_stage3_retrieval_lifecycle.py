"""Stage 3 ingestion-to-retrieval lifecycle against isolated PostgreSQL."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from api.main import create_app
from application.ingestion import IngestionConfig, IngestionOrchestrator, SourceRegistrationService
from domain.models import (
    IngestionTask,
    RetrievalProfile,
    Source,
    SourceType,
    Space,
    TaskOperation,
)
from httpx import ASGITransport, AsyncClient
from infrastructure.blob_store import LocalFileBlobStore
from infrastructure.chunkers import StructureChunker
from infrastructure.config import settings
from infrastructure.database import Database
from infrastructure.orm import Base
from infrastructure.parsers import MarkdownParser
from infrastructure.repositories import (
    ChunkRepository,
    DocumentRepository,
    DocumentVersionRepository,
    IngestionTaskRepository,
    SourceRepository,
    SpaceRepository,
)
from model_gateway import CapabilityAlias, EmbeddingRequest, FakeModelGateway

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION") != "1",
        reason="set RUN_INTEGRATION=1 with isolated PostgreSQL and Redis services",
    ),
]


class _GatewayTextEmbedder:
    def __init__(self, gateway: FakeModelGateway) -> None:
        self._gateway = gateway

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        response = await self._gateway.embed(
            EmbeddingRequest(texts=texts),
            capability=CapabilityAlias.EMBEDDING_ZH,
        )
        return response.vectors


@pytest.fixture
async def lifecycle_database() -> AsyncIterator[Database]:
    database = Database(settings.database_url)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield database
    finally:
        await database.dispose()


def _orchestrator(
    database_session,
    gateway: FakeModelGateway,
    blob_store: LocalFileBlobStore,
) -> IngestionOrchestrator:
    return IngestionOrchestrator(
        source_repo=SourceRepository(database_session),
        document_repo=DocumentRepository(database_session),
        version_repo=DocumentVersionRepository(database_session),
        chunk_repo=ChunkRepository(database_session),
        task_repo=IngestionTaskRepository(database_session),
        parser=MarkdownParser(),
        chunker=StructureChunker(),
        text_embedder=_GatewayTextEmbedder(gateway),
        blob_store=blob_store,
    )


async def _search(
    client: AsyncClient,
    space_id: UUID,
    query: str,
    mode: str,
) -> dict[str, object]:
    response = await client.post(
        f"/api/v1/spaces/{space_id}/search",
        json={"query": query, "mode": mode},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_ingest_publish_switch_delete_and_search_lifecycle(
    lifecycle_database: Database,
    tmp_path: Path,
) -> None:
    gateway = FakeModelGateway()
    blob_store = LocalFileBlobStore(tmp_path / "blobs")
    identity = settings.active_embedding_identity()

    async with lifecycle_database.session() as session:
        space = await SpaceRepository(session).create(
            Space(
                name=f"stage3-lifecycle-{uuid4().hex}",
                retrieval_profile=RetrievalProfile(
                    top_k=1,
                    rerank_k=1,
                    extra={"adjacent_window": "0", "reranker_enabled": "true"},
                ),
            )
        )
        source = await SourceRepository(session).create(
            Source(
                space_id=space.id,
                source_type=SourceType.UPLOAD,
                uri="fixture://stage3-lifecycle.md",
            )
        )
        registration = SourceRegistrationService(
            source_repo=SourceRepository(session),
            document_repo=DocumentRepository(session),
            version_repo=DocumentVersionRepository(session),
        )
        first = await registration.register_file(
            source,
            b"# Stage 3\n\nfirstlifecyclemarker published evidence.\n",
            blob_store,
            file_stable_key="stage3-lifecycle.md",
            file_path="stage3-lifecycle.md",
        )
        assert first.version_id is not None
        first_task = await IngestionTaskRepository(session).create(
            IngestionTask(
                source_id=source.id,
                operation=TaskOperation.INGEST,
                target_version_id=first.version_id,
            )
        )
        await session.commit()

        orchestrator = _orchestrator(session, gateway, blob_store)
        await orchestrator.run_pipeline(
            first_task,
            config=IngestionConfig(embedding_identity=identity),
        )
        await session.commit()

    app = create_app(model_gateway=gateway, database=lifecycle_database)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for mode in ("keyword", "dense", "hybrid", "hybrid_rerank"):
            payload = await _search(client, space.id, "firstlifecyclemarker", mode)
            assert len(payload["hits"]) == 1
            assert payload["hits"][0]["version_id"] == str(first.version_id)

        async with lifecycle_database.session() as session:
            registration = SourceRegistrationService(
                source_repo=SourceRepository(session),
                document_repo=DocumentRepository(session),
                version_repo=DocumentVersionRepository(session),
            )
            second = await registration.register_file(
                source,
                b"# Stage 3\n\nsecondlifecyclemarker rebuilt evidence.\n",
                blob_store,
                file_stable_key="stage3-lifecycle.md",
                file_path="stage3-lifecycle.md",
            )
            assert second.version_id is not None
            assert second.document.id == first.document.id
            current_before_publish = await DocumentRepository(session).get(first.document.id)
            assert current_before_publish is not None
            assert current_before_publish.current_version_id == first.version_id

            second_task = await IngestionTaskRepository(session).create(
                IngestionTask(
                    source_id=source.id,
                    operation=TaskOperation.INGEST,
                    target_version_id=second.version_id,
                )
            )
            await session.commit()
            orchestrator = _orchestrator(session, gateway, blob_store)
            await orchestrator.run_pipeline(
                second_task,
                config=IngestionConfig(embedding_identity=identity),
            )
            await session.commit()

        old_payload = await _search(client, space.id, "firstlifecyclemarker", "keyword")
        assert old_payload["hits"] == []
        new_payload = await _search(client, space.id, "secondlifecyclemarker", "hybrid")
        assert len(new_payload["hits"]) == 1
        assert new_payload["hits"][0]["version_id"] == str(second.version_id)

        async with lifecycle_database.session() as session:
            document = await DocumentRepository(session).get(first.document.id)
            assert document is not None
            orchestrator = _orchestrator(session, gateway, blob_store)
            cleanup_task = await orchestrator.delete_document(document)
            assert cleanup_task is not None
            await session.commit()

        withdrawn = await _search(client, space.id, "secondlifecyclemarker", "keyword")
        assert withdrawn["hits"] == []

        async with lifecycle_database.session() as session:
            cleanup_task = await IngestionTaskRepository(session).get(cleanup_task.id)
            assert cleanup_task is not None
            orchestrator = _orchestrator(session, gateway, blob_store)
            await orchestrator.run_cleanup(cleanup_task)
            await session.commit()

        after_cleanup = await _search(client, space.id, "secondlifecyclemarker", "keyword")
        assert after_cleanup["hits"] == []
