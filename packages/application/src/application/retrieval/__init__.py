"""Retrieval application services and evaluation primitives."""

from application.retrieval.dense import (
    GatewayQueryTextEmbedder,
    QueryEmbeddingBatchResult,
    QueryEmbeddingBatchRunner,
    QueryEmbeddingConfig,
    QueryEmbeddingService,
    QueryTextEmbedder,
    document_embedding_config,
    query_embedding_config,
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
    "document_embedding_config",
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
    "query_embedding_config",
    "aggregate_retrieval_metrics",
    "canonical_config_hash",
    "classify_retrieval_failure",
    "evaluate_retrieval_case",
    "run_retrieval_evaluation",
    "fuse_candidates",
    "GatewayReranker",
    "GatewayRerankerConfig",
]
