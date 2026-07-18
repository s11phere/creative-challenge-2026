"""Integration tests for the full data model — migration + CRUD.

Requires ``RUN_INTEGRATION=1`` and isolated PostgreSQL and Redis services.
"""

from __future__ import annotations

import os
from uuid import UUID

import pytest
from domain.models import (
    Chunk,
    Document,
    DocumentStatus,
    DocumentVersion,
    IngestionTask,
    RetrievalProfile,
    Source,
    SourceType,
    Space,
    TaskStage,
)
from infrastructure.config import settings
from infrastructure.orm import Base
from infrastructure.repositories import (
    ChunkRepository,
    DocumentRepository,
    DocumentVersionRepository,
    IngestionTaskRepository,
    SourceRepository,
    SpaceRepository,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION") != "1",
        reason="set RUN_INTEGRATION=1 with isolated PostgreSQL and Redis services",
    ),
]


@pytest.fixture
async def session() -> AsyncSession:
    """Create a clean schema for each test and tear it down afterwards."""
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with maker() as sess:
        yield sess
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


# ============================================================
# Space CRUD
# ============================================================


class TestSpaceRepository:
    async def test_create_and_get(self, session: AsyncSession) -> None:
        repo = SpaceRepository(session)
        space = Space(name="Test Space", owner_id="user-1")
        created = await repo.create(space)
        assert created.id == space.id
        assert created.name == "Test Space"

        fetched = await repo.get(space.id)
        assert fetched is not None
        assert fetched.name == "Test Space"
        assert isinstance(fetched.retrieval_profile, RetrievalProfile)

    async def test_list(self, session: AsyncSession) -> None:
        repo = SpaceRepository(session)
        s1 = await repo.create(Space(name="A"))
        s2 = await repo.create(Space(name="B"))
        spaces = await repo.list()
        ids = {s.id for s in spaces}
        assert s1.id in ids
        assert s2.id in ids

    async def test_update(self, session: AsyncSession) -> None:
        repo = SpaceRepository(session)
        space = await repo.create(Space(name="Original"))
        updated = Space(
            id=space.id,
            name="Updated",
            owner_id=space.owner_id,
            retrieval_profile=RetrievalProfile(chunk_size=128),
            created_at=space.created_at,
        )
        result = await repo.update(updated)
        assert result.name == "Updated"
        assert result.retrieval_profile.chunk_size == 128

    async def test_delete(self, session: AsyncSession) -> None:
        repo = SpaceRepository(session)
        space = await repo.create(Space(name="To Delete"))
        await repo.delete(space.id)
        assert await repo.get(space.id) is None


# ============================================================
# Source CRUD
# ============================================================


class TestSourceRepository:
    async def test_create_and_get(self, session: AsyncSession) -> None:
        space_repo = SpaceRepository(session)
        space = await space_repo.create(Space(name="Source Space"))

        repo = SourceRepository(session)
        source = Source(
            space_id=space.id,
            source_type=SourceType.UPLOAD,
            uri="/tmp/test.txt",
        )
        created = await repo.create(source)
        assert created.id == source.id
        assert created.source_type == SourceType.UPLOAD
        assert created.uri == "/tmp/test.txt"

        fetched = await repo.get(source.id)
        assert fetched is not None
        assert fetched.space_id == space.id

    async def test_get_by_space(self, session: AsyncSession) -> None:
        space_repo = SpaceRepository(session)
        space = await space_repo.create(Space(name="Multi Source"))

        repo = SourceRepository(session)
        await repo.create(Source(space_id=space.id, uri="a.txt"))
        await repo.create(Source(space_id=space.id, uri="b.txt"))

        sources = await repo.get_by_space(space.id)
        assert len(sources) == 2
        assert {s.uri for s in sources} == {"a.txt", "b.txt"}

    async def test_cascade_on_space_delete(self, session: AsyncSession) -> None:
        space_repo = SpaceRepository(session)
        space = await space_repo.create(Space(name="Cascade Test"))

        repo = SourceRepository(session)
        await repo.create(Source(space_id=space.id, uri="cascade.txt"))

        await space_repo.delete(space.id)
        remaining = await repo.get_by_space(space.id)
        assert remaining == []


# ============================================================
# Document + DocumentVersion + Chunk CRUD
# ============================================================


