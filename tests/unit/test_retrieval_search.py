from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

import pytest
from application.retrieval.search import SearchService, fuse_candidates
from domain.models import Document, Source, Space
from domain.retrieval import (
    CandidateBatch,
    CandidateChannel,
    DenseCandidateQuery,
    HybridEmbeddingFailurePolicy,
    KeywordCandidateQuery,
    KeywordLanguageSlice,
    KeywordQueryKind,
    LocatorKind,
    QueryEmbedding,
    RerankerFailurePolicy,
    RerankRequest,
    RerankResponse,
    RerankScore,
    RetrievalCandidate,
    RetrievalError,
    RetrievalErrorCode,
    RetrievalMode,
    RetrievalProfileV1,
    SearchExecutionContext,
    SearchFilters,
    SearchLocator,
    SearchRequest,
)

SPACE_ID = UUID(int=1)
OTHER_SPACE_ID = UUID(int=2)
SOURCE_ID = UUID(int=10)
OTHER_SOURCE_ID = UUID(int=11)
DOCUMENT_ID = UUID(int=20)
OTHER_DOCUMENT_ID = UUID(int=21)


@dataclass
class _SpaceRepo:
    spaces: dict[UUID, Space]

    async def get(self, space_id: UUID) -> Space | None:
        return self.spaces.get(space_id)


@dataclass
class _SourceRepo:
    sources: dict[UUID, Source]

    async def get(self, source_id: UUID) -> Source | None:
        return self.sources.get(source_id)


@dataclass
class _DocumentRepo:
    documents: dict[UUID, Document]

    async def get(self, document_id: UUID) -> Document | None:
        return self.documents.get(document_id)


@dataclass
class _FakeStore:
    keyword: tuple[RetrievalCandidate, ...] = ()
    dense: tuple[RetrievalCandidate, ...] = ()
    keyword_error: Exception | None = None
    dense_error: Exception | None = None
    keyword_queries: list[KeywordCandidateQuery] = field(default_factory=list)
    dense_queries: list[DenseCandidateQuery] = field(default_factory=list)

    async def keyword_candidates(self, query: KeywordCandidateQuery) -> CandidateBatch:
        self.keyword_queries.append(query)
        if self.keyword_error:
            raise self.keyword_error
        return CandidateBatch(
            channel=CandidateChannel.KEYWORD,
            candidates=self.keyword,
            index_version="fts-simple-v1",
            latency_ms=2.0,
        )

    async def dense_candidates(self, query: DenseCandidateQuery) -> CandidateBatch:
        self.dense_queries.append(query)
        if self.dense_error:
            raise self.dense_error
        return CandidateBatch(
            channel=CandidateChannel.DENSE,
            candidates=self.dense,
            index_version="pgvector-exact-v1",
            latency_ms=3.0,
        )


@dataclass
class _FakeEmbedder:
    dimensions: int = 768
    model_version: str = "fake-embedding-v1"
    error: Exception | None = None
    call_count: int = 0

    async def embed_query(self, _query: str) -> QueryEmbedding:
        self.call_count += 1
        if self.error:
            raise self.error
        return QueryEmbedding(
            vector=tuple(0.01 for _ in range(self.dimensions)),
            model_version=self.model_version,
            latency_ms=1.0,
        )


@dataclass
class _FakeReranker:
    scores: tuple[RerankScore, ...] | None = None
    error: Exception | None = None
    requests: list[RerankRequest] = field(default_factory=list)

    async def rerank(self, request: RerankRequest) -> RerankResponse:
        self.requests.append(request)
        if self.error:
            raise self.error
        scores = self.scores
        if scores is None:
            scores = tuple(
                RerankScore(index=document.index, score=float(document.index))
                for document in request.documents
            )
        return RerankResponse(scores=scores, model_version="fake-reranker-v1", latency_ms=4.0)


def _candidate(
    chunk: int,
    *,
    channel: CandidateChannel,
    rank: int,
    score: float,
    document_id: UUID = DOCUMENT_ID,
    source_id: UUID = SOURCE_ID,
) -> RetrievalCandidate:
    return RetrievalCandidate(
        chunk_id=UUID(int=chunk),
        version_id=UUID(int=100 + chunk),
        document_id=document_id,
        source_id=source_id,
        source_key=f"space/source-{source_id.int}",
        text=f"chunk {chunk}",
        chunk_hash=f"{chunk:064x}",
        locators=(SearchLocator(LocatorKind.LINES, chunk, chunk + 1),),
        channel=channel,
        rank=rank,
        score=score,
    )


