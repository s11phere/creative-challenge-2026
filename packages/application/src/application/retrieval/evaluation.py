"""Deterministic evidence mapping and retrieval metrics.

This module deliberately has no knowledge of JSONL, YAML, databases, or a
particular retrieval backend. File loading belongs to the evaluation runner;
retrieval execution will be connected after the Stage 3 contracts exist.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
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


@dataclass(frozen=True)
class RetrievedChunk:
    """The minimum safe metadata required to score one retrieved Chunk."""

    chunk_id: str
    source_key: str
    source_version: str
    rank: int
    locators: tuple[Locator, ...]

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
) -> CaseRetrievalMetrics:
    """Score one ranked result list against independent evidence units.

    Evidence nDCG is the mean discounted first-hit rank of each evidence unit.
    This avoids counting overlapping Chunks repeatedly while allowing one Chunk
    to satisfy more than one independently annotated evidence unit.
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
                (item.rank for item in top_k if evidence_matches_chunk(evidence, item)),
                None,
            )
        )

    matched_ranks = [rank for rank in first_hit_ranks if rank is not None]
    matched_count = len(matched_ranks)
    recall = matched_count / len(gold_evidence)
    first_relevant_rank = min(matched_ranks, default=None)
    reciprocal_rank = 0.0 if first_relevant_rank is None else 1.0 / first_relevant_rank
    evidence_ndcg = sum(1.0 / math.log2(rank + 1) for rank in matched_ranks) / len(gold_evidence)

    return CaseRetrievalMetrics(
        evidence_recall_at_k=recall,
        reciprocal_rank=reciprocal_rank,
        evidence_ndcg_at_k=evidence_ndcg,
        full_evidence_coverage_at_k=matched_count == len(gold_evidence),
        matched_evidence_count=matched_count,
        gold_evidence_count=len(gold_evidence),
        must_exclude_violations=violations,
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
    )


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
