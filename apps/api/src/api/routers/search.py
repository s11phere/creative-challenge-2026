"""Space-scoped retrieval API backed by the Search application service."""

from __future__ import annotations

import logging
from typing import Annotated, Any
from uuid import UUID

from application.retrieval import (
    GatewayQueryTextEmbedder,
    GatewayReranker,
    QueryEmbeddingConfig,
    QueryEmbeddingService,
    RetrievalProfileResolver,
    SearchService,
)
from domain.retrieval import (
    MAX_SEARCH_QUERY_CHARS,
    CandidateCounts,
    RetrievalError,
    RetrievalErrorCode,
    RetrievalMode,
    SearchDiagnostics,
    SearchFilters,
    SearchHit,
    SearchLocator,
    SearchRequest,
    StageTiming,
    normalize_search_query,
)
from fastapi import APIRouter, Request
from infrastructure.config import settings
from infrastructure.database import Database
from infrastructure.repositories import DocumentRepository, SourceRepository, SpaceRepository
from infrastructure.retrieval import PostgresRetrievalStore
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..errors import AppError, ErrorResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")


class SearchFiltersRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_ids: list[UUID] = Field(default_factory=list, max_length=100)
    document_ids: list[UUID] = Field(default_factory=list, max_length=100)


class SearchApiRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: Annotated[str, Field(min_length=1, max_length=MAX_SEARCH_QUERY_CHARS)]
    mode: RetrievalMode = RetrievalMode.HYBRID_RERANK
    filters: SearchFiltersRequest = Field(default_factory=SearchFiltersRequest)

    @field_validator("query")
    @classmethod
    def validate_query(cls, value: str) -> str:
        return normalize_search_query(value)


class SearchLocatorResponse(BaseModel):
    kind: str
    start: int
    end: int


class SearchHitResponse(BaseModel):
    chunk_id: UUID
    version_id: UUID
    document_id: UUID
    source_id: UUID
    source_key: str
    text: str
    chunk_hash: str
    locators: list[SearchLocatorResponse]
    final_rank: int
    keyword_rank: int | None = None
    keyword_score: float | None = None
    dense_rank: int | None = None
    dense_score: float | None = None
    fused_rank: int | None = None
    fused_score: float | None = None
    rerank_rank: int | None = None
    rerank_score: float | None = None
    context_only: bool


class CandidateCountsResponse(BaseModel):
    keyword: int
    dense: int
    fused: int
    reranked: int
    final: int


class StageTimingResponse(BaseModel):
    stage: str
    latency_ms: float


class SearchDiagnosticsResponse(BaseModel):
    requested_mode: RetrievalMode
    executed_mode: RetrievalMode
    profile_version: str
    embedding_version: str | None
    reranker_version: str | None
    keyword_index_version: str | None
    dense_index_version: str | None
    candidate_counts: CandidateCountsResponse
    stage_timings: list[StageTimingResponse]
    keyword_language_slice: str | None
    keyword_query_kind: str | None
    keyword_literal_term_count: int
    dense_language_slice: str | None
    dense_query_kind: str | None
    degraded: bool
    degradation_reasons: list[str]
    filter_reasons: list[str]
    source_filter_count: int
    document_filter_count: int
    context_only_count: int


class SearchApiResponse(BaseModel):
    requested_mode: RetrievalMode
    executed_mode: RetrievalMode
    profile_version: str
    embedding_version: str | None
    reranker_version: str | None
    keyword_index_version: str | None
    dense_index_version: str | None
    degraded: bool
    degradation_reasons: list[str]
    hits: list[SearchHitResponse]
    diagnostics: SearchDiagnosticsResponse | None = None


_ERROR_STATUS = {
    RetrievalErrorCode.SPACE_NOT_FOUND: 404,
    RetrievalErrorCode.INVALID_FILTER: 400,
    RetrievalErrorCode.PROVIDER_POLICY_DENIED: 403,
    RetrievalErrorCode.EMBEDDING_DIMENSION_MISMATCH: 409,
    RetrievalErrorCode.PROFILE_INCOMPATIBLE: 409,
    RetrievalErrorCode.EMBEDDING_UNAVAILABLE: 503,
    RetrievalErrorCode.RERANKER_UNAVAILABLE: 503,
    RetrievalErrorCode.RETRIEVAL_TIMEOUT: 504,
}

