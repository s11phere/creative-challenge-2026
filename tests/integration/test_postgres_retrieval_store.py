"""PostgreSQL retrieval boundary, FTS, and pgvector integration checks."""

from __future__ import annotations

import math
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from domain.models import DocumentStatus
from domain.retrieval import (
    ContextCandidateQuery,
    DenseCandidateQuery,
    KeywordCandidateQuery,
    SearchFilters,
)
from infrastructure.config import settings
from infrastructure.orm import (
    EMBEDDING_DIMENSIONS,
    Base,
    ChunkModel,
    DocumentModel,
    DocumentVersionModel,
    SourceModel,
    SpaceModel,
)
from infrastructure.retrieval import DenseSearchMode, PostgresRetrievalStore
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION") != "1",
        reason="set RUN_INTEGRATION=1 with isolated PostgreSQL and Redis services",
    ),
]

_EMPTY_FILTERS = SearchFilters()


@dataclass(frozen=True)
class RetrievalDatabase:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    schema_name: str


@dataclass(frozen=True)
class SeededChunk:
    space_id: UUID
    source_id: UUID
    document_id: UUID
    version_id: UUID
    chunk_id: UUID


@pytest.fixture
async def retrieval_database() -> AsyncIterator[RetrievalDatabase]:
    engine = create_async_engine(settings.database_url)
    schema_name = f"retrieval_{uuid4().hex}"
    quoted_schema = engine.dialect.identifier_preparer.quote(schema_name)
    translated_engine = engine.execution_options(schema_translate_map={None: schema_name})
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f"CREATE SCHEMA {quoted_schema}"))
        async with translated_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        yield RetrievalDatabase(
            engine=translated_engine,
            sessions=async_sessionmaker(
                translated_engine,
                class_=AsyncSession,
                expire_on_commit=False,
            ),
            schema_name=schema_name,
        )
    finally:
        await translated_engine.dispose()
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP SCHEMA IF EXISTS {quoted_schema} CASCADE"))
        await engine.dispose()


def _vector(angle: float = 0.0) -> list[float]:
    return [math.cos(angle), math.sin(angle), *([0.0] * (EMBEDDING_DIMENSIONS - 2))]


async def _add_space(session: AsyncSession, label: str) -> SpaceModel:
    space = SpaceModel(name=label, owner_id="integration", retrieval_profile={})
    session.add(space)
    await session.flush()
    return space


async def _add_version(
    session: AsyncSession,
    *,
    document: DocumentModel,
    label: str,
    text_value: str,
    embedding: list[float],
    embedding_version: str = "embedding-v1",
    status: str = DocumentStatus.PUBLISHED.value,
    make_current: bool = False,
) -> SeededChunk:
    version = DocumentVersionModel(
        document_id=document.id,
        blob_hash=f"blob-{label}",
        content_hash=f"content-{label}",
        embedding_version=embedding_version,
        processing_config_hash=f"config-{label}",
        processing_config={"fixture": label},
        status=status,
    )
    session.add(version)
    await session.flush()
    chunk = ChunkModel(
        version_id=version.id,
        ordinal=0,
        chunk_hash=f"chunk-{label}",
        text=text_value,
        meta={
            "start_line": "2",
            "end_line": "4",
            "start_page": "1",
            "end_page": "1",
        },
        embedding=embedding,
    )
    session.add(chunk)
    await session.flush()
    if make_current:
        document.current_version_id = version.id
        await session.flush()
    source = await session.get(SourceModel, document.source_id)
    assert source is not None
    return SeededChunk(
        space_id=source.space_id,
        source_id=source.id,
        document_id=document.id,
        version_id=version.id,
        chunk_id=chunk.id,
    )


