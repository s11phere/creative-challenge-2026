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
    EvidenceUnit,
    Locator,
    RetrievedChunk,
    aggregate_retrieval_metrics,
    canonical_config_hash,
    evaluate_retrieval_case,
)
from application.retrieval.profile import RetrievalProfileResolver
from application.retrieval.reranker import GatewayReranker, GatewayRerankerConfig
from application.retrieval.search import FusedCandidate, SearchService, fuse_candidates

__all__ = [
    "AggregateRetrievalMetrics",
    "CaseRetrievalMetrics",
    "EvidenceUnit",
    "FusedCandidate",
    "GatewayQueryTextEmbedder",
    "Locator",
    "RetrievedChunk",
    "SearchService",
    "RetrievalProfileResolver",
    "QueryEmbeddingBatchResult",
    "QueryEmbeddingBatchRunner",
    "QueryEmbeddingConfig",
    "QueryEmbeddingService",
    "QueryTextEmbedder",
    "aggregate_retrieval_metrics",
    "canonical_config_hash",
    "evaluate_retrieval_case",
    "fuse_candidates",
    "GatewayReranker",
    "GatewayRerankerConfig",
]