_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorResponse, "description": "Stable retrieval error"}
    for status in sorted(set(_ERROR_STATUS.values()))
}
_ERROR_RESPONSES.update(
    {
        422: {"model": ErrorResponse, "description": "Request validation failed"},
        500: {"model": ErrorResponse, "description": "Unexpected server error"},
    }
)


@router.post(
    "/spaces/{space_id}/search",
    response_model=SearchApiResponse,
    responses=_ERROR_RESPONSES,
)
async def search_space(
    space_id: UUID,
    body: SearchApiRequest,
    request: Request,
) -> SearchApiResponse:
    """Execute one bounded retrieval request inside a single Space."""
    database: Database = request.app.state.database
    gateway = request.app.state.model_gateway
    try:
        identity = settings.active_embedding_identity(
            allow_unconfigured=body.mode is RetrievalMode.KEYWORD
        )
    except ValueError as exc:
        logger.warning(
            "retrieval_search_failed",
            extra={
                "retrieval_mode": body.mode.value,
                "error_code": RetrievalErrorCode.PROFILE_INCOMPATIBLE.value,
                "retryable": False,
            },
        )
        raise AppError(
            code=RetrievalErrorCode.PROFILE_INCOMPATIBLE.value,
            message="The active embedding identity is not configured.",
            status_code=409,
        ) from exc
    search_request = SearchRequest(
        query=body.query,
        space_id=space_id,
        mode=body.mode,
        filters=SearchFilters(
            source_ids=frozenset(body.filters.source_ids),
            document_ids=frozenset(body.filters.document_ids),
        ),
    )

    try:
        async with database.session() as session:
            space_repo = SpaceRepository(session)
            space = await space_repo.get(space_id)
            if space is None:
                raise RetrievalError(
                    RetrievalErrorCode.SPACE_NOT_FOUND,
                    "The requested Space does not exist.",
                )
            profile = RetrievalProfileResolver(
                embedding_identity=identity,
                timeout_seconds=settings.retrieval_timeout_seconds,
            ).resolve(space)
            service = SearchService(
                space_repo=space_repo,
                source_repo=SourceRepository(session),
                document_repo=DocumentRepository(session),
                retrieval_store=PostgresRetrievalStore(session),
                query_embedder=QueryEmbeddingService(
                    GatewayQueryTextEmbedder(gateway),
                    config=QueryEmbeddingConfig(
                        identity=identity,
                        query_prefix=settings.query_embedding_prefix(),
                        timeout_seconds=settings.retrieval_timeout_seconds,
                    ),
                ),
                reranker=GatewayReranker(gateway),
            )
            result = await service.search(search_request, profile)
    except RetrievalError as exc:
        logger.warning(
            "retrieval_search_failed",
            extra={
                "retrieval_mode": body.mode.value,
                "error_code": exc.code.value,
                "retryable": exc.retryable,
            },
        )
        raise AppError(
            code=exc.code.value,
            message=str(exc),
            status_code=_ERROR_STATUS[exc.code],
        ) from exc

    diagnostics = result.diagnostics
    logger.info(
        "retrieval_search_completed",
        extra={
            "requested_mode": diagnostics.requested_mode.value,
            "executed_mode": diagnostics.executed_mode.value,
            "profile_version": diagnostics.profile_version,
            "embedding_version": diagnostics.embedding_version,
            "reranker_version": diagnostics.reranker_version,
            "keyword_index_version": diagnostics.keyword_index_version,
            "dense_index_version": diagnostics.dense_index_version,
            "keyword_candidates": diagnostics.candidate_counts.keyword,
            "dense_candidates": diagnostics.candidate_counts.dense,
            "fused_candidates": diagnostics.candidate_counts.fused,
            "reranked_candidates": diagnostics.candidate_counts.reranked,
            "final_candidates": diagnostics.candidate_counts.final,
            "stage_timings_ms": {
                timing.stage: round(timing.latency_ms, 3) for timing in diagnostics.stage_timings
            },
            "degraded": diagnostics.degraded,
            "degradation_reasons": [reason.value for reason in diagnostics.degradation_reasons],
        },
    )
    debug_enabled = settings.app_env != "production" and settings.retrieval_debug_diagnostics
    return _response(result.hits, diagnostics, include_diagnostics=debug_enabled)


