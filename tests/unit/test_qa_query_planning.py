from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from application.qa.profile import QAPlanningProfileV1, load_qa_planning_profile
from application.qa.query_planning import QASearchCoordinator, QueryPlanner, classify_question
from domain.grounded_qa import (
    QAContractError,
    QueryFallbackReason,
    QuestionInput,
    QuestionType,
)
from domain.retrieval import (
    CandidateCounts,
    KeywordLanguageSlice,
    KeywordQueryKind,
    RetrievalMode,
    RetrievalProfileV1,
    SearchDiagnostics,
    SearchFilters,
    SearchHit,
    SearchHitSummary,
    SearchRequest,
    SearchResult,
)

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SPACE_ID = UUID(int=1)
SOURCE_ID = UUID(int=2)
DOCUMENT_ID = UUID(int=3)
VERSION_ID = UUID(int=4)


class StaticRewriter:
    def __init__(self, queries: tuple[str, ...]) -> None:
        self.queries = queries
        self.calls = 0

    async def rewrite(self, _question: QuestionInput, *, max_queries: int) -> tuple[str, ...]:
        self.calls += 1
        assert max_queries >= len(self.queries)
        return self.queries


class SlowRewriter:
    async def rewrite(self, _question: QuestionInput, *, max_queries: int) -> tuple[str, ...]:
        assert max_queries > 0
        await asyncio.sleep(1)
        return ("never returned",)


class FakeSearchService:
    def __init__(self, results: tuple[SearchResult, ...]) -> None:
        self.results = iter(results)
        self.requests: list[SearchRequest] = []

    async def search(self, request: SearchRequest, _profile: RetrievalProfileV1) -> SearchResult:
        self.requests.append(request)
        return next(self.results)


def _diagnostics() -> SearchDiagnostics:
    return SearchDiagnostics(
        requested_mode=RetrievalMode.HYBRID,
        executed_mode=RetrievalMode.HYBRID,
        profile_version="retrieval-profile-v1",
        embedding_version="embedding-v1",
        reranker_version=None,
        keyword_index_version="fts-v1",
        dense_index_version="dense-v1",
        candidate_counts=CandidateCounts(),
        stage_timings=(),
    )


def _hit(
    chunk_id: UUID,
    *,
    rank: int,
    context_only: bool = False,
    text: str = "evidence",
) -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id,
        version_id=VERSION_ID,
        document_id=DOCUMENT_ID,
        source_id=SOURCE_ID,
        source_key="fixture/source",
        text=text,
        chunk_hash=f"{chunk_id.int:x}".rjust(64, "0"),
        safe_summary=SearchHitSummary(
            chunk_hash=f"{chunk_id.int:x}".rjust(64, "0"),
            text_length=len(text),
            locator_count=0,
        ),
        locators=(),
        final_rank=rank,
        context_only=context_only,
    )


def _question(text: str = "如何比较 C++ vector 和 array?") -> QuestionInput:
    return QuestionInput(question=text, space_id=SPACE_ID, caller_id="user-1")


def test_repository_profile_loads_all_planning_limits() -> None:
    data = yaml.safe_load(
        (REPOSITORY_ROOT / "cases/evals/configs/qa-profile-v1.yaml").read_text(encoding="utf-8")
    )
    profile = load_qa_planning_profile(data)

    assert profile.max_subqueries == 6
    assert profile.max_history_tokens == 6000
    assert profile.max_evidence_per_source == 32
    assert profile.rewrite_enabled is False


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("What is a kernel?", QuestionType.FACTUAL),
        ("Compare vector and array", QuestionType.COMPARISON),
        ("如何构建索引？", QuestionType.PROCEDURAL),
        ("两份材料有什么联系？", QuestionType.SYNTHESIS),
    ],
)
def test_question_classification_is_deterministic(question: str, expected: QuestionType) -> None:
    assert classify_question(question) is expected


@pytest.mark.asyncio
async def test_default_plan_uses_only_original_question_and_reports_code_language() -> None:
    rewriter = StaticRewriter(("unused",))
    result = await QueryPlanner(rewriter).plan(_question(), QAPlanningProfileV1())

    assert result.plan.queries == (_question().question,)
    assert result.plan.language_slice is KeywordLanguageSlice.MIXED
    assert result.plan.query_kind is KeywordQueryKind.CODE
    assert result.plan.question_type is QuestionType.COMPARISON
    assert result.diagnostic.rewrite_attempted is False
    assert rewriter.calls == 0