async def _add_document(
    session: AsyncSession,
    *,
    space_id: UUID,
    label: str,
    text_value: str,
    embedding: list[float] | None = None,
    embedding_version: str = "embedding-v1",
    status: str = DocumentStatus.PUBLISHED.value,
    deleted: bool = False,
) -> tuple[DocumentModel, SeededChunk]:
    source = SourceModel(space_id=space_id, source_type="upload", uri=f"fixture://{label}")
    session.add(source)
    await session.flush()
    document = DocumentModel(
        source_id=source.id,
        stable_key=f"{label}.md",
        deleted_at=datetime.now(UTC) if deleted else None,
    )
    session.add(document)
    await session.flush()
    seeded = await _add_version(
        session,
        document=document,
        label=label,
        text_value=text_value,
        embedding=embedding or _vector(),
        embedding_version=embedding_version,
        status=status,
        make_current=True,
    )
    return document, seeded


def _keyword_query(
    term: str,
    space_id: UUID,
    *,
    filters: SearchFilters = _EMPTY_FILTERS,
) -> KeywordCandidateQuery:
    return KeywordCandidateQuery(query=term, space_id=space_id, filters=filters, limit=10)


def _dense_query(space_id: UUID, *, filters: SearchFilters = _EMPTY_FILTERS) -> DenseCandidateQuery:
    return DenseCandidateQuery(
        query_vector=tuple(_vector()),
        space_id=space_id,
        filters=filters,
        limit=10,
        embedding_version="embedding-v1",
    )


async def test_keyword_and_dense_share_the_published_candidate_boundary(
    retrieval_database: RetrievalDatabase,
) -> None:
    async with retrieval_database.sessions() as session:
        target_space = await _add_space(session, "target")
        other_space = await _add_space(session, "other")
        visible_document, visible = await _add_document(
            session,
            space_id=target_space.id,
            label="visible",
            text_value="visibleterm current published",
        )
        await _add_version(
            session,
            document=visible_document,
            label="visible-old",
            text_value="oldterm published but not current",
            embedding=_vector(0.01),
        )
        await _add_document(
            session,
            space_id=target_space.id,
            label="unpublished",
            text_value="unpublishedterm candidate",
            status=DocumentStatus.EMBEDDED.value,
        )
        await _add_document(
            session,
            space_id=target_space.id,
            label="deleted",
            text_value="deletedterm tombstone",
            deleted=True,
        )
        await _add_document(
            session,
            space_id=target_space.id,
            label="wrong-embedding",
            text_value="wrongembeddingterm current published",
            embedding_version="embedding-v2",
        )
        _, other = await _add_document(
            session,
            space_id=other_space.id,
            label="other-space",
            text_value="visibleterm crossspaceterm",
        )
        await session.commit()

        store = PostgresRetrievalStore(session)
        keyword = await store.keyword_candidates(_keyword_query("visibleterm", target_space.id))
        assert [candidate.chunk_id for candidate in keyword.candidates] == [visible.chunk_id]
        assert [(locator.start, locator.end) for locator in keyword.candidates[0].locators] == [
            (2, 4),
            (1, 1),
        ]
        for hidden_term in ("oldterm", "unpublishedterm", "deletedterm", "crossspaceterm"):
            hidden = await store.keyword_candidates(_keyword_query(hidden_term, target_space.id))
            assert hidden.candidates == ()

        dense = await store.dense_candidates(_dense_query(target_space.id))
        assert [candidate.chunk_id for candidate in dense.candidates] == [visible.chunk_id]
        assert dense.index_version == "pgvector-cosine-exact-v1"
        assert dense.candidates[0].rank == 1
        assert math.isclose(dense.candidates[0].score, 1.0, rel_tol=1e-6)

        ivfflat_store = PostgresRetrievalStore(
            session,
            dense_mode=DenseSearchMode.IVFFLAT,
            ivfflat_probes=10,
        )
        approximate = await ivfflat_store.dense_candidates(_dense_query(target_space.id))
        assert [candidate.chunk_id for candidate in approximate.candidates] == [visible.chunk_id]
        assert approximate.index_version == "pgvector-ivfflat-lists100-probes10-v1"

        matching_filter = SearchFilters(
            source_ids=frozenset({visible.source_id}),
            document_ids=frozenset({visible.document_id}),
        )
        filtered = await store.keyword_candidates(
            _keyword_query("visibleterm", target_space.id, filters=matching_filter)
        )
        assert [candidate.chunk_id for candidate in filtered.candidates] == [visible.chunk_id]

        cross_space_filter = SearchFilters(source_ids=frozenset({other.source_id}))
        narrowed_to_empty = await store.keyword_candidates(
            _keyword_query("visibleterm", target_space.id, filters=cross_space_filter)
        )
        assert narrowed_to_empty.candidates == ()