def _response(
    hits: tuple[SearchHit, ...],
    diagnostics: SearchDiagnostics,
    *,
    include_diagnostics: bool,
) -> SearchApiResponse:
    return SearchApiResponse(
        requested_mode=diagnostics.requested_mode,
        executed_mode=diagnostics.executed_mode,
        profile_version=diagnostics.profile_version,
        embedding_version=diagnostics.embedding_version,
        reranker_version=diagnostics.reranker_version,
        keyword_index_version=diagnostics.keyword_index_version,
        dense_index_version=diagnostics.dense_index_version,
        degraded=diagnostics.degraded,
        degradation_reasons=[reason.value for reason in diagnostics.degradation_reasons],
        hits=[_hit_response(hit) for hit in hits],
        diagnostics=(_diagnostics_response(diagnostics) if include_diagnostics else None),
    )


def _hit_response(hit: SearchHit) -> SearchHitResponse:
    return SearchHitResponse(
        chunk_id=hit.chunk_id,
        version_id=hit.version_id,
        document_id=hit.document_id,
        source_id=hit.source_id,
        source_key=hit.source_key,
        text=hit.text,
        chunk_hash=hit.chunk_hash,
        locators=[_locator_response(locator) for locator in hit.locators],
        final_rank=hit.final_rank,
        keyword_rank=hit.keyword_rank,
        keyword_score=hit.keyword_score,
        dense_rank=hit.dense_rank,
        dense_score=hit.dense_score,
        fused_rank=hit.fused_rank,
        fused_score=hit.fused_score,
        rerank_rank=hit.rerank_rank,
        rerank_score=hit.rerank_score,
        context_only=hit.context_only,
    )


def _locator_response(locator: SearchLocator) -> SearchLocatorResponse:
    return SearchLocatorResponse(
        kind=locator.kind.value,
        start=locator.start,
        end=locator.end,
    )


def _diagnostics_response(diagnostics: SearchDiagnostics) -> SearchDiagnosticsResponse:
    return SearchDiagnosticsResponse(
        requested_mode=diagnostics.requested_mode,
        executed_mode=diagnostics.executed_mode,
        profile_version=diagnostics.profile_version,
        embedding_version=diagnostics.embedding_version,
        reranker_version=diagnostics.reranker_version,
        keyword_index_version=diagnostics.keyword_index_version,
        dense_index_version=diagnostics.dense_index_version,
        candidate_counts=_candidate_counts_response(diagnostics.candidate_counts),
        stage_timings=[_timing_response(timing) for timing in diagnostics.stage_timings],
        keyword_language_slice=(
            diagnostics.keyword_language_slice.value
            if diagnostics.keyword_language_slice is not None
            else None
        ),
        keyword_query_kind=(
            diagnostics.keyword_query_kind.value
            if diagnostics.keyword_query_kind is not None
            else None
        ),
        keyword_literal_term_count=diagnostics.keyword_literal_term_count,
        dense_language_slice=(
            diagnostics.dense_language_slice.value
            if diagnostics.dense_language_slice is not None
            else None
        ),
        dense_query_kind=(
            diagnostics.dense_query_kind.value if diagnostics.dense_query_kind is not None else None
        ),
        degraded=diagnostics.degraded,
        degradation_reasons=[reason.value for reason in diagnostics.degradation_reasons],
        filter_reasons=list(diagnostics.filter_reasons),
        source_filter_count=diagnostics.source_filter_count,
        document_filter_count=diagnostics.document_filter_count,
        context_only_count=diagnostics.context_only_count,
    )


def _candidate_counts_response(counts: CandidateCounts) -> CandidateCountsResponse:
    return CandidateCountsResponse(
        keyword=counts.keyword,
        dense=counts.dense,
        fused=counts.fused,
        reranked=counts.reranked,
        final=counts.final,
    )


def _timing_response(timing: StageTiming) -> StageTimingResponse:
    return StageTimingResponse(stage=timing.stage, latency_ms=timing.latency_ms)