@pytest.mark.asyncio
async def test_enabled_rewrite_retains_original_and_is_bounded() -> None:
    rewriter = StaticRewriter(("C++ vector", "C++ array"))
    profile = replace(QAPlanningProfileV1(), rewrite_enabled=True)
    result = await QueryPlanner(rewriter).plan(_question(), profile)

    assert result.plan.queries == (_question().question, "C++ vector", "C++ array")
    assert result.plan.rewrite_applied is True
    assert result.plan.fallback_reason is None


@pytest.mark.asyncio
async def test_rewrite_timeout_and_invalid_response_fall_back_without_query_in_diagnostic() -> None:
    timeout_profile = replace(
        QAPlanningProfileV1(), rewrite_enabled=True, rewrite_timeout_seconds=0.001
    )
    timed_out = await QueryPlanner(SlowRewriter()).plan(
        _question("private marker"), timeout_profile
    )
    assert timed_out.plan.queries == ("private marker",)
    assert timed_out.plan.fallback_reason is QueryFallbackReason.TIMEOUT
    assert "private marker" not in repr(timed_out.diagnostic)

    invalid = await QueryPlanner(StaticRewriter(("duplicate", "duplicate"))).plan(
        _question(), replace(QAPlanningProfileV1(), rewrite_enabled=True)
    )
    assert invalid.plan.queries == (_question().question,)
    assert invalid.plan.fallback_reason is QueryFallbackReason.INVALID_RESPONSE


@pytest.mark.asyncio
async def test_multi_query_search_preserves_scope_and_deduplicates_deterministically() -> None:
    shared_chunk = UUID(int=10)
    first = SearchResult(
        hits=(_hit(shared_chunk, rank=1, context_only=True), _hit(UUID(int=11), rank=2)),
        diagnostics=_diagnostics(),
    )
    second = SearchResult(
        hits=(_hit(shared_chunk, rank=2, context_only=False), _hit(UUID(int=12), rank=1)),
        diagnostics=_diagnostics(),
    )
    service = FakeSearchService((first, second))
    plan = (
        await QueryPlanner(StaticRewriter(("vector capacity",))).plan(
            _question(), replace(QAPlanningProfileV1(), rewrite_enabled=True)
        )
    ).plan
    filters = SearchFilters(source_ids=frozenset({SOURCE_ID}))
    base_request = SearchRequest(
        query=plan.original_question,
        space_id=SPACE_ID,
        filters=filters,
    )

    merged = await QASearchCoordinator(service).search(
        base_request=base_request,
        plan=plan,
        profile=RetrievalProfileV1(embedding_version="embedding-v1"),
        limit=3,
    )

    assert tuple(hit.chunk_id for hit in merged.hits) == (UUID(int=11), UUID(int=12), shared_chunk)
    assert merged.hits[-1].context_only is False
    assert merged.duplicate_count == 1
    assert all(request.space_id == SPACE_ID for request in service.requests)
    assert all(request.filters == filters for request in service.requests)
    assert tuple(request.query for request in service.requests) == plan.queries


@pytest.mark.asyncio
async def test_search_rejects_a_plan_not_bound_to_the_base_request() -> None:
    service = FakeSearchService(())
    plan = (await QueryPlanner().plan(_question("original"), QAPlanningProfileV1())).plan
    with pytest.raises(QAContractError, match="original question"):
        await QASearchCoordinator(service).search(
            base_request=SearchRequest(query="different", space_id=SPACE_ID),
            plan=plan,
            profile=RetrievalProfileV1(embedding_version="embedding-v1"),
            limit=1,
        )


@pytest.mark.asyncio
async def test_multi_query_search_rejects_same_chunk_with_different_text() -> None:
    shared_chunk = UUID(int=10)
    service = FakeSearchService(
        (
            SearchResult(
                hits=(_hit(shared_chunk, rank=1, text="first"),), diagnostics=_diagnostics()
            ),
            SearchResult(
                hits=(_hit(shared_chunk, rank=1, text="changed"),), diagnostics=_diagnostics()
            ),
        )
    )
    plan = (
        await QueryPlanner(StaticRewriter(("second query",))).plan(
            _question(), replace(QAPlanningProfileV1(), rewrite_enabled=True)
        )
    ).plan

    with pytest.raises(QAContractError, match="identity differs"):
        await QASearchCoordinator(service).search(
            base_request=SearchRequest(query=plan.original_question, space_id=SPACE_ID),
            plan=plan,
            profile=RetrievalProfileV1(embedding_version="embedding-v1"),
            limit=2,
        )