@pytest.fixture
def repos() -> tuple[_SpaceRepo, _SourceRepo, _DocumentRepo]:
    return (
        _SpaceRepo({SPACE_ID: Space(id=SPACE_ID, name="main")}),
        _SourceRepo(
            {
                SOURCE_ID: Source(id=SOURCE_ID, space_id=SPACE_ID),
                OTHER_SOURCE_ID: Source(id=OTHER_SOURCE_ID, space_id=OTHER_SPACE_ID),
            }
        ),
        _DocumentRepo(
            {
                DOCUMENT_ID: Document(id=DOCUMENT_ID, source_id=SOURCE_ID),
                OTHER_DOCUMENT_ID: Document(id=OTHER_DOCUMENT_ID, source_id=OTHER_SOURCE_ID),
            }
        ),
    )


def _service(
    repos: tuple[_SpaceRepo, _SourceRepo, _DocumentRepo],
    store: _FakeStore,
    embedder: _FakeEmbedder,
    reranker: _FakeReranker | None = None,
) -> SearchService:
    spaces, sources, documents = repos
    return SearchService(
        space_repo=spaces,
        source_repo=sources,
        document_repo=documents,
        retrieval_store=store,
        query_embedder=embedder,
        reranker=reranker,
    )


def _profile(**overrides: object) -> RetrievalProfileV1:
    values: dict[str, object] = {
        "keyword_candidate_k": 10,
        "dense_candidate_k": 10,
        "fusion_candidate_k": 10,
        "rerank_k": 5,
        "final_k": 3,
        "embedding_version": "fake-embedding-v1",
    }
    values.update(overrides)
    return RetrievalProfileV1(**values)  # type: ignore[arg-type]


async def test_keyword_mode_never_calls_embedder(repos) -> None:
    store = _FakeStore(
        keyword=(_candidate(1, channel=CandidateChannel.KEYWORD, rank=1, score=0.8),)
    )
    embedder = _FakeEmbedder()
    result = await _service(repos, store, embedder).search(
        SearchRequest("  kernel\nthread  ", SPACE_ID, mode=RetrievalMode.KEYWORD), _profile()
    )
    assert [hit.chunk_id for hit in result.hits] == [UUID(int=1)]
    assert result.hits[0].keyword_rank == 1
    assert result.hits[0].safe_summary.text_length == len("chunk 1")
    assert not hasattr(result.hits[0].safe_summary, "text")
    assert result.diagnostics.executed_mode is RetrievalMode.KEYWORD
    assert result.diagnostics.keyword_language_slice is KeywordLanguageSlice.ENGLISH
    assert result.diagnostics.keyword_query_kind is KeywordQueryKind.NATURAL_LANGUAGE
    assert result.diagnostics.keyword_literal_term_count == 0
    assert store.keyword_queries[0].query == "kernel thread"
    assert embedder.call_count == 0
    assert not store.dense_queries


async def test_keyword_diagnostics_report_mixed_code_query(repos) -> None:
    store = _FakeStore()
    result = await _service(repos, store, _FakeEmbedder()).search(
        SearchRequest("\u4f7f\u7528 std::vector", SPACE_ID, mode=RetrievalMode.KEYWORD),
        _profile(),
    )
    assert result.diagnostics.keyword_language_slice is KeywordLanguageSlice.MIXED
    assert result.diagnostics.keyword_query_kind is KeywordQueryKind.CODE
    assert result.diagnostics.keyword_literal_term_count == 1
    assert store.keyword_queries[0].analysis.literal_terms == ("std::vector",)


async def test_embedding_version_mismatch_never_queries_store(repos) -> None:
    store = _FakeStore()
    embedder = _FakeEmbedder(model_version="other-embedding-v2")
    with pytest.raises(RetrievalError) as captured:
        await _service(repos, store, embedder).search(
            SearchRequest("query", SPACE_ID, mode=RetrievalMode.DENSE), _profile()
        )
    assert captured.value.code is RetrievalErrorCode.PROFILE_INCOMPATIBLE
    assert not store.dense_queries


