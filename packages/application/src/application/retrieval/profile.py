"""Resolve the active retrieval profile from trusted server-side state."""

from __future__ import annotations

from dataclasses import dataclass

from domain.embedding import EmbeddingIdentity
from domain.models import Space
from domain.retrieval import (
    HybridEmbeddingFailurePolicy,
    RerankerFailurePolicy,
    RetrievalError,
    RetrievalErrorCode,
    RetrievalProfileV1,
)


@dataclass(frozen=True)
class RetrievalProfileResolver:
    """Map the persisted Space profile onto the versioned retrieval contract.

    Only explicitly allowlisted values from ``RetrievalProfile.extra`` are
    accepted. Model identity and timeouts always come from server settings.
    """

    embedding_identity: EmbeddingIdentity
    timeout_seconds: float = 15.0

    def resolve(self, space: Space) -> RetrievalProfileV1:
        legacy = space.retrieval_profile
        extra = legacy.extra
        try:
            reranker_enabled = _bool_value(extra, "reranker_enabled", False)
            hybrid_policy = HybridEmbeddingFailurePolicy(
                extra.get("hybrid_embedding_failure_policy", "error")
            )
            reranker_policy = RerankerFailurePolicy(extra.get("reranker_failure_policy", "error"))
            candidate_floor = max(legacy.top_k, legacy.rerank_k)
            fusion_candidate_k = max(
                candidate_floor,
                _int_value(extra, "fusion_candidate_k", 30),
            )
            return RetrievalProfileV1(
                keyword_candidate_k=_int_value(extra, "keyword_candidate_k", 30),
                dense_candidate_k=_int_value(extra, "dense_candidate_k", 30),
                fusion_candidate_k=fusion_candidate_k,
                rrf_k=_int_value(extra, "rrf_k", 60),
                fusion_alpha=legacy.fusion_alpha,
                reranker_enabled=reranker_enabled,
                rerank_k=legacy.rerank_k,
                final_k=legacy.top_k,
                adjacent_window=_int_value(extra, "adjacent_window", 1),
                max_chunks_per_document=_int_value(extra, "max_chunks_per_document", 3),
                hybrid_embedding_failure_policy=hybrid_policy,
                reranker_failure_policy=reranker_policy,
                embedding_version=self.embedding_identity.version,
                keyword_timeout_seconds=self.timeout_seconds,
                dense_timeout_seconds=self.timeout_seconds,
            )
        except (TypeError, ValueError) as exc:
            raise RetrievalError(
                RetrievalErrorCode.PROFILE_INCOMPATIBLE,
                "The active Space retrieval profile is incompatible.",
            ) from exc


def _int_value(values: dict[str, str], key: str, default: int) -> int:
    raw = values.get(key)
    return default if raw is None else int(raw)


def _bool_value(values: dict[str, str], key: str, default: bool) -> bool:
    raw = values.get(key)
    if raw is None:
        return default
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise ValueError(f"{key} must be 'true' or 'false'")