class TestDocumentChain:
    async def test_full_chain(self, session: AsyncSession) -> None:
        # Create Space → Source
        space = await SpaceRepository(session).create(Space(name="Doc Space"))
        source = await SourceRepository(session).create(Source(space_id=space.id, uri="doc.md"))

        # Create Document
        doc_repo = DocumentRepository(session)
        doc = await doc_repo.create(Document(source_id=source.id, stable_key="sha256-abc"))
        assert doc.stable_key == "sha256-abc"

        # Create DocumentVersion
        ver_repo = DocumentVersionRepository(session)
        version = await ver_repo.create(
            DocumentVersion(
                document_id=doc.id,
                content_hash="sha256-abc",
                status=DocumentStatus.PARSED,
                file_path="/tmp/doc.md",
            )
        )
        assert version.status == DocumentStatus.PARSED

        # Update document's current_version_id
        updated_doc = Document(
            id=doc.id,
            source_id=doc.source_id,
            stable_key=doc.stable_key,
            current_version_id=version.id,
            created_at=doc.created_at,
        )
        await doc_repo.update(updated_doc)

        # Create chunks
        chunk_repo = ChunkRepository(session)
        chunks = await chunk_repo.create_batch(
            [
                Chunk(version_id=version.id, ordinal=0, text="# Intro", meta={"page": "1"}),
                Chunk(version_id=version.id, ordinal=1, text="Content body", meta={"page": "1"}),
            ]
        )
        assert len(chunks) == 2

        # Verify relationships
        fetched_doc = await doc_repo.get(doc.id)
        assert fetched_doc is not None
        assert fetched_doc.current_version_id == version.id

        fetched_versions = await ver_repo.get_by_document(doc.id)
        assert len(fetched_versions) == 1
        assert fetched_versions[0].file_path == "/tmp/doc.md"

        fetched_chunks = await chunk_repo.get_by_version(version.id)
        assert len(fetched_chunks) == 2
        assert fetched_chunks[0].text == "# Intro"

        # Delete chunks by version
        deleted = await chunk_repo.delete_by_version(version.id)
        assert deleted == 2
        assert await chunk_repo.get_by_version(version.id) == []

    async def test_document_by_stable_key(self, session: AsyncSession) -> None:
        space = await SpaceRepository(session).create(Space(name="Dedup"))
        source = await SourceRepository(session).create(Source(space_id=space.id, uri="dedup.md"))
        doc_repo = DocumentRepository(session)
        doc = await doc_repo.create(Document(source_id=source.id, stable_key="unique-key"))

        fetched = await doc_repo.get_by_stable_key("unique-key")
        assert fetched is not None
        assert fetched.id == doc.id

        missing = await doc_repo.get_by_stable_key("nonexistent")
        assert missing is None

    async def test_latest_version(self, session: AsyncSession) -> None:
        space = await SpaceRepository(session).create(Space(name="Versions"))
        source = await SourceRepository(session).create(Source(space_id=space.id, uri="v.md"))
        doc = await DocumentRepository(session).create(
            Document(source_id=source.id, stable_key="v-key")
        )
        ver_repo = DocumentVersionRepository(session)

        await ver_repo.create(DocumentVersion(document_id=doc.id, content_hash="v1"))
        await ver_repo.create(DocumentVersion(document_id=doc.id, content_hash="v2"))

        latest = await ver_repo.get_latest(doc.id)
        assert latest is not None
        assert latest.content_hash == "v2"

        empty = await ver_repo.get_latest(UUID(int=0))
        assert empty is None


# ============================================================
# IngestionTask CRUD
# ============================================================


class TestIngestionTaskRepository:
    async def test_create_and_update(self, session: AsyncSession) -> None:
        space = await SpaceRepository(session).create(Space(name="Task Space"))
        source = await SourceRepository(session).create(Source(space_id=space.id, uri="task.md"))

        repo = IngestionTaskRepository(session)
        task = await repo.create(IngestionTask(source_id=source.id, stage=TaskStage.DISCOVER))
        assert task.stage == TaskStage.DISCOVER
        assert task.progress == 0.0

        # Update progress
        updated = IngestionTask(
            id=task.id,
            source_id=task.source_id,
            stage=TaskStage.PARSE,
            progress=0.5,
            retry_count=task.retry_count,
            created_at=task.created_at,
        )
        result = await repo.update(updated)
        assert result.stage == TaskStage.PARSE
        assert result.progress == 0.5

    async def test_get_by_source(self, session: AsyncSession) -> None:
        space = await SpaceRepository(session).create(Space(name="Multi Task"))
        source = await SourceRepository(session).create(Source(space_id=space.id, uri="multi.md"))
        repo = IngestionTaskRepository(session)

        t1 = await repo.create(IngestionTask(source_id=source.id))
        t2 = await repo.create(IngestionTask(source_id=source.id))

        tasks = await repo.get_by_source(source.id)
        assert len(tasks) == 2
        assert {t.id for t in tasks} == {t1.id, t2.id}

    async def test_task_not_found(self, session: AsyncSession) -> None:
        repo = IngestionTaskRepository(session)
        result = await repo.get(UUID("00000000-0000-4000-8000-000000000001"))
        assert result is None