async def test_context_expansion_stays_in_seed_version_document_and_space(
    retrieval_database: RetrievalDatabase,
) -> None:
    async with retrieval_database.sessions() as session:
        target_space = await _add_space(session, "target-context")
        other_space = await _add_space(session, "other-context")
        document, parent = await _add_document(
            session,
            space_id=target_space.id,
            label="context-document",
            text_value="parent context",
        )
        parent_chunk = await session.get(ChunkModel, parent.chunk_id)
        assert parent_chunk is not None
        parent_chunk.meta = {
            **parent_chunk.meta,
            "node_type": "table_of_contents",
        }
        seed_chunk = ChunkModel(
            version_id=parent.version_id,
            ordinal=1,
            chunk_hash="context-seed",
            text="seedterm matched child",
            meta={"start_line": "5", "end_line": "7", "parent_ordinal": "0"},
            embedding=_vector(0.01),
        )
        adjacent_chunk = ChunkModel(
            version_id=parent.version_id,
            ordinal=2,
            chunk_hash="context-adjacent",
            text="adjacent child",
            meta={"start_line": "8", "end_line": "10"},
            embedding=_vector(0.02),
        )
        session.add_all((seed_chunk, adjacent_chunk))
        await _add_version(
            session,
            document=document,
            label="context-old",
            text_value="old version neighbor",
            embedding=_vector(0.03),
        )
        await _add_document(
            session,
            space_id=target_space.id,
            label="other-document-context",
            text_value="other document neighbor",
        )
        await _add_document(
            session,
            space_id=other_space.id,
            label="other-space-context",
            text_value="other space neighbor",
        )
        await session.commit()

        store = PostgresRetrievalStore(session)
        seed_batch = await store.keyword_candidates(_keyword_query("seedterm", target_space.id))
        assert len(seed_batch.candidates) == 1
        seed = seed_batch.candidates[0]
        assert seed.ordinal == 1
        assert dict(seed.metadata)["parent_ordinal"] == "0"

        contexts = await store.context_candidates(
            ContextCandidateQuery(
                space_id=target_space.id,
                filters=SearchFilters(document_ids=frozenset({document.id})),
                seeds=(seed,),
                adjacent_window=1,
            )
        )
        assert {candidate.chunk_id for candidate in contexts} == {
            parent.chunk_id,
            adjacent_chunk.id,
        }
        assert {candidate.version_id for candidate in contexts} == {parent.version_id}
        assert {candidate.document_id for candidate in contexts} == {document.id}


async def test_direct_retrieval_excludes_toc_before_candidate_limit(
    retrieval_database: RetrievalDatabase,
) -> None:
    async with retrieval_database.sessions() as session:
        space = await _add_space(session, "toc-filter")
        _, content = await _add_document(
            session,
            space_id=space.id,
            label="toc-filter-document",
            text_value="sharedterm answer content",
            embedding=_vector(0.1),
        )
        toc = ChunkModel(
            version_id=content.version_id,
            ordinal=1,
            chunk_hash="toc-centroid",
            text="sharedterm sharedterm sharedterm section index",
            meta={
                "start_line": "1",
                "end_line": "3",
                "node_type": "table_of_contents",
            },
            embedding=_vector(),
        )
        session.add(toc)
        await session.commit()

        keyword_query = KeywordCandidateQuery(
            query="sharedterm",
            space_id=space.id,
            filters=_EMPTY_FILTERS,
            limit=1,
        )
        dense_query = DenseCandidateQuery(
            query_vector=tuple(_vector()),
            space_id=space.id,
            filters=_EMPTY_FILTERS,
            limit=1,
            embedding_version="embedding-v1",
        )
        exact_store = PostgresRetrievalStore(session)
        keyword = await exact_store.keyword_candidates(keyword_query)
        dense = await exact_store.dense_candidates(dense_query)

        assert [candidate.chunk_id for candidate in keyword.candidates] == [content.chunk_id]
        assert [candidate.chunk_id for candidate in dense.candidates] == [content.chunk_id]

        ivfflat_store = PostgresRetrievalStore(
            session,
            dense_mode=DenseSearchMode.IVFFLAT,
            ivfflat_probes=10,
        )
        approximate = await ivfflat_store.dense_candidates(dense_query)
        assert [candidate.chunk_id for candidate in approximate.candidates] == [content.chunk_id]