async def test_dense_mode_passes_versioned_vector_query(repos) -> None:
    store = _FakeStore(dense=(_candidate(2, channel=CandidateChannel.DENSE, rank=1, score=0.7),))
    embedder = _FakeEmbedder()
    result = await _service(repos, store, embedder).search(
        SearchRequest("kernel", SPACE_ID, mode=RetrievalMode.DENSE), _profile()
    )
    assert result.hits[0].dense_rank == 1
    assert result.diagnostics.embedding_version == "fake-embedding-v1"
    assert store.dense_queries[0].embedding_version == "fake-embedding-v1"
    assert len(store.dense_queries[0].query_vector) == 768


async def test_hybrid_mode_fuses_and_deduplicates_candidates(repos) -> None:
    store = _FakeStore(
        keyword=(
            _candidate(1, channel=CandidateChannel.KEYWORD, rank=1, score=0.9),
            _candidate(2, channel=CandidateChannel.KEYWORD, rank=2, score=0.8),
        ),
        dense=(
            _candidate(2, channel=CandidateChannel.DENSE, rank=1, score=0.7),
            _candidate(3, channel=CandidateChannel.DENSE, rank=2, score=0.6),
        ),
    )
    result = await _service(repos, store, _FakeEmbedder()).search(
        SearchRequest("kernel", SPACE_ID, mode=RetrievalMode.HYBRID), _profile()
    )
    assert len(result.hits) == 3
    assert result.hits[0].chunk_id == UUID(int=2)
    assert result.hits[0].keyword_rank == 2
    assert result.hits[0].dense_rank == 1
    assert result.diagnostics.candidate_counts.fused == 3
    assert result.diagnostics.executed_mode is RetrievalMode.HYBRID


async def test_hybrid_rerank_maps_scores_back_to_chunks(repos) -> None:
    store = _FakeStore(
        keyword=(
            _candidate(1, channel=CandidateChannel.KEYWORD, rank=1, score=0.9),
            _candidate(2, channel=CandidateChannel.KEYWORD, rank=2, score=0.8),
        ),
        dense=(
            _candidate(1, channel=CandidateChannel.DENSE, rank=1, score=0.9),
            _candidate(2, channel=CandidateChannel.DENSE, rank=2, score=0.8),
        ),
    )
    reranker = _FakeReranker(scores=(RerankScore(0, 0.1), RerankScore(1, 0.9)))
    result = await _service(repos, store, _FakeEmbedder(), reranker).search(
        SearchRequest("kernel", SPACE_ID, mode=RetrievalMode.HYBRID_RERANK),
        _profile(reranker_enabled=True),
    )
    assert [hit.chunk_id for hit in result.hits] == [UUID(int=2), UUID(int=1)]
    assert [hit.rerank_rank for hit in result.hits] == [1, 2]
    assert result.diagnostics.reranker_version == "fake-reranker-v1"
    assert result.diagnostics.executed_mode is RetrievalMode.HYBRID_RERANK


async def test_empty_candidate_sets_are_successful(repos) -> None:
    result = await _service(repos, _FakeStore(), _FakeEmbedder()).search(
        SearchRequest("missing", SPACE_ID, mode=RetrievalMode.HYBRID), _profile()
    )
    assert result.hits == ()
    assert result.diagnostics.candidate_counts.final == 0
    assert result.diagnostics.degraded is False
    assert "fusion" in {timing.stage for timing in result.diagnostics.stage_timings}


@pytest.mark.parametrize(
    "filters",
    [
        SearchFilters(source_ids=frozenset({OTHER_SOURCE_ID})),
        SearchFilters(document_ids=frozenset({OTHER_DOCUMENT_ID})),
    ],
)
async def test_cross_space_filters_are_rejected_before_store_call(repos, filters) -> None:
    store = _FakeStore()
    with pytest.raises(RetrievalError) as captured:
        await _service(repos, store, _FakeEmbedder()).search(
            SearchRequest("query", SPACE_ID, filters=filters), _profile()
        )
    assert captured.value.code is RetrievalErrorCode.INVALID_FILTER
    assert not store.keyword_queries
    assert not store.dense_queries


