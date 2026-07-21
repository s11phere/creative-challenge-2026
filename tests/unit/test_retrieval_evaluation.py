from __future__ import annotations

import math

import pytest
from application.retrieval.evaluation import (
    EvidenceUnit,
    Locator,
    RetrievedChunk,
    aggregate_retrieval_metrics,
    canonical_config_hash,
    evaluate_retrieval_case,
    evidence_matches_chunk,
)

VERSION = "a" * 64


def _evidence(source: str, locator: Locator, *, version: str = VERSION) -> EvidenceUnit:
    return EvidenceUnit(source_key=source, source_version=version, locator=locator)


def _chunk(
    chunk_id: str,
    source: str,
    rank: int,
    *locators: Locator,
    version: str = VERSION,
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        source_key=source,
        source_version=version,
        rank=rank,
        locators=tuple(locators),
    )


@pytest.mark.parametrize(
    ("gold", "chunk_locator", "expected"),
    [
        (Locator("lines", 10, 20), Locator("lines", 20, 30), True),
        (Locator("lines", 10, 20), Locator("lines", 21, 30), False),
        (Locator("pdf_page", 3, 3), Locator("pdf_page", 2, 4), True),
        (Locator("pdf_page", 3, 3), Locator("lines", 3, 3), False),
    ],
)
def test_evidence_match_requires_locator_overlap(
    gold: Locator, chunk_locator: Locator, expected: bool
) -> None:
    evidence = _evidence("space/doc", gold)
    chunk = _chunk("chunk-1", "space/doc", 1, chunk_locator)
    assert evidence_matches_chunk(evidence, chunk) is expected


def test_evidence_match_requires_same_source_and_version() -> None:
    evidence = _evidence("space/doc", Locator("lines", 1, 5))
    assert not evidence_matches_chunk(
        evidence, _chunk("wrong-source", "other/doc", 1, Locator("lines", 1, 5))
    )
    assert not evidence_matches_chunk(
        evidence,
        _chunk("wrong-version", "space/doc", 1, Locator("lines", 1, 5), version="b" * 64),
    )


def test_case_metrics_score_independent_evidence_units() -> None:
    gold = (
        _evidence("space/a", Locator("lines", 10, 12)),
        _evidence("space/b", Locator("pdf_page", 4, 4)),
    )
    hits = (
        _chunk("irrelevant", "space/a", 1, Locator("lines", 1, 2)),
        _chunk("first-gold", "space/a", 2, Locator("lines", 8, 11)),
        _chunk("second-gold", "space/b", 5, Locator("pdf_page", 4, 4)),
    )

    metrics = evaluate_retrieval_case(gold, hits, k=5)

    assert metrics.evidence_recall_at_k == 1.0
    assert metrics.reciprocal_rank == 0.5
    assert metrics.evidence_ndcg_at_k == pytest.approx((1 / math.log2(3) + 1 / math.log2(6)) / 2)
    assert metrics.full_evidence_coverage_at_k is True


def test_no_evidence_case_is_excluded_from_ranking_denominators() -> None:
    metrics = evaluate_retrieval_case(
        (),
        (_chunk("forbidden", "ml_learning/svm", 1, Locator("pdf_page", 1, 1)),),
        must_exclude=("ml_learning/*",),
    )

    assert metrics.evidence_recall_at_k is None
    assert metrics.reciprocal_rank is None
    assert metrics.evidence_ndcg_at_k is None
    assert metrics.full_evidence_coverage_at_k is None
    assert metrics.must_exclude_violations == ("forbidden",)


def test_aggregate_metrics_keep_safety_cases_separate() -> None:
    evidenced = evaluate_retrieval_case(
        (_evidence("space/doc", Locator("lines", 1, 2)),),
        (_chunk("hit", "space/doc", 1, Locator("lines", 1, 3)),),
    )
    no_evidence = evaluate_retrieval_case(
        (),
        (_chunk("leak", "other/doc", 1, Locator("lines", 1, 2)),),
        must_exclude=("other/*",),
    )

    aggregate = aggregate_retrieval_metrics(
        (evidenced, no_evidence),
        latency_ms=(10.0, 20.0, 30.0),
        failure_count=1,
        total_query_count=3,
    )

    assert aggregate.evidence_recall_at_k == 1.0
    assert aggregate.mrr == 1.0
    assert aggregate.evidence_ndcg_at_k == 1.0
    assert aggregate.full_evidence_coverage_rate == 1.0
    assert aggregate.must_exclude_violation_count == 1
    assert aggregate.latency_p50_ms == 20.0
    assert aggregate.latency_p95_ms == pytest.approx(29.0)
    assert aggregate.failure_rate == pytest.approx(1 / 3)


def test_aggregate_recall_is_micro_averaged_by_evidence_unit() -> None:
    one_of_one = evaluate_retrieval_case(
        (_evidence("space/a", Locator("lines", 1, 1)),),
        (_chunk("a", "space/a", 1, Locator("lines", 1, 1)),),
    )
    one_of_three = evaluate_retrieval_case(
        (
            _evidence("space/b", Locator("lines", 1, 1)),
            _evidence("space/b", Locator("lines", 10, 10)),
            _evidence("space/b", Locator("lines", 20, 20)),
        ),
        (_chunk("b", "space/b", 1, Locator("lines", 1, 1)),),
    )

    aggregate = aggregate_retrieval_metrics((one_of_one, one_of_three))

    assert aggregate.evidence_recall_at_k == 0.5


def test_metric_input_rejects_duplicate_ranks_and_chunks() -> None:
    gold = (_evidence("space/doc", Locator("lines", 1, 2)),)
    with pytest.raises(ValueError, match="ranks"):
        evaluate_retrieval_case(
            gold,
            (
                _chunk("one", "space/doc", 1, Locator("lines", 1, 2)),
                _chunk("two", "space/doc", 1, Locator("lines", 3, 4)),
            ),
        )
    with pytest.raises(ValueError, match="chunk IDs"):
        evaluate_retrieval_case(
            gold,
            (
                _chunk("same", "space/doc", 1, Locator("lines", 1, 2)),
                _chunk("same", "space/doc", 2, Locator("lines", 3, 4)),
            ),
        )


def test_config_hash_is_semantic_and_deterministic() -> None:
    left = {"schema_version": "v1", "nested": {"alpha": 1, "beta": [2, 3]}}
    right = {"nested": {"beta": [2, 3], "alpha": 1}, "schema_version": "v1"}
    assert canonical_config_hash(left) == canonical_config_hash(right)