async def test_keyword_baseline_covers_language_code_and_stable_ranking(
    retrieval_database: RetrievalDatabase,
) -> None:
    async with retrieval_database.sessions() as session:
        space = await _add_space(session, "keyword-slices")
        _, english = await _add_document(
            session,
            space_id=space.id,
            label="english-natural",
            text_value="Transaction retry budget and deadline handling.",
        )
        _, chinese = await _add_document(
            session,
            space_id=space.id,
            label="chinese-natural",
            text_value=(
                "\u5e76\u53d1\u63a7\u5236 "
                "\u4f7f\u7528\u4e92\u65a5\u9501\u4fdd\u62a4\u5171\u4eab\u72b6\u6001\u3002"
            ),
        )
        _, mixed_code = await _add_document(
            session,
            space_id=space.id,
            label="mixed-code",
            text_value=(
                "\u6a21\u677f\u5bb9\u5668 \u4f7f\u7528 C++ std::vector and "
                "snake_case_identifier for examples."
            ),
        )
        await _add_document(
            session,
            space_id=space.id,
            label="plain-c",
            text_value="C language arrays and pointers.",
        )
        _, first_tie = await _add_document(
            session,
            space_id=space.id,
            label="stable-tie-a",
            text_value="stabletie identical ranking text",
        )
        _, second_tie = await _add_document(
            session,
            space_id=space.id,
            label="stable-tie-b",
            text_value="stabletie identical ranking text",
        )
        await session.commit()

        store = PostgresRetrievalStore(session)
        english_result = await store.keyword_candidates(
            _keyword_query("  transaction\nretry  ", space.id)
        )
        assert [candidate.chunk_id for candidate in english_result.candidates] == [english.chunk_id]

        partial_english_result = await store.keyword_candidates(
            _keyword_query("transaction retry absentterm", space.id)
        )
        assert [candidate.chunk_id for candidate in partial_english_result.candidates] == [
            english.chunk_id
        ]

        chinese_result = await store.keyword_candidates(
            _keyword_query("\u5e76\u53d1\u63a7\u5236", space.id)
        )
        assert [candidate.chunk_id for candidate in chinese_result.candidates] == [chinese.chunk_id]

        mixed_result = await store.keyword_candidates(
            _keyword_query("\u4f7f\u7528 std::vector", space.id)
        )
        assert [candidate.chunk_id for candidate in mixed_result.candidates] == [
            mixed_code.chunk_id
        ]

        cpp_result = await store.keyword_candidates(_keyword_query("\uff23\uff0b\uff0b", space.id))
        assert [candidate.chunk_id for candidate in cpp_result.candidates] == [mixed_code.chunk_id]
        identifier_result = await store.keyword_candidates(
            _keyword_query("snake_case_identifier", space.id)
        )
        assert [candidate.chunk_id for candidate in identifier_result.candidates] == [
            mixed_code.chunk_id
        ]

        expected_tie_order = sorted(
            (first_tie.chunk_id, second_tie.chunk_id),
            key=str,
        )
        repeated_orders = []
        for _ in range(3):
            result = await store.keyword_candidates(_keyword_query("stabletie", space.id))
            repeated_orders.append([candidate.chunk_id for candidate in result.candidates])
            assert all(candidate.score > 0 for candidate in result.candidates)
            assert [candidate.rank for candidate in result.candidates] == [1, 2]
        assert repeated_orders == [expected_tie_order] * 3