async def test_valid_filters_are_forwarded_without_space_override(repos) -> None:
    filters = SearchFilters(
        source_ids=frozenset({SOURCE_ID}), document_ids=frozenset({DOCUMENT_ID})
    )
    store = _FakeStore()
    await _service(repos, store, _FakeEmbedder()).search(
        SearchRequest("query", SPACE_ID, mode=RetrievalMode.KEYWORD, filters=filters), _profile()
    )
    assert store.keyword_queries[0].space_id == SPACE_ID
    assert store.keyword_queries[0].filters == filters
    assert not hasattr(filters, "space_id")
    result = await _service(repos, _FakeStore(), _FakeEmbedder()).search(
        SearchRequest("query", SPACE_ID, mode=RetrievalMode.KEYWORD, filters=filters), _profile()
    )
    assert result.diagnostics.filter_reasons == ("source_filter", "document_filter")


async def test_missing_space_has_stable_error(repos) -> None:
    with pytest.raises(RetrievalError) as captured:
        await _service(repos, _FakeStore(), _FakeEmbedder()).search(
            SearchRequest("query", OTHER_SPACE_ID), _profile()
        )
    assert captured.value.code is RetrievalErrorCode.SPACE_NOT_FOUND


async def test_dense_dimension_mismatch_never_queries_store(repos) -> None:
    store = _FakeStore()
    with pytest.raises(RetrievalError) as captured:
        await _service(repos, store, _FakeEmbedder(dimensions=3)).search(
            SearchRequest("query", SPACE_ID, mode=RetrievalMode.DENSE), _profile()
        )
    assert captured.value.code is RetrievalErrorCode.EMBEDDING_DIMENSION_MISMATCH
    assert not store.dense_queries


async def test_dense_embedding_failure_never_degrades(repos) -> None:
    error = RetrievalError(RetrievalErrorCode.EMBEDDING_UNAVAILABLE, "offline", retryable=True)
    embedder = _FakeEmbedder(error=error)
    with pytest.raises(RetrievalError) as captured:
        await _service(repos, _FakeStore(), embedder).search(
            SearchRequest("query", SPACE_ID, mode=RetrievalMode.DENSE),
            _profile(
                hybrid_embedding_failure_policy=(HybridEmbeddingFailurePolicy.KEYWORD_FALLBACK)
            ),
        )
    assert captured.value is error


async def test_online_hybrid_can_degrade_to_keyword(repos) -> None:
    store = _FakeStore(
        keyword=(_candidate(1, channel=CandidateChannel.KEYWORD, rank=1, score=0.8),)
    )
    error = RetrievalError(RetrievalErrorCode.EMBEDDING_UNAVAILABLE, "offline")
    result = await _service(repos, store, _FakeEmbedder(error=error)).search(
        SearchRequest("query", SPACE_ID, mode=RetrievalMode.HYBRID),
        _profile(hybrid_embedding_failure_policy=HybridEmbeddingFailurePolicy.KEYWORD_FALLBACK),
    )
    assert result.diagnostics.executed_mode is RetrievalMode.KEYWORD
    assert result.diagnostics.degraded is True
    assert result.diagnostics.degradation_reasons == (RetrievalErrorCode.EMBEDDING_UNAVAILABLE,)


@pytest.mark.parametrize(
    "context,error_code",
    [
        (SearchExecutionContext.OFFLINE_EVALUATION, RetrievalErrorCode.EMBEDDING_UNAVAILABLE),
        (SearchExecutionContext.ONLINE, RetrievalErrorCode.PROVIDER_POLICY_DENIED),
    ],
)
async def test_hybrid_does_not_silently_degrade_for_forbidden_cases(
    repos, context, error_code
) -> None:
    error = RetrievalError(error_code, "blocked")
    with pytest.raises(RetrievalError) as captured:
        await _service(repos, _FakeStore(), _FakeEmbedder(error=error)).search(
            SearchRequest(
                "query",
                SPACE_ID,
                mode=RetrievalMode.HYBRID,
                execution_context=context,
            ),
            _profile(
                hybrid_embedding_failure_policy=(HybridEmbeddingFailurePolicy.KEYWORD_FALLBACK)
            ),
        )
    assert captured.value is error


