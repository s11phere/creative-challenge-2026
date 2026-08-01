"""Deterministic evidence mapping and retrieval metrics.

This module deliberately has no knowledge of JSONL, YAML, databases, or a
particular retrieval backend. File loading belongs to the evaluation runner;
retrieval execution will be connected after the Stage 3 contracts exist.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase


@dataclass(frozen=True)
class Locator:
    """A one-based inclusive page or line interval."""

    kind: str
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.kind not in {"pdf_page", "lines"}:
            raise ValueError(f"Unsupported locator kind: {self.kind}")
        if self.start < 1 or self.end < self.start:
            raise ValueError("Locator bounds must be one-based and inclusive")

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> Locator:
        """Build a locator from the dataset or Chunk metadata representation."""
        kind = value.get("type")
        if kind == "pdf_page":
            page = value.get("page")
            if not isinstance(page, int):
                raise ValueError("pdf_page locator requires an integer page")
            return cls(kind=kind, start=page, end=page)
        if kind == "lines":
            start = value.get("start")
            end = value.get("end")
            if not isinstance(start, int) or not isinstance(end, int):
                raise ValueError("lines locator requires integer start and end")
            return cls(kind=kind, start=start, end=end)
        raise ValueError(f"Unsupported locator kind: {kind}")

    def overlaps(self, other: Locator) -> bool:
        """Return whether two same-kind inclusive intervals overlap."""
        return self.kind == other.kind and self.start <= other.end and other.start <= self.end


@dataclass(frozen=True)
class EvidenceUnit:
    """One independently scored gold evidence annotation."""

    source_key: str
    source_version: str
    locator: Locator
    evidence_id: str | None = None


@dataclass(frozen=True)
class GoldClaim:
    """One answerable claim with its acceptable OR/AND evidence sets.

    ``acceptable_evidence_sets`` is a disjunction of conjunctions: a claim is
    satisfied when *any one* evidence set is fully retrieved (every evidence ID
    in a set appears in the matched set).
    """

    claim_id: str
    acceptable_evidence_sets: tuple[frozenset[str], ...]


@dataclass(frozen=True)
class RetrievedChunk:
    """The minimum safe metadata required to score one retrieved Chunk."""

    chunk_id: str
    source_key: str
    source_version: str
    rank: int
    locators: tuple[Locator, ...]
    keyword_rank: int | None = None
    keyword_score: float | None = None
    dense_rank: int | None = None
    dense_score: float | None = None
    fused_rank: int | None = None
    fused_score: float | None = None
    rerank_rank: int | None = None
    rerank_score: float | None = None
    context_only: bool = False

    def __post_init__(self) -> None:
        if self.rank < 1:
            raise ValueError("Retrieved rank must be one-based")
        if not self.chunk_id:
            raise ValueError("Retrieved chunk_id must not be empty")


@dataclass(frozen=True)
class CaseRetrievalMetrics:
    """Retrieval-only metrics for one evaluation case."""

    evidence_recall_at_k: float | None
    reciprocal_rank: float | None
    evidence_ndcg_at_k: float | None
    full_evidence_coverage_at_k: bool | None
    matched_evidence_count: int
    gold_evidence_count: int
    must_exclude_violations: tuple[str, ...]
    claim_recall_at_k: float | None = None
    matched_claim_count: int = 0
    total_claim_count: int = 0
    full_claim_coverage_at_k: bool | None = None


@dataclass(frozen=True)
class AggregateRetrievalMetrics:
    """Macro metrics and operational statistics for an evaluation run."""

    evidence_recall_at_k: float | None
    mrr: float | None
    evidence_ndcg_at_k: float | None
    full_evidence_coverage_rate: float | None
    must_exclude_violation_count: int
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    failure_rate: float
    claim_recall_at_k: float | None = None


class EvaluationFailureCategory(StrEnum):
    PARSER = "parser"
    LOCATOR_MAPPING = "locator_mapping"
    KEYWORD_RECALL = "keyword_recall"
    DENSE_RECALL = "dense_recall"
    FUSION = "fusion"
    RERANK = "rerank"
    VERSION_OR_FILTER = "version_or_filter"
    SAFETY_VIOLATION = "safety_violation"
    PROVIDER = "provider"
    INFRASTRUCTURE = "infrastructure"
    PROFILE = "profile"


@dataclass(frozen=True)
class RetrievalEvaluationCase:
    """Private query plus gold metadata for one retrieval-only case."""

    case_id: str
    category: str
    space_id: str
    query: str
    gold_evidence: tuple[EvidenceUnit, ...]
    must_exclude: tuple[str, ...] = ()
    gold_claims: tuple[GoldClaim, ...] = ()


@dataclass(frozen=True)
class RetrievalEvaluationObservation:
    """Safe execution output; it never carries query or Chunk text."""

    requested_mode: str
    executed_mode: str
    hits: tuple[RetrievedChunk, ...]
    latency_ms: float
    profile_version: str
    embedding_version: str | None = None
    reranker_version: str | None = None
    keyword_index_version: str | None = None
    dense_index_version: str | None = None
    candidate_counts: tuple[tuple[str, int], ...] = ()
    degraded: bool = False
    degradation_reasons: tuple[str, ...] = ()
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.latency_ms < 0 or not math.isfinite(self.latency_ms):
            raise ValueError("Evaluation latency must be finite and non-negative")


@dataclass(frozen=True)
class RetrievalEvaluationCaseResult:
    case_id: str
    category: str
    status: str
    metrics: CaseRetrievalMetrics
    observation: RetrievalEvaluationObservation
    failure_categories: tuple[EvaluationFailureCategory, ...] = ()


@dataclass(frozen=True)
class RetrievalEvaluationResult:
    cases: tuple[RetrievalEvaluationCaseResult, ...]
    metrics: AggregateRetrievalMetrics
    slices: tuple[tuple[str, AggregateRetrievalMetrics], ...]


def canonical_config_hash(config: Mapping[str, object]) -> str:
    """Hash a config by semantic JSON content, independent of YAML formatting."""
    encoded = json.dumps(
        config,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def evidence_matches_chunk(evidence: EvidenceUnit, chunk: RetrievedChunk) -> bool:
    """Match only the same source version with an overlapping locator."""
    return (
        evidence.source_key == chunk.source_key
        and evidence.source_version == chunk.source_version
        and any(evidence.locator.overlaps(locator) for locator in chunk.locators)
    )


def evaluate_retrieval_case(
    gold_evidence: Sequence[EvidenceUnit],
    retrieved_chunks: Sequence[RetrievedChunk],
    *,
    k: int = 5,
    must_exclude: Sequence[str] = (),
    gold_claims: Sequence[GoldClaim] = (),
) -> CaseRetrievalMetrics:
    """Score one ranked result list against independent evidence units.

    Evidence nDCG is the mean discounted first-hit rank of each evidence unit.
    This avoids counting overlapping Chunks repeatedly while allowing one Chunk
    to satisfy more than one independently annotated evidence unit.

    When ``gold_claims`` is supplied, a claim-level recall is also computed:
    a claim is satisfied when any one of its acceptable evidence sets is fully
    covered by the matched evidence units.
    """
    if k < 1:
        raise ValueError("k must be at least 1")

    ranked = sorted(retrieved_chunks, key=lambda item: (item.rank, item.chunk_id))
    ranks = [item.rank for item in ranked]
    chunk_ids = [item.chunk_id for item in ranked]
    if len(ranks) != len(set(ranks)):
        raise ValueError("Retrieved ranks must be unique")
    if len(chunk_ids) != len(set(chunk_ids)):
        raise ValueError("Retrieved chunk IDs must be unique")

    top_k = tuple(item for item in ranked if item.rank <= k)
    evidence_top_k = tuple(item for item in top_k if not item.context_only)
    violations = tuple(
        item.chunk_id
        for item in top_k
        if any(fnmatchcase(item.source_key, pattern) for pattern in must_exclude)
    )

    if not gold_evidence:
        return CaseRetrievalMetrics(
            evidence_recall_at_k=None,
            reciprocal_rank=None,
            evidence_ndcg_at_k=None,
            full_evidence_coverage_at_k=None,
            matched_evidence_count=0,
            gold_evidence_count=0,
            must_exclude_violations=violations,
        )

    first_hit_ranks: list[int | None] = []
    for evidence in gold_evidence:
        first_hit_ranks.append(
            next(
                (item.rank for item in evidence_top_k if evidence_matches_chunk(evidence, item)),
                None,
            )
        )

    matched_ranks = [rank for rank in first_hit_ranks if rank is not None]
    matched_count = len(matched_ranks)
    recall = matched_count / len(gold_evidence)
    first_relevant_rank = min(matched_ranks, default=None)
    reciprocal_rank = 0.0 if first_relevant_rank is None else 1.0 / first_relevant_rank
    evidence_ndcg = sum(1.0 / math.log2(rank + 1) for rank in matched_ranks) / len(gold_evidence)

    matched_evidence_ids = {
        evidence.evidence_id
        for evidence, rank in zip(gold_evidence, first_hit_ranks, strict=True)
        if rank is not None and evidence.evidence_id is not None
    }
    matched_claims = sum(
        1
        for claim in gold_claims
        if any(
            evidence_set <= matched_evidence_ids for evidence_set in claim.acceptable_evidence_sets
        )
    )

    return CaseRetrievalMetrics(
        evidence_recall_at_k=recall,
        reciprocal_rank=reciprocal_rank,
        evidence_ndcg_at_k=evidence_ndcg,
        full_evidence_coverage_at_k=matched_count == len(gold_evidence),
        matched_evidence_count=matched_count,
        gold_evidence_count=len(gold_evidence),
        must_exclude_violations=violations,
        claim_recall_at_k=(matched_claims / len(gold_claims) if gold_claims else None),
        matched_claim_count=matched_claims,
        total_claim_count=len(gold_claims),
        full_claim_coverage_at_k=(matched_claims == len(gold_claims) if gold_claims else None),
    )


def aggregate_retrieval_metrics(
    cases: Sequence[CaseRetrievalMetrics],
    *,
    latency_ms: Sequence[float] = (),
    failure_count: int = 0,
    total_query_count: int | None = None,
) -> AggregateRetrievalMetrics:
    """Aggregate evidenced cases separately from no-evidence safety slices."""
    if failure_count < 0:
        raise ValueError("failure_count cannot be negative")
    if any(value < 0 or not math.isfinite(value) for value in latency_ms):
        raise ValueError("latency values must be finite and non-negative")

    evidenced = [case for case in cases if case.gold_evidence_count > 0]
    query_count = len(cases) if total_query_count is None else total_query_count
    if query_count < len(cases) or failure_count > query_count:
        raise ValueError("total_query_count is inconsistent with cases and failures")

    claimed = [case for case in cases if case.total_claim_count > 0]

    return AggregateRetrievalMetrics(
        evidence_recall_at_k=(
            None
            if not evidenced
            else sum(case.matched_evidence_count for case in evidenced)
            / sum(case.gold_evidence_count for case in evidenced)
        ),
        mrr=_mean(case.reciprocal_rank for case in evidenced if case.reciprocal_rank is not None),
        evidence_ndcg_at_k=_mean(
            case.evidence_ndcg_at_k for case in evidenced if case.evidence_ndcg_at_k is not None
        ),
        full_evidence_coverage_rate=_mean(
            1.0 if case.full_evidence_coverage_at_k else 0.0 for case in evidenced
        ),
        must_exclude_violation_count=sum(len(case.must_exclude_violations) for case in cases),
        latency_p50_ms=_percentile(latency_ms, 0.50),
        latency_p95_ms=_percentile(latency_ms, 0.95),
        failure_rate=0.0 if query_count == 0 else failure_count / query_count,
        claim_recall_at_k=(
            None
            if not claimed
            else sum(case.matched_claim_count for case in claimed)
            / sum(case.total_claim_count for case in claimed)
        ),
    )


async def run_retrieval_evaluation(
    cases: Sequence[RetrievalEvaluationCase],
    execute: Callable[[RetrievalEvaluationCase], Awaitable[RetrievalEvaluationObservation]],
    *,
    k: int = 5,
    failed_source_keys: frozenset[str] = frozenset(),
) -> RetrievalEvaluationResult:
    """Execute and score one experiment without serializing private inputs."""
    results: list[RetrievalEvaluationCaseResult] = []
    for case in cases:
        observation = await execute(case)
        metrics = evaluate_retrieval_case(
            case.gold_evidence,
            observation.hits,
            k=k,
            must_exclude=case.must_exclude,
            gold_claims=case.gold_claims,
        )
        failures = classify_retrieval_failure(
            case,
            observation,
            metrics,
            failed_source_keys=failed_source_keys,
        )
        results.append(
            RetrievalEvaluationCaseResult(
                case_id=case.case_id,
                category=case.category,
                status="passed" if not failures else "failed",
                metrics=metrics,
                observation=observation,
                failure_categories=failures,
            )
        )

    aggregate = _aggregate_case_results(results)
    by_category: defaultdict[str, list[RetrievalEvaluationCaseResult]] = defaultdict(list)
    for result in results:
        by_category[result.category].append(result)
    slices = tuple(
        (category, _aggregate_case_results(category_results))
        for category, category_results in sorted(by_category.items())
    )
    return RetrievalEvaluationResult(cases=tuple(results), metrics=aggregate, slices=slices)


def classify_retrieval_failure(
    case: RetrievalEvaluationCase,
    observation: RetrievalEvaluationObservation,
    metrics: CaseRetrievalMetrics,
    *,
    failed_source_keys: frozenset[str] = frozenset(),
) -> tuple[EvaluationFailureCategory, ...]:
    """Return deterministic, content-free failure attribution categories."""
    failures: list[EvaluationFailureCategory] = []
    if observation.error_code is not None:
        failures.append(_error_category(observation.error_code))
    if metrics.must_exclude_violations:
        failures.append(EvaluationFailureCategory.SAFETY_VIOLATION)
    if metrics.gold_evidence_count and not metrics.full_evidence_coverage_at_k:
        gold_sources = {evidence.source_key for evidence in case.gold_evidence}
        if gold_sources & failed_source_keys:
            failures.append(EvaluationFailureCategory.PARSER)
        elif _has_source_version_mismatch(case.gold_evidence, observation.hits):
            failures.append(EvaluationFailureCategory.VERSION_OR_FILTER)
        elif _has_locator_mismatch(case.gold_evidence, observation.hits):
            failures.append(EvaluationFailureCategory.LOCATOR_MAPPING)
        else:
            failures.append(_mode_failure_category(observation.requested_mode))
    return tuple(dict.fromkeys(failures))


def _aggregate_case_results(
    results: Sequence[RetrievalEvaluationCaseResult],
) -> AggregateRetrievalMetrics:
    return aggregate_retrieval_metrics(
        [result.metrics for result in results],
        latency_ms=[
            result.observation.latency_ms
            for result in results
            if result.observation.error_code is None
        ],
        failure_count=sum(result.observation.error_code is not None for result in results),
        total_query_count=len(results),
    )


def _has_source_version_mismatch(
    evidence: Sequence[EvidenceUnit], hits: Sequence[RetrievedChunk]
) -> bool:
    return any(
        gold.source_key == hit.source_key and gold.source_version != hit.source_version
        for gold in evidence
        for hit in hits
        if not hit.context_only
    )


def _has_locator_mismatch(evidence: Sequence[EvidenceUnit], hits: Sequence[RetrievedChunk]) -> bool:
    return any(
        gold.source_key == hit.source_key
        and gold.source_version == hit.source_version
        and not any(gold.locator.overlaps(locator) for locator in hit.locators)
        for gold in evidence
        for hit in hits
        if not hit.context_only
    )


def _mode_failure_category(mode: str) -> EvaluationFailureCategory:
    return {
        "keyword": EvaluationFailureCategory.KEYWORD_RECALL,
        "dense": EvaluationFailureCategory.DENSE_RECALL,
        "hybrid": EvaluationFailureCategory.FUSION,
        "hybrid_rerank": EvaluationFailureCategory.RERANK,
    }.get(mode, EvaluationFailureCategory.INFRASTRUCTURE)


def _error_category(error_code: str) -> EvaluationFailureCategory:
    if "EMBEDDING" in error_code or "RERANKER" in error_code or "PROVIDER" in error_code:
        return EvaluationFailureCategory.PROVIDER
    if "PROFILE" in error_code:
        return EvaluationFailureCategory.PROFILE
    if "FILTER" in error_code or "SPACE" in error_code:
        return EvaluationFailureCategory.VERSION_OR_FILTER
    return EvaluationFailureCategory.INFRASTRUCTURE


def _mean(values: Iterable[float]) -> float | None:
    materialized = tuple(values)
    return None if not materialized else sum(materialized) / len(materialized)


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return float(ordered[lower])
    weight = index - lower
    return float(ordered[lower] * (1.0 - weight) + ordered[upper] * weight)
