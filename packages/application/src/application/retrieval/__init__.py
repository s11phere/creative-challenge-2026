"""Retrieval application services and evaluation primitives."""

from application.retrieval.dense import (
    GatewayQueryTextEmbedder,
    QueryEmbeddingBatchResult,
    QueryEmbeddingBatchRunner,
    QueryEmbeddingConfig,
    QueryEmbeddingService,
    QueryTextEmbedder,
)
from application.retrieval.evaluation import (
    AggregateRetrievalMetrics,
    CaseRetrievalMetrics,
    EvaluationFailureCategory,
    EvidenceUnit,
    Locator,
    RetrievalEvaluationCase,
    RetrievalEvaluationCaseResult,
    RetrievalEvaluationObservation,
    RetrievalEvaluationResult,
    RetrievedChunk,
    aggregate_retrieval_metrics,
    canonical_config_hash,
    classify_retrieval_failure,
    evaluate_retrieval_case,
    run_retrieval_evaluation,
)
from application.retrieval.profile import RetrievalProfileResolver
from application.retrieval.reranker import GatewayReranker, GatewayRerankerConfig
from application.retrieval.search import FusedCandidate, SearchService, fuse_candidates

__all__ = [
    "AggregateRetrievalMetrics",
    "CaseRetrievalMetrics",
    "EvaluationFailureCategory",
    "EvidenceUnit",
    "FusedCandidate",
    "GatewayQueryTextEmbedder",
    "Locator",
    "RetrievedChunk",
    "RetrievalEvaluationCase",
    "RetrievalEvaluationCaseResult",
    "RetrievalEvaluationObservation",
    "RetrievalEvaluationResult",
    "SearchService",
    "RetrievalProfileResolver",
    "QueryEmbeddingBatchResult",
    "QueryEmbeddingBatchRunner",
    "QueryEmbeddingConfig",
    "QueryEmbeddingService",
    "QueryTextEmbedder",
    "aggregate_retrieval_metrics",
    "canonical_config_hash",
    "classify_retrieval_failure",
    "evaluate_retrieval_case",
    "run_retrieval_evaluation",
    "fuse_candidates",
    "GatewayReranker",
    "GatewayRerankerConfig",
]