async def test_online_reranker_failure_can_fall_back_to_fusion(repos) -> None:
    store = _FakeStore(
        keyword=(_candidate(1, channel=CandidateChannel.KEYWORD, rank=1, score=0.8),),
        dense=(_candidate(1, channel=CandidateChannel.DENSE, rank=1, score=0.8),),
    )
    reranker = _FakeReranker(
        error=RetrievalError(RetrievalErrorCode.RERANKER_UNAVAILABLE, "offline")
    )
    result = await _service(repos, store, _FakeEmbedder(), reranker).search(
        SearchRequest("query", SPACE_ID, mode=RetrievalMode.HYBRID_RERANK),
        _profile(
            reranker_enabled=True,
            reranker_failure_policy=RerankerFailurePolicy.FUSED_FALLBACK,
        ),
    )
    assert result.diagnostics.executed_mode is RetrievalMode.HYBRID
    assert result.diagnostics.degraded is True


async def test_offline_evaluation_rejects_reranker_fallback(repos) -> None:
    store = _FakeStore(
        keyword=(_candidate(1, channel=CandidateChannel.KEYWORD, rank=1, score=0.8),),
        dense=(_candidate(1, channel=CandidateChannel.DENSE, rank=1, score=0.8),),
    )
    error = RetrievalError(RetrievalErrorCode.RERANKER_UNAVAILABLE, "offline")
    with pytest.raises(RetrievalError) as captured:
        await _service(repos, store, _FakeEmbedder(), _FakeReranker(error=error)).search(
            SearchRequest(
                "query",
                SPACE_ID,
                mode=RetrievalMode.HYBRID_RERANK,
                execution_context=SearchExecutionContext.OFFLINE_EVALUATION,
            ),
            _profile(
                reranker_enabled=True,
                reranker_failure_policy=RerankerFailurePolicy.FUSED_FALLBACK,
            ),
        )
    assert captured.value is error


async def test_invalid_reranker_mapping_is_rejected(repos) -> None:
    store = _FakeStore(
        keyword=(_candidate(1, channel=CandidateChannel.KEYWORD, rank=1, score=0.8),),
        dense=(_candidate(1, channel=CandidateChannel.DENSE, rank=1, score=0.8),),
    )
    reranker = _FakeReranker(scores=(RerankScore(1, 0.5),))
    with pytest.raises(RetrievalError) as captured:
        await _service(repos, store, _FakeEmbedder(), reranker).search(
            SearchRequest("query", SPACE_ID, mode=RetrievalMode.HYBRID_RERANK),
            _profile(reranker_enabled=True),
        )
    assert captured.value.code is RetrievalErrorCode.RERANKER_UNAVAILABLE


async def test_hybrid_rerank_requires_compatible_profile(repos) -> None:
    with pytest.raises(RetrievalError) as captured:
        await _service(repos, _FakeStore(), _FakeEmbedder()).search(
            SearchRequest("query", SPACE_ID, mode=RetrievalMode.HYBRID_RERANK), _profile()
        )
    assert captured.value.code is RetrievalErrorCode.PROFILE_INCOMPATIBLE


def test_rrf_is_order_independent_deduplicated_and_stably_tied() -> None:
    keyword = (
        _candidate(1, channel=CandidateChannel.KEYWORD, rank=1, score=10.0),
        _candidate(2, channel=CandidateChannel.KEYWORD, rank=2, score=1.0),
    )
    dense = (
        _candidate(2, channel=CandidateChannel.DENSE, rank=1, score=0.1),
        _candidate(1, channel=CandidateChannel.DENSE, rank=2, score=0.9),
        _candidate(3, channel=CandidateChannel.DENSE, rank=3, score=0.8),
    )
    first = fuse_candidates(keyword, dense, fusion_alpha=0.5, rrf_k=60, limit=2)
    second = fuse_candidates(
        tuple(reversed(keyword)),
        tuple(reversed(dense)),
        fusion_alpha=0.5,
        rrf_k=60,
        limit=2,
    )
    assert [item.candidate.chunk_id for item in first] == [UUID(int=1), UUID(int=2)]
    assert [item.candidate.chunk_id for item in second] == [UUID(int=1), UUID(int=2)]
    assert len(first) == 2
    assert first[0].fused_score == pytest.approx(first[1].fused_score)


def test_rrf_keeps_same_text_at_different_locations() -> None:
    first = _candidate(1, channel=CandidateChannel.KEYWORD, rank=1, score=1.0)
    second = _candidate(2, channel=CandidateChannel.KEYWORD, rank=2, score=0.9)
    second = RetrievalCandidate(**{**second.__dict__, "text": first.text})
    fused = fuse_candidates((first, second), (), fusion_alpha=0.5, rrf_k=60, limit=10)
    assert len(fused) == 2
