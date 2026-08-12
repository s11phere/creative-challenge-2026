"""PostgreSQL FTS and pgvector retrieval constrained to published documents."""

from __future__ import annotations

import json
import math
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from time import perf_counter
from typing import Any, cast
from uuid import UUID

from domain.models import DocumentStatus
from domain.retrieval import (
    CandidateBatch,
    CandidateChannel,
    ContextCandidateQuery,
    DenseCandidateQuery,
    KeywordCandidateQuery,
    KeywordQueryAnalysis,
    LocatorKind,
    RetrievalCandidate,
    SearchFilters,
    SearchLocator,
)
from sqlalchemy import Select, and_, func, literal, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from ..orm import ChunkModel, DocumentModel, DocumentVersionModel, SourceModel


class DenseSearchMode(StrEnum):
    """Execution path selected for normal dense candidate retrieval."""

    EXACT = "exact"
    IVFFLAT = "ivfflat"


@dataclass(frozen=True)
class DensePathComparison:
    """Bounded exact/approximate comparison for operational diagnostics."""

    exact: CandidateBatch
    ivfflat: CandidateBatch
    overlap_count: int
    overlap_ratio: float
    ivfflat_lists: int
    ivfflat_probes: int
    ivfflat_shortlist_k: int
    statistics_analyzed: bool
    ivfflat_index_used: bool
    exact_plan: str
    ivfflat_plan: str


