from __future__ import annotations

import pytest
from application.retrieval import RetrievalProfileResolver
from domain.embedding import EmbeddingIdentity
from domain.models import RetrievalProfile, Space
from domain.retrieval import (
    HybridEmbeddingFailurePolicy,
    RerankerFailurePolicy,
    RetrievalError,
    RetrievalErrorCode,
)


def test_profile_resolver_uses_server_identity_and_allowlisted_space_values() -> None:
    identity = EmbeddingIdentity(model_revision="synthetic-model@revision")
    space = Space(
        retrieval_profile=RetrievalProfile(
            top_k=4,
            rerank_k=8,
            fusion_alpha=0.25,
            extra={
                "keyword_candidate_k": "20",
                "dense_candidate_k": "24",
                "fusion_candidate_k": "25",
                "rrf_k": "40",
                "adjacent_window": "0",
                "max_chunks_per_document": "2",
                "reranker_enabled": "true",
                "hybrid_embedding_failure_policy": "keyword_fallback",
                "reranker_failure_policy": "fused_fallback",
                "model_name": "must-be-ignored",
            },
        )
    )

    profile = RetrievalProfileResolver(identity, timeout_seconds=2.5).resolve(space)

    assert profile.embedding_version == identity.version
    assert profile.keyword_candidate_k == 20
    assert profile.dense_candidate_k == 24
    assert profile.fusion_candidate_k == 25
    assert profile.rrf_k == 40
    assert profile.final_k == 4
    assert profile.rerank_k == 8
    assert profile.fusion_alpha == 0.25
    assert profile.reranker_enabled is True
    assert profile.hybrid_embedding_failure_policy is HybridEmbeddingFailurePolicy.KEYWORD_FALLBACK
    assert profile.reranker_failure_policy is RerankerFailurePolicy.FUSED_FALLBACK
    assert profile.keyword_timeout_seconds == 2.5
    assert profile.dense_timeout_seconds == 2.5
    assert not hasattr(profile, "model_name")


@pytest.mark.parametrize(
    "extra",
    [
        {"reranker_enabled": "yes"},
        {"keyword_candidate_k": "not-an-int"},
        {"hybrid_embedding_failure_policy": "silent"},
    ],
)
def test_profile_resolver_maps_invalid_persisted_values_to_stable_error(
    extra: dict[str, str],
) -> None:
    resolver = RetrievalProfileResolver(EmbeddingIdentity())
    with pytest.raises(RetrievalError) as captured:
        resolver.resolve(Space(retrieval_profile=RetrievalProfile(extra=extra)))
    assert captured.value.code is RetrievalErrorCode.PROFILE_INCOMPATIBLE
