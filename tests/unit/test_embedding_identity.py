"""Embedding processing identity tests."""

import pytest
from domain.embedding import EmbeddingIdentity, compute_processing_config_hash


def test_identity_version_covers_every_vector_affecting_field() -> None:
    baseline = EmbeddingIdentity(model_revision="model@abc")
    variants = (
        EmbeddingIdentity(model_revision="model@def"),
        EmbeddingIdentity(model_revision="model@abc", query_instruction_version="query-v2"),
        EmbeddingIdentity(model_revision="model@abc", document_instruction_version="doc-v2"),
        EmbeddingIdentity(model_revision="model@abc", normalization="l2"),
        EmbeddingIdentity(model_revision="model@abc", precision="float16"),
    )

    assert len(baseline.version) < 100
    assert all(candidate.version != baseline.version for candidate in variants)
    assert compute_processing_config_hash(baseline.processing_config()) == (
        compute_processing_config_hash(dict(reversed(baseline.processing_config().items())))
    )


def test_vector_dimensions_remain_fixed_by_adr_005() -> None:
    with pytest.raises(ValueError, match="fixed at 768"):
        EmbeddingIdentity(dimensions=384)


def test_l2_normalization_is_deterministic_and_rejects_zero_vectors() -> None:
    identity = EmbeddingIdentity(normalization="l2")
    normalized = identity.normalize_vectors(((3.0, 4.0),))
    assert normalized == ((0.6, 0.8),)

    with pytest.raises(ValueError, match="zero embedding"):
        identity.normalize_vectors(((0.0, 0.0),))


def test_identity_round_trips_from_persisted_processing_config() -> None:
    identity = EmbeddingIdentity(
        model_revision="model@abc",
        query_instruction_version="qwen3-knowledge-qa-v1",
        document_instruction_version="qwen3-knowledge-qa-v1",
        normalization="l2",
    )

    restored = EmbeddingIdentity.from_processing_config(identity.processing_config())

    assert restored == identity


def test_identity_rejects_non_finite_vectors_even_without_normalization() -> None:
    with pytest.raises(ValueError, match="finite"):
        EmbeddingIdentity().normalize_vectors(((float("nan"),),))