class PostgresRetrievalStore:
    """RetrievalStore implementation with one non-bypassable published boundary."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        dense_mode: DenseSearchMode = DenseSearchMode.EXACT,
        ivfflat_probes: int = 10,
    ) -> None:
        if ivfflat_probes < 1:
            raise ValueError("ivfflat_probes must be positive")
        self._session = session
        self._dense_mode = dense_mode
        self._ivfflat_probes = ivfflat_probes

    async def keyword_candidates(self, query: KeywordCandidateQuery) -> CandidateBatch:
        tsquery = func.websearch_to_tsquery("simple", _disjunctive_fts_query(query.analysis))
        rank_score = func.ts_rank_cd(ChunkModel.search_vector, tsquery).label("score")
        statement = (
            self._published_candidates(query.space_id, query.filters)
            .add_columns(rank_score)
            .where(ChunkModel.search_vector.bool_op("@@")(tsquery))
            .where(_is_direct_retrieval_content())
            .order_by(rank_score.desc(), ChunkModel.id.asc())
            .limit(query.limit)
        )
        for literal_term in query.analysis.literal_terms:
            statement = statement.where(
                func.strpos(func.lower(ChunkModel.text), literal_term.lower()) > 0
            )
        return await self._execute_candidates(
            statement,
            channel=CandidateChannel.KEYWORD,
            index_version="postgres-fts-simple-v1",
        )

    async def dense_candidates(self, query: DenseCandidateQuery) -> CandidateBatch:
        if self._dense_mode is DenseSearchMode.IVFFLAT:
            statement = self._ivfflat_statement(query)
            async with self._planner_settings({"ivfflat.probes": str(self._ivfflat_probes)}):
                return await self._execute_candidates(
                    statement,
                    channel=CandidateChannel.DENSE,
                    index_version=self._ivfflat_index_version,
                )

        statement = self._dense_statement(query)
        async with self._planner_settings({"enable_indexscan": "off", "enable_bitmapscan": "off"}):
            return await self._execute_candidates(
                statement,
                channel=CandidateChannel.DENSE,
                index_version="pgvector-cosine-exact-v1",
            )

    async def context_candidates(
        self, query: ContextCandidateQuery
    ) -> tuple[RetrievalCandidate, ...]:
        ordinals_by_version: dict[UUID, set[int]] = {}
        seed_ids = {seed.chunk_id for seed in query.seeds}
        for seed in query.seeds:
            ordinals = ordinals_by_version.setdefault(seed.version_id, set())
            start = max(0, seed.ordinal - query.adjacent_window)
            ordinals.update(range(start, seed.ordinal + query.adjacent_window + 1))
            parent_ordinal = _metadata_int(seed.metadata, "parent_ordinal")
            if parent_ordinal is not None:
                ordinals.add(parent_ordinal)

        version_windows = tuple(
            and_(
                ChunkModel.version_id == version_id,
                ChunkModel.ordinal.in_(sorted(ordinals)),
            )
            for version_id, ordinals in sorted(
                ordinals_by_version.items(), key=lambda item: str(item[0])
            )
        )
        statement = (
            self._published_candidates(query.space_id, query.filters)
            .add_columns(literal(0.0).label("score"))
            .where(or_(*version_windows), ChunkModel.id.not_in(seed_ids))
            .order_by(DocumentModel.id.asc(), ChunkModel.ordinal.asc(), ChunkModel.id.asc())
        )
        batch = await self._execute_candidates(
            statement,
            channel=CandidateChannel.KEYWORD,
            index_version="postgres-published-context-v1",
        )
        return batch.candidates

    async def compare_dense_paths(self, query: DenseCandidateQuery) -> DensePathComparison:
        """Compare the exact baseline with a forced IVFFlat execution path."""
        exact_statement = self._dense_statement(query)
        async with self._planner_settings({"enable_indexscan": "off", "enable_bitmapscan": "off"}):
            exact_plan = await self._explain(exact_statement)
            exact = await self._execute_candidates(
                exact_statement,
                channel=CandidateChannel.DENSE,
                index_version="pgvector-cosine-exact-v1",
            )

        ivfflat_statement = self._ivfflat_statement(query)
        async with self._planner_settings(
            {"enable_seqscan": "off", "ivfflat.probes": str(self._ivfflat_probes)}
        ):
            ivfflat_plan = await self._explain(ivfflat_statement)
            ivfflat = await self._execute_candidates(
                ivfflat_statement,
                channel=CandidateChannel.DENSE,
                index_version=self._ivfflat_index_version,
            )

        exact_ids = {candidate.chunk_id for candidate in exact.candidates}
        ivfflat_ids = {candidate.chunk_id for candidate in ivfflat.candidates}
        overlap_count = len(exact_ids & ivfflat_ids)
        overlap_ratio = overlap_count / len(exact_ids) if exact_ids else 1.0
        return DensePathComparison(
            exact=exact,
            ivfflat=ivfflat,
            overlap_count=overlap_count,
            overlap_ratio=overlap_ratio,
            ivfflat_lists=100,
            ivfflat_probes=self._ivfflat_probes,
            ivfflat_shortlist_k=query.limit * 10,
            statistics_analyzed=await self._statistics_analyzed(),
            ivfflat_index_used="idx_chunks_embedding" in ivfflat_plan,
            exact_plan=exact_plan,
            ivfflat_plan=ivfflat_plan,
        )

    @property
    def _ivfflat_index_version(self) -> str:
        return f"pgvector-ivfflat-lists100-probes{self._ivfflat_probes}-v1"

    def _published_candidates(
        self,
        space_id: UUID,
        filters: SearchFilters,
    ) -> Select[tuple[Any, ...]]:
        """Return the only SQL candidate boundary used by both retrieval channels."""
        return self._apply_published_scope(
            select(
                ChunkModel.id.label("chunk_id"),
                ChunkModel.version_id.label("version_id"),
                DocumentModel.id.label("document_id"),
                SourceModel.id.label("source_id"),
                SourceModel.uri.label("source_uri"),
                SourceModel.name.label("source_name"),
                DocumentModel.stable_key.label("stable_key"),
                ChunkModel.text.label("chunk_text"),
                ChunkModel.chunk_hash.label("chunk_hash"),
                ChunkModel.meta.label("chunk_meta"),
                ChunkModel.ordinal.label("chunk_ordinal"),
            ).select_from(ChunkModel),
            chunk_version_id=ChunkModel.version_id,
            space_id=space_id,
            filters=filters,
        )

    def _apply_published_scope(
        self,
        statement: Select[tuple[Any, ...]],
        *,
        chunk_version_id: Any,
        space_id: UUID,
        filters: SearchFilters,
    ) -> Select[tuple[Any, ...]]:
        statement = (
            statement.join(DocumentVersionModel, chunk_version_id == DocumentVersionModel.id)
            .join(
                DocumentModel,
                and_(
                    DocumentVersionModel.document_id == DocumentModel.id,
                    DocumentModel.current_version_id == DocumentVersionModel.id,
                ),
            )
            .join(SourceModel, DocumentModel.source_id == SourceModel.id)
            .where(
                SourceModel.space_id == space_id,
                DocumentModel.deleted_at.is_(None),
                DocumentVersionModel.status == DocumentStatus.PUBLISHED.value,
            )
        )
        if filters.source_ids:
            statement = statement.where(SourceModel.id.in_(filters.source_ids))
        if filters.document_ids:
            statement = statement.where(DocumentModel.id.in_(filters.document_ids))
        if filters.version_ids:
            statement = statement.where(DocumentVersionModel.id.in_(filters.version_ids))
        return statement

    def _dense_statement(self, query: DenseCandidateQuery) -> Select[tuple[Any, ...]]:
        distance = ChunkModel.embedding.cosine_distance(list(query.query_vector))
        score = (1.0 - distance).label("score")
        return (
            self._published_candidates(query.space_id, query.filters)
            .add_columns(score)
            .where(
                DocumentVersionModel.embedding_version == query.embedding_version,
                ChunkModel.embedding.is_not(None),
                distance.is_not(None),
                _is_direct_retrieval_content(),
            )
            .order_by(distance.asc(), ChunkModel.id.asc())
            .limit(query.limit)
        )

    def _ivfflat_statement(self, query: DenseCandidateQuery) -> Select[tuple[Any, ...]]:
        distance = ChunkModel.embedding.cosine_distance(list(query.query_vector))
        shortlist = (
            select(
                ChunkModel.id.label("chunk_id"),
                ChunkModel.version_id.label("version_id"),
                ChunkModel.text.label("chunk_text"),
                ChunkModel.chunk_hash.label("chunk_hash"),
                ChunkModel.meta.label("chunk_meta"),
                ChunkModel.ordinal.label("chunk_ordinal"),
                (1.0 - distance).label("score"),
            )
            .where(
                ChunkModel.embedding.is_not(None),
                distance.is_not(None),
                _is_direct_retrieval_content(),
            )
            .order_by(distance.asc())
            .limit(query.limit * 10)
            .cte("ivfflat_shortlist")
            .prefix_with("MATERIALIZED")
        )
        statement = select(
            shortlist.c.chunk_id,
            shortlist.c.version_id,
            DocumentModel.id.label("document_id"),
            SourceModel.id.label("source_id"),
            SourceModel.uri.label("source_uri"),
            SourceModel.name.label("source_name"),
            DocumentModel.stable_key.label("stable_key"),
            shortlist.c.chunk_text,
            shortlist.c.chunk_hash,
            shortlist.c.chunk_meta,
            shortlist.c.chunk_ordinal,
            shortlist.c.score,
        ).select_from(shortlist)
        return (
            self._apply_published_scope(
                statement,
                chunk_version_id=shortlist.c.version_id,
                space_id=query.space_id,
                filters=query.filters,
            )
            .where(DocumentVersionModel.embedding_version == query.embedding_version)
            .order_by(shortlist.c.score.desc(), shortlist.c.chunk_id.asc())
            .limit(query.limit)
        )

    async def _execute_candidates(
        self,
        statement: Select[tuple[Any, ...]],
        *,
        channel: CandidateChannel,
        index_version: str,
    ) -> CandidateBatch:
        started = perf_counter()
        result = await self._session.execute(statement)
        elapsed_ms = (perf_counter() - started) * 1000
        scored_rows: list[tuple[float, Mapping[str, Any]]] = []
        for row in result.mappings():
            score = float(row["score"])
            if not math.isfinite(score):
                continue
            scored_rows.append((score, row))
        scored_rows.sort(key=lambda item: (-item[0], str(item[1]["chunk_id"])))

        candidates: list[RetrievalCandidate] = []
        for rank, (score, row) in enumerate(scored_rows, start=1):
            candidates.append(
                RetrievalCandidate(
                    chunk_id=row["chunk_id"],
                    version_id=row["version_id"],
                    document_id=row["document_id"],
                    source_id=row["source_id"],
                    source_key=str(
                        row["source_name"]
                        or row["source_uri"]
                        or row["stable_key"]
                        or row["source_id"]
                    ),
                    text=str(row["chunk_text"]),
                    chunk_hash=str(row["chunk_hash"]),
                    locators=locators_from_meta(row["chunk_meta"]),
                    channel=channel,
                    rank=rank,
                    score=score,
                    ordinal=int(row["chunk_ordinal"]),
                    metadata=_metadata(row["chunk_meta"]),
                )
            )
        return CandidateBatch(
            channel=channel,
            candidates=tuple(candidates),
            index_version=index_version,
            latency_ms=elapsed_ms,
        )

    async def _explain(self, statement: Select[tuple[Any, ...]]) -> str:
        bind = self._session.get_bind()
        schema_translate_map = bind.get_execution_options().get("schema_translate_map")
        compiled = statement.compile(
            dialect=bind.dialect,
            schema_translate_map=schema_translate_map,
            render_schema_translate=bool(schema_translate_map),
            compile_kwargs={"literal_binds": True},
        )
        result = await self._session.execute(text(f"EXPLAIN (FORMAT JSON) {compiled}"))
        return json.dumps(result.scalar_one(), sort_keys=True)

    async def _statistics_analyzed(self) -> bool:
        bind = self._session.get_bind()
        schema_translate_map = bind.get_execution_options().get("schema_translate_map")
        schema_name = "public"
        if isinstance(schema_translate_map, Mapping):
            translated_schema = schema_translate_map.get(None)
            if isinstance(translated_schema, str):
                schema_name = translated_schema
        statement = text(
            "SELECT pg_stat_get_last_analyze_time(c.oid) IS NOT NULL "
            "FROM pg_class AS c "
            "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
            "WHERE n.nspname = :schema_name AND c.relname = 'chunks'"
        )
        return bool(await self._session.scalar(statement, {"schema_name": schema_name}))

    @asynccontextmanager
    async def _planner_settings(self, values: Mapping[str, str]) -> AsyncIterator[None]:
        previous: list[tuple[str, str]] = []
        try:
            for name, value in values.items():
                previous_value = await self._session.scalar(
                    select(func.current_setting(name, True))
                )
                if previous_value is None and name.startswith("ivfflat."):
                    # pgvector registers IVFFlat GUCs when its library is first loaded in
                    # each PostgreSQL backend. Planner settings run before the vector SQL.
                    await self._session.execute(text("SELECT '[0]'::vector"))
                    previous_value = await self._session.scalar(
                        select(func.current_setting(name, True))
                    )
                if previous_value is None:
                    raise RuntimeError(f"PostgreSQL planner setting is unavailable: {name}")
                previous.append((name, previous_value))
                await self._session.execute(select(func.set_config(name, value, True)))
            yield
        finally:
            for name, value in reversed(previous):
                await self._session.execute(select(func.set_config(name, value, True)))


def locators_from_meta(raw_meta: object) -> tuple[SearchLocator, ...]:
    meta: Mapping[str, object] = raw_meta if isinstance(raw_meta, Mapping) else {}
    locators: list[SearchLocator] = []
    line_locator = _locator(meta, "start_line", "end_line", LocatorKind.LINES)
    if line_locator is not None:
        locators.append(line_locator)
    page_locator = _locator(meta, "start_page", "end_page", LocatorKind.PDF_PAGE)
    if page_locator is not None:
        locators.append(page_locator)
    return tuple(locators)


def _locators(raw_meta: object) -> tuple[SearchLocator, ...]:
    """Backward-compatible private alias for existing retrieval tests."""
    return locators_from_meta(raw_meta)


_FTS_TERM = re.compile(r"[A-Za-z0-9]+|[\u3400-\u9fff]+")
_MAX_FTS_TERMS = 32


def _disjunctive_fts_query(analysis: KeywordQueryAnalysis) -> str:
    """Build a bounded websearch query that recalls any normalized lexical term."""
    terms = tuple(dict.fromkeys(_FTS_TERM.findall(analysis.normalized_query)))[:_MAX_FTS_TERMS]
    return " OR ".join(terms) if terms else analysis.normalized_query


def _metadata(raw_meta: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw_meta, Mapping):
        return ()
    return tuple(
        sorted(
            (str(key), str(value))
            for key, value in raw_meta.items()
            if isinstance(value, (str, int))
        )
    )


def _metadata_int(metadata: tuple[tuple[str, str], ...], key: str) -> int | None:
    raw_value = dict(metadata).get(key)
    if raw_value is None:
        return None
    try:
        value = int(raw_value)
    except ValueError:
        return None
    return value if value >= 0 else None


def _is_direct_retrieval_content() -> ColumnElement[bool]:
    """Exclude TOC centroids before candidate LIMIT while retaining stored chunks."""
    node_type = ChunkModel.meta["node_type"].as_string()
    return cast(ColumnElement[bool], node_type.is_distinct_from("table_of_contents"))


def _locator(
    meta: Mapping[str, object],
    start_key: str,
    end_key: str,
    kind: LocatorKind,
) -> SearchLocator | None:
    try:
        raw_start = meta[start_key]
        raw_end = meta[end_key]
        if not isinstance(raw_start, (str, int)) or isinstance(raw_start, bool):
            return None
        if not isinstance(raw_end, (str, int)) or isinstance(raw_end, bool):
            return None
        start = int(raw_start)
        end = int(raw_end)
        return SearchLocator(kind=kind, start=start, end=end)
    except (KeyError, TypeError, ValueError):
        return None
