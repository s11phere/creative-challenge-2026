"""Space-safe retrieval orchestration for keyword, dense, hybrid, and reranked modes."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass

from domain.repositories import DocumentRepository, SourceRepository, SpaceRepository
from domain.retrieval import (
    RETRIEVAL_EMBEDDING_DIMENSIONS,
    CandidateBatch,
    CandidateChannel,
    CandidateCounts,
    DenseCandidateQuery,
    HybridEmbeddingFailurePolicy,
    KeywordCandidateQuery,
    QueryEmbedder,
    QueryEmbedding,
    RerankDocument,
    Reranker,
    RerankerFailurePolicy,
    RerankRequest,
    RerankResponse,
    RetrievalCandidate,
    RetrievalError,
    RetrievalErrorCode,
    RetrievalMode,
    RetrievalProfileV1,
    RetrievalStore,
    SearchDiagnostics,
    SearchExecutionContext,
    SearchHit,
    SearchHitSummary,
    SearchRequest,
    SearchResult,
    StageTiming,
    analyze_keyword_query,
)


@dataclass(frozen=True)
class FusedCandidate:
    candidate: RetrievalCandidate
    fused_score: float
    fused_rank: int
    keyword_rank: int | None = None
    keyword_score: float | None = None
    dense_rank: int | None = None
    dense_score: float | None = None


def fuse_candidates(
    keyword: tuple[RetrievalCandidate, ...],
    dense: tuple[RetrievalCandidate, ...],
    *,
    fusion_alpha: float,
    rrf_k: int,
    limit: int,
) -> tuple[FusedCandidate, ...]:
    """Fuse two ranked lists by weighted RRF with deterministic de-duplication."""
    if not 0.0 <= fusion_alpha <= 1.0:
        raise ValueError("fusion_alpha must be between 0 and 1")
    if rrf_k < 1 or limit < 1:
        raise ValueError("rrf_k and limit must be positive")

    by_chunk: dict[object, dict[str, RetrievalCandidate]] = {}
    for channel, candidates in (("keyword", keyword), ("dense", dense)):
        for candidate in candidates:
            existing = by_chunk.setdefault(candidate.chunk_id, {})
            if existing and not _same_candidate_identity(next(iter(existing.values())), candidate):
                raise ValueError("Candidate identity differs across retrieval channels")
            existing[channel] = candidate

    scored: list[tuple[float, int, str, dict[str, RetrievalCandidate]]] = []
    for chunk_id, channels in by_chunk.items():
        keyword_candidate = channels.get("keyword")
        dense_candidate = channels.get("dense")
        keyword_rank = keyword_candidate.rank if keyword_candidate else None
        dense_rank = dense_candidate.rank if dense_candidate else None
        score = 0.0
        if keyword_rank is not None:
            score += (1.0 - fusion_alpha) / (rrf_k + keyword_rank)
        if dense_rank is not None:
            score += fusion_alpha / (rrf_k + dense_rank)
        best_rank = min(rank for rank in (keyword_rank, dense_rank) if rank is not None)
        scored.append((score, best_rank, str(chunk_id), channels))

    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    result: list[FusedCandidate] = []
    for fused_rank, (score, _best_rank, _chunk_id, channels) in enumerate(scored[:limit], start=1):
        keyword_candidate = channels.get("keyword")
        dense_candidate = channels.get("dense")
        primary_candidate = keyword_candidate or dense_candidate
        if primary_candidate is None:  # pragma: no cover - guarded by construction
            continue
        result.append(
            FusedCandidate(
                candidate=primary_candidate,
                fused_score=score,
                fused_rank=fused_rank,
                keyword_rank=keyword_candidate.rank if keyword_candidate else None,
                keyword_score=keyword_candidate.score if keyword_candidate else None,
                dense_rank=dense_candidate.rank if dense_candidate else None,
                dense_score=dense_candidate.score if dense_candidate else None,
            )
        )
    return tuple(result)


class SearchService:
    """Execute one bounded retrieval request through provider-neutral ports."""

    def __init__(
        self,
        *,
        space_repo: SpaceRepository,
        source_repo: SourceRepository,
        document_repo: DocumentRepository,
        retrieval_store: RetrievalStore,
        query_embedder: QueryEmbedder,
        reranker: Reranker | None = None,
    ) -> None:
        self._space_repo = space_repo
        self._source_repo = source_repo
        self._document_repo = document_repo
        self._retrieval_store = retrieval_store
        self._query_embedder = query_embedder
        self._reranker = reranker

    async def search(
        self,
        request: SearchRequest,
        profile: RetrievalProfileV1,
    ) -> SearchResult:
        await self._validate_scope(request)

        if request.mode is RetrievalMode.KEYWORD:
            keyword = await self._keyword(request, profile)
            hits = _raw_hits(keyword, final_k=profile.final_k)
            return SearchResult(
                hits=hits,
                diagnostics=self._diagnostics(
                    request,
                    profile,
                    executed_mode=RetrievalMode.KEYWORD,
                    keyword=keyword,
                    final_count=len(hits),
                ),
            )

        if request.mode is RetrievalMode.DENSE:
            embedding, dense = await self._dense(request, profile)
            hits = _raw_hits(dense, final_k=profile.final_k)
            return SearchResult(
                hits=hits,
                diagnostics=self._diagnostics(
                    request,
                    profile,
                    executed_mode=RetrievalMode.DENSE,
                    embedding=embedding,
                    dense=dense,
                    final_count=len(hits),
                ),
            )

        keyword = await self._keyword(request, profile)
        try:
            embedding, dense = await self._dense(request, profile)
        except RetrievalError as exc:
            if not self._can_fallback_embedding(request, profile, exc):
                raise
            hits = _raw_hits(keyword, final_k=profile.final_k)
            return SearchResult(
                hits=hits,
                diagnostics=self._diagnostics(
                    request,
                    profile,
                    executed_mode=RetrievalMode.KEYWORD,
                    keyword=keyword,
                    final_count=len(hits),
                    degradation_reasons=(exc.code,),
                ),
            )

        try:
            fused = fuse_candidates(
                keyword.candidates,
                dense.candidates,
                fusion_alpha=profile.fusion_alpha,
                rrf_k=profile.rrf_k,
                limit=profile.fusion_candidate_k,
            )
        except ValueError as exc:
            raise RetrievalError(
                RetrievalErrorCode.PROFILE_INCOMPATIBLE,
                "Retrieval candidates violate the fusion contract.",
            ) from exc

        if request.mode is RetrievalMode.HYBRID:
            hits = _fused_hits(fused, final_k=profile.final_k)
            return SearchResult(
                hits=hits,
                diagnostics=self._diagnostics(
                    request,
                    profile,
                    executed_mode=RetrievalMode.HYBRID,
                    keyword=keyword,
                    embedding=embedding,
                    dense=dense,
                    fused_count=len(fused),
                    final_count=len(hits),
                ),
            )

        if not profile.reranker_enabled:
            raise RetrievalError(
                RetrievalErrorCode.PROFILE_INCOMPATIBLE,
                "hybrid_rerank mode requires an enabled reranker profile.",
            )

        try:
            response = await self._rerank(request, fused, profile)
            hits = _reranked_hits(fused, response, final_k=profile.final_k)
        except RetrievalError as exc:
            if not self._can_fallback_reranker(request, profile, exc):
                raise
            hits = _fused_hits(fused, final_k=profile.final_k)
            return SearchResult(
                hits=hits,
                diagnostics=self._diagnostics(
                    request,
                    profile,
                    executed_mode=RetrievalMode.HYBRID,
                    keyword=keyword,
                    embedding=embedding,
                    dense=dense,
                    fused_count=len(fused),
                    final_count=len(hits),
                    degradation_reasons=(exc.code,),
                ),
            )

        return SearchResult(
            hits=hits,
            diagnostics=self._diagnostics(
                request,
                profile,
                executed_mode=RetrievalMode.HYBRID_RERANK,
                keyword=keyword,
                embedding=embedding,
                dense=dense,
                fused_count=len(fused),
                rerank_response=response,
                final_count=len(hits),
            ),
        )

    async def _validate_scope(self, request: SearchRequest) -> None:
        if await self._space_repo.get(request.space_id) is None:
            raise RetrievalError(
                RetrievalErrorCode.SPACE_NOT_FOUND,
                "The requested Space does not exist.",
            )

        for source_id in sorted(request.filters.source_ids, key=str):
            source = await self._source_repo.get(source_id)
            if source is None or source.space_id != request.space_id:
                raise RetrievalError(
                    RetrievalErrorCode.INVALID_FILTER,
                    "A source filter does not belong to the requested Space.",
                )

        for document_id in sorted(request.filters.document_ids, key=str):
            document = await self._document_repo.get(document_id)
            if document is None or document.deleted_at is not None:
                raise RetrievalError(
                    RetrievalErrorCode.INVALID_FILTER,
                    "A document filter is invalid for retrieval.",
                )
            source = await self._source_repo.get(document.source_id)
            if source is None or source.space_id != request.space_id:
                raise RetrievalError(
                    RetrievalErrorCode.INVALID_FILTER,
                    "A document filter does not belong to the requested Space.",
                )

    async def _keyword(self, request: SearchRequest, profile: RetrievalProfileV1) -> CandidateBatch:
        try:
            return await self._retrieval_store.keyword_candidates(
                KeywordCandidateQuery(
                    query=request.query,
                    space_id=request.space_id,
                    filters=request.filters,
                    limit=profile.keyword_candidate_k,
                )
            )
        except TimeoutError as exc:
            raise RetrievalError(
                RetrievalErrorCode.RETRIEVAL_TIMEOUT,
                "Keyword retrieval timed out.",
                retryable=True,
            ) from exc

    async def _dense(
        self, request: SearchRequest, profile: RetrievalProfileV1
    ) -> tuple[QueryEmbedding, CandidateBatch]:
        try:
            async with asyncio.timeout(profile.dense_timeout_seconds):
                embedding = await self._query_embedder.embed_query(request.query)
                if len(embedding.vector) != RETRIEVAL_EMBEDDING_DIMENSIONS or any(
                    not math.isfinite(value) for value in embedding.vector
                ):
                    raise RetrievalError(
                        RetrievalErrorCode.EMBEDDING_DIMENSION_MISMATCH,
                        "Query embedding must be finite and exactly 768 dimensions.",
                    )
                if embedding.model_version != profile.embedding_version:
                    raise RetrievalError(
                        RetrievalErrorCode.PROFILE_INCOMPATIBLE,
                        "Query embedding version does not match the active profile.",
                    )
                dense = await self._retrieval_store.dense_candidates(
                    DenseCandidateQuery(
                        query_vector=embedding.vector,
                        space_id=request.space_id,
                        filters=request.filters,
                        limit=profile.dense_candidate_k,
                        embedding_version=profile.embedding_version,
                    )
                )
        except TimeoutError as exc:
            raise RetrievalError(
                RetrievalErrorCode.RETRIEVAL_TIMEOUT,
                "Dense search exceeded the active profile timeout.",
                retryable=True,
            ) from exc
        return embedding, dense

    async def _rerank(
        self,
        request: SearchRequest,
        fused: tuple[FusedCandidate, ...],
        profile: RetrievalProfileV1,
    ) -> RerankResponse:
        if self._reranker is None:
            raise RetrievalError(
                RetrievalErrorCode.RERANKER_UNAVAILABLE,
                "The active profile requires a reranker, but none is available.",
                retryable=True,
            )
        selected = fused[: profile.rerank_k]
        rerank_request = RerankRequest(
            query=request.query,
            documents=tuple(
                RerankDocument(
                    index=index,
                    chunk_id=item.candidate.chunk_id,
                    text=item.candidate.text,
                )
                for index, item in enumerate(selected)
            ),
        )
        try:
            response = await self._reranker.rerank(rerank_request)
        except TimeoutError as exc:
            raise RetrievalError(
                RetrievalErrorCode.RERANKER_UNAVAILABLE,
                "Reranking timed out.",
                retryable=True,
            ) from exc
        expected_indices = set(range(len(selected)))
        actual_indices = [score.index for score in response.scores]
        if (
            len(actual_indices) != len(set(actual_indices))
            or set(actual_indices) != expected_indices
        ):
            raise RetrievalError(
                RetrievalErrorCode.RERANKER_UNAVAILABLE,
                "The reranker returned an invalid result mapping.",
            )
        return response

    @staticmethod
    def _can_fallback_embedding(
        request: SearchRequest,
        profile: RetrievalProfileV1,
        error: RetrievalError,
    ) -> bool:
        return (
            request.execution_context is SearchExecutionContext.ONLINE
            and profile.hybrid_embedding_failure_policy
            is HybridEmbeddingFailurePolicy.KEYWORD_FALLBACK
            and error.code
            in {RetrievalErrorCode.EMBEDDING_UNAVAILABLE, RetrievalErrorCode.RETRIEVAL_TIMEOUT}
        )

    @staticmethod
    def _can_fallback_reranker(
        request: SearchRequest,
        profile: RetrievalProfileV1,
        error: RetrievalError,
    ) -> bool:
        return (
            request.execution_context is SearchExecutionContext.ONLINE
            and profile.reranker_failure_policy is RerankerFailurePolicy.FUSED_FALLBACK
            and error.code is RetrievalErrorCode.RERANKER_UNAVAILABLE
        )

    @staticmethod
    def _diagnostics(
        request: SearchRequest,
        profile: RetrievalProfileV1,
        *,
        executed_mode: RetrievalMode,
        keyword: CandidateBatch | None = None,
        embedding: QueryEmbedding | None = None,
        dense: CandidateBatch | None = None,
        fused_count: int = 0,
        rerank_response: RerankResponse | None = None,
        final_count: int = 0,
        degradation_reasons: tuple[RetrievalErrorCode, ...] = (),
    ) -> SearchDiagnostics:
        keyword_analysis = analyze_keyword_query(request.query) if keyword is not None else None
        dense_analysis = analyze_keyword_query(request.query) if dense is not None else None
        timings: list[StageTiming] = []
        if keyword is not None:
            timings.append(StageTiming(stage="keyword", latency_ms=keyword.latency_ms))
        if embedding is not None:
            timings.append(StageTiming(stage="query_embedding", latency_ms=embedding.latency_ms))
        if dense is not None:
            timings.append(StageTiming(stage="dense", latency_ms=dense.latency_ms))
        if (
            keyword is not None
            and dense is not None
            and request.mode in {RetrievalMode.HYBRID, RetrievalMode.HYBRID_RERANK}
        ):
            timings.append(StageTiming(stage="fusion", latency_ms=0.0))
        if rerank_response is not None:
            timings.append(StageTiming(stage="rerank", latency_ms=rerank_response.latency_ms))
        return SearchDiagnostics(
            requested_mode=request.mode,
            executed_mode=executed_mode,
            profile_version=profile.profile_version,
            embedding_version=embedding.model_version if embedding else None,
            reranker_version=rerank_response.model_version if rerank_response else None,
            keyword_index_version=keyword.index_version if keyword else None,
            dense_index_version=dense.index_version if dense else None,
            candidate_counts=CandidateCounts(
                keyword=len(keyword.candidates) if keyword else 0,
                dense=len(dense.candidates) if dense else 0,
                fused=fused_count,
                reranked=len(rerank_response.scores) if rerank_response else 0,
                final=final_count,
            ),
            stage_timings=tuple(timings),
            keyword_language_slice=(
                keyword_analysis.language_slice if keyword_analysis is not None else None
            ),
            keyword_query_kind=(
                keyword_analysis.query_kind if keyword_analysis is not None else None
            ),
            keyword_literal_term_count=(
                len(keyword_analysis.literal_terms) if keyword_analysis is not None else 0
            ),
            dense_language_slice=(
                dense_analysis.language_slice if dense_analysis is not None else None
            ),
            dense_query_kind=(dense_analysis.query_kind if dense_analysis is not None else None),
            degraded=bool(degradation_reasons),
            degradation_reasons=degradation_reasons,
            filter_reasons=tuple(
                reason
                for reason, active in (
                    ("source_filter", bool(request.filters.source_ids)),
                    ("document_filter", bool(request.filters.document_ids)),
                )
                if active
            ),
            source_filter_count=len(request.filters.source_ids),
            document_filter_count=len(request.filters.document_ids),
        )


def _same_candidate_identity(left: RetrievalCandidate, right: RetrievalCandidate) -> bool:
    return (
        left.chunk_id == right.chunk_id
        and left.version_id == right.version_id
        and left.document_id == right.document_id
        and left.source_id == right.source_id
        and left.source_key == right.source_key
        and left.chunk_hash == right.chunk_hash
        and left.locators == right.locators
    )


def _raw_hits(batch: CandidateBatch, *, final_k: int) -> tuple[SearchHit, ...]:
    ordered = sorted(batch.candidates, key=lambda item: (item.rank, str(item.chunk_id)))[:final_k]
    return tuple(
        _to_hit(
            candidate,
            final_rank=final_rank,
            keyword_rank=candidate.rank if batch.channel is CandidateChannel.KEYWORD else None,
            keyword_score=candidate.score if batch.channel is CandidateChannel.KEYWORD else None,
            dense_rank=candidate.rank if batch.channel is CandidateChannel.DENSE else None,
            dense_score=candidate.score if batch.channel is CandidateChannel.DENSE else None,
        )
        for final_rank, candidate in enumerate(ordered, start=1)
    )


def _fused_hits(fused: tuple[FusedCandidate, ...], *, final_k: int) -> tuple[SearchHit, ...]:
    return tuple(
        _to_hit(
            item.candidate,
            final_rank=final_rank,
            keyword_rank=item.keyword_rank,
            keyword_score=item.keyword_score,
            dense_rank=item.dense_rank,
            dense_score=item.dense_score,
            fused_rank=item.fused_rank,
            fused_score=item.fused_score,
        )
        for final_rank, item in enumerate(fused[:final_k], start=1)
    )


def _reranked_hits(
    fused: tuple[FusedCandidate, ...],
    response: RerankResponse,
    *,
    final_k: int,
) -> tuple[SearchHit, ...]:
    selected = fused[: len(response.scores)]
    scores = sorted(
        response.scores,
        key=lambda score: (-score.score, selected[score.index].fused_rank),
    )
    return tuple(
        _to_hit(
            selected[score.index].candidate,
            final_rank=final_rank,
            keyword_rank=selected[score.index].keyword_rank,
            keyword_score=selected[score.index].keyword_score,
            dense_rank=selected[score.index].dense_rank,
            dense_score=selected[score.index].dense_score,
            fused_rank=selected[score.index].fused_rank,
            fused_score=selected[score.index].fused_score,
            rerank_rank=final_rank,
            rerank_score=score.score,
        )
        for final_rank, score in enumerate(scores[:final_k], start=1)
    )


def _to_hit(
    candidate: RetrievalCandidate,
    *,
    final_rank: int,
    keyword_rank: int | None = None,
    keyword_score: float | None = None,
    dense_rank: int | None = None,
    dense_score: float | None = None,
    fused_rank: int | None = None,
    fused_score: float | None = None,
    rerank_rank: int | None = None,
    rerank_score: float | None = None,
) -> SearchHit:
    return SearchHit(
        chunk_id=candidate.chunk_id,
        version_id=candidate.version_id,
        document_id=candidate.document_id,
        source_id=candidate.source_id,
        source_key=candidate.source_key,
        text=candidate.text,
        chunk_hash=candidate.chunk_hash,
        safe_summary=SearchHitSummary(
            chunk_hash=candidate.chunk_hash,
            text_length=len(candidate.text),
            locator_count=len(candidate.locators),
        ),
        locators=candidate.locators,
        final_rank=final_rank,
        keyword_rank=keyword_rank,
        keyword_score=keyword_score,
        dense_rank=dense_rank,
        dense_score=dense_score,
        fused_rank=fused_rank,
        fused_score=fused_score,
        rerank_rank=rerank_rank,
        rerank_score=rerank_score,
    )