async def test_uncommitted_version_switch_keeps_old_version_visible(
    retrieval_database: RetrievalDatabase,
) -> None:
    async with retrieval_database.sessions() as seed_session:
        space = await _add_space(seed_session, "switch")
        document, old = await _add_document(
            seed_session,
            space_id=space.id,
            label="switch-old",
            text_value="switchterm old version",
        )
        new = await _add_version(
            seed_session,
            document=document,
            label="switch-new",
            text_value="switchterm new version",
            embedding=_vector(0.02),
        )
        await seed_session.commit()

    async with retrieval_database.sessions() as reader, retrieval_database.sessions() as writer:
        store = PostgresRetrievalStore(reader)
        before = await store.keyword_candidates(_keyword_query("switchterm", space.id))
        assert [candidate.chunk_id for candidate in before.candidates] == [old.chunk_id]

        writable_document = await writer.get(DocumentModel, document.id)
        assert writable_document is not None
        writable_document.current_version_id = new.version_id
        await writer.flush()

        while_uncommitted = await store.keyword_candidates(_keyword_query("switchterm", space.id))
        assert [candidate.chunk_id for candidate in while_uncommitted.candidates] == [old.chunk_id]

        await writer.commit()
        after_commit = await store.keyword_candidates(_keyword_query("switchterm", space.id))
        assert [candidate.chunk_id for candidate in after_commit.candidates] == [new.chunk_id]


async def test_exact_and_ivfflat_paths_report_overlap_plans_and_statistics(
    retrieval_database: RetrievalDatabase,
) -> None:
    async with retrieval_database.sessions() as session:
        space = await _add_space(session, "vector-comparison")
        source = SourceModel(space_id=space.id, source_type="upload", uri="fixture://vectors")
        session.add(source)
        await session.flush()

        documents: list[DocumentModel] = []
        versions: list[DocumentVersionModel] = []
        chunks: list[ChunkModel] = []
        for ordinal in range(120):
            document = DocumentModel(
                id=uuid4(),
                source_id=source.id,
                stable_key=f"vector-{ordinal}.md",
            )
            version = DocumentVersionModel(
                id=uuid4(),
                document_id=document.id,
                blob_hash=f"{ordinal:064x}",
                content_hash=f"{ordinal + 1000:064x}",
                embedding_version="embedding-v1",
                processing_config_hash=f"{ordinal + 2000:064x}",
                status=DocumentStatus.PUBLISHED.value,
            )
            chunk = ChunkModel(
                id=uuid4(),
                version_id=version.id,
                ordinal=0,
                chunk_hash=f"{ordinal + 3000:064x}",
                text=f"vector fixture {ordinal}",
                meta={},
                embedding=_vector(ordinal * 0.005),
            )
            documents.append(document)
            versions.append(version)
            chunks.append(chunk)
        session.add_all(documents)
        await session.flush()
        session.add_all(versions)
        await session.flush()
        session.add_all(chunks)
        await session.flush()
        for document, version in zip(documents, versions, strict=True):
            document.current_version_id = version.id
        await session.commit()

    quoted_schema = retrieval_database.engine.dialect.identifier_preparer.quote(
        retrieval_database.schema_name
    )
    async with retrieval_database.engine.begin() as connection:
        await connection.execute(text(f"ANALYZE {quoted_schema}.chunks"))

    async with retrieval_database.sessions() as session:
        store = PostgresRetrievalStore(
            session,
            dense_mode=DenseSearchMode.EXACT,
            ivfflat_probes=10,
        )
        comparison = await store.compare_dense_paths(_dense_query(space.id))

    assert len(comparison.exact.candidates) == 10
    assert len(comparison.ivfflat.candidates) == 10
    assert comparison.overlap_count == 10
    assert comparison.overlap_ratio == 1.0
    assert comparison.ivfflat_lists == 100
    assert comparison.ivfflat_probes == 10
    assert comparison.ivfflat_shortlist_k == 100
    assert comparison.statistics_analyzed is True
    assert comparison.ivfflat_index_used is True
    assert "Seq Scan" in comparison.exact_plan
    assert "idx_chunks_embedding" in comparison.ivfflat_plan
