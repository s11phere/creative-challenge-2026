"""Deterministic query planning and Space-safe multi-query retrieval."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Protocol

from domain.grounded_qa import (
    QAContractError,
    QueryFallbackReason,
    QueryPlan,
    QueryRewriter,
    QuestionInput,
    QuestionType,
    normalize_question,
)
from domain.retrieval import (
    RetrievalProfileV1,
    SearchDiagnostics,
    SearchHit,
    SearchRequest,
    SearchResult,
    analyze_keyword_query,
)

from .profile import QAPlanningProfileV1


class SearchServicePort(Protocol):
    async def search(self, request: SearchRequest, profile: RetrievalProfileV1) -> SearchResult: ...


@dataclass(frozen=True)
class QueryPlanningDiagnostic:
    rewrite_attempted: bool
    rewrite_applied: bool
    fallback_reason: QueryFallbackReason | None
    query_count: int
    question_length: int


@dataclass(frozen=True)
class QueryPlanningResult:
    plan: QueryPlan
    diagnostic: QueryPlanningDiagnostic


@dataclass(frozen=True)
class QueryRetrievalDiagnostic:
    query_index: int
    hit_count: int
    retrieval: SearchDiagnostics


@dataclass(frozen=True)
class MergedSearchResult:
    hits: tuple[SearchHit, ...]
    diagnostics: tuple[QueryRetrievalDiagnostic, ...]
    duplicate_count: int


class QueryPlanner:
    def __init__(self, rewriter: QueryRewriter | None = None) -> None:
        self._rewriter = rewriter

    async def plan(
        self, question: QuestionInput, profile: QAPlanningProfileV1
    ) -> QueryPlanningResult:
        if len(question.question) > profile.max_question_chars:
            raise QAContractError("Question exceeds the active QA profile limit")
        analysis = analyze_keyword_query(question.question)
        question_type = classify_question(question.question)
        fallback: QueryFallbackReason | None = None
        queries: tuple[str, ...] = (question.question,)
        attempted = profile.rewrite_enabled and profile.max_subqueries > 1
        rewriter = self._rewriter

        if attempted and rewriter is None:
            fallback = QueryFallbackReason.REWRITER_UNAVAILABLE
        elif attempted and rewriter is not None:
            try:
                async with asyncio.timeout(profile.rewrite_timeout_seconds):
                    rewritten = await rewriter.rewrite(
                        question, max_queries=profile.max_subqueries - 1
                    )
                novel = _validate_rewrites(
                    question.question,
                    rewritten,
                    max_queries=profile.max_subqueries - 1,
                )
                if novel:
                    queries = (question.question, *novel)
                else:
                    fallback = QueryFallbackReason.EMPTY_RESPONSE
            except TimeoutError:
                fallback = QueryFallbackReason.TIMEOUT
            except (QAContractError, ValueError, TypeError):
                fallback = QueryFallbackReason.INVALID_RESPONSE
            except Exception:
                fallback = QueryFallbackReason.INVALID_RESPONSE

        plan = QueryPlan(
            original_question=question.question,
            queries=queries,
            language_slice=analysis.language_slice,
            query_kind=analysis.query_kind,
            question_type=question_type,
            rewrite_applied=len(queries) > 1,
            fallback_reason=fallback,
        )
        return QueryPlanningResult(
            plan=plan,
            diagnostic=QueryPlanningDiagnostic(
                rewrite_attempted=attempted,
                rewrite_applied=plan.rewrite_applied,
                fallback_reason=fallback,
                query_count=len(queries),
                question_length=len(question.question),
            ),
        )


class QASearchCoordinator:
    """Call only SearchService while preserving one immutable request scope."""

    def __init__(self, search_service: SearchServicePort) -> None:
        self._search_service = search_service

    async def search(
        self,
        *,
        base_request: SearchRequest,
        plan: QueryPlan,
        profile: RetrievalProfileV1,
        limit: int,
    ) -> MergedSearchResult:
        if base_request.query != plan.original_question:
            raise QAContractError("Base SearchRequest must contain the original question")
        if limit < 1:
            raise QAContractError("Merged search limit must be positive")

        diagnostics: list[QueryRetrievalDiagnostic] = []
        by_chunk: dict[object, tuple[tuple[bool, int, int, str], SearchHit]] = {}
        total_hits = 0
        for query_index, query in enumerate(plan.queries):
            result = await self._search_service.search(
                SearchRequest(
                    query=query,
                    space_id=base_request.space_id,
                    mode=base_request.mode,
                    filters=base_request.filters,
                    execution_context=base_request.execution_context,
                ),
                profile,
            )
            diagnostics.append(
                QueryRetrievalDiagnostic(query_index, len(result.hits), result.diagnostics)
            )
            total_hits += len(result.hits)
            for hit in result.hits:
                key = (hit.context_only, query_index, hit.final_rank, str(hit.chunk_id))
                existing = by_chunk.get(hit.chunk_id)
                if existing is not None and not _same_hit_identity(existing[1], hit):
                    raise QAContractError("Duplicate SearchHit identity differs across queries")
                if existing is None or key < existing[0]:
                    by_chunk[hit.chunk_id] = (key, hit)

        ordered = sorted(by_chunk.values(), key=lambda item: item[0])[:limit]
        hits = tuple(replace(hit, final_rank=rank) for rank, (_key, hit) in enumerate(ordered, 1))
        return MergedSearchResult(
            hits=hits,
            diagnostics=tuple(diagnostics),
            duplicate_count=total_hits - len(by_chunk),
        )


def classify_question(question: str) -> QuestionType:
    normalized = normalize_question(question).casefold()
    if any(marker in normalized for marker in ("compare", "difference", "区别", "比较", "分别")):
        return QuestionType.COMPARISON
    if any(marker in normalized for marker in ("how", "步骤", "如何", "怎么", "流程")):
        return QuestionType.PROCEDURAL
    if any(marker in normalized for marker in ("relationship", "联系", "综合", "结合", "共同")):
        return QuestionType.SYNTHESIS
    return QuestionType.FACTUAL


def _validate_rewrites(
    original: str, rewritten: tuple[str, ...], *, max_queries: int
) -> tuple[str, ...]:
    if len(rewritten) > max_queries:
        raise QAContractError("Query rewriter exceeded its bounded query count")
    normalized = tuple(normalize_question(query) for query in rewritten)
    novel = tuple(dict.fromkeys(query for query in normalized if query != original))
    if len(novel) != len(normalized):
        raise QAContractError("Query rewriter returned duplicate or original queries")
    return novel


def _same_hit_identity(left: SearchHit, right: SearchHit) -> bool:
    return (
        left.chunk_id,
        left.version_id,
        left.document_id,
        left.source_id,
        left.source_key,
        left.text,
        left.chunk_hash,
        left.safe_summary,
        left.locators,
    ) == (
        right.chunk_id,
        right.version_id,
        right.document_id,
        right.source_id,
        right.source_key,
        right.text,
        right.chunk_hash,
        right.safe_summary,
        right.locators,
    )


__all__ = [
    "MergedSearchResult",
    "QASearchCoordinator",
    "QueryPlanner",
    "QueryPlanningDiagnostic",
    "QueryPlanningResult",
    "QueryRetrievalDiagnostic",
    "SearchServicePort",
    "classify_question",
]
