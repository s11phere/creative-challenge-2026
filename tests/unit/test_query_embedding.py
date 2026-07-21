"""Tests for versioned query embeddings and bounded evaluation concurrency."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field

import pytest
from application.retrieval.dense import (
    QueryEmbeddingBatchRunner,
    QueryEmbeddingConfig,
    QueryEmbeddingService,
)
from domain.embedding import EmbeddingIdentity
from domain.retrieval import QueryEmbedding, RetrievalError, RetrievalErrorCode


def _vector(first: float = 1.0, second: float = 0.0) -> tuple[float, ...]:
    return (first, second, *([0.0] * 766))


@dataclass
class _TextEmbedder:
    vectors: tuple[tuple[float, ...], ...] = field(default_factory=lambda: (_vector(),))
    delay_seconds: float = 0.0
    requests: list[tuple[str, ...]] = field(default_factory=list)

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        self.requests.append(texts)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return self.vectors


async def test_query_embedding_applies_instruction_identity_and_l2_normalization() -> None:
    embedder = _TextEmbedder(vectors=(_vector(3.0, 4.0),))
    identity = EmbeddingIdentity(
        model_revision="synthetic-model@revision",
        query_instruction_version="query-prefix-v1",
        document_instruction_version="document-v1",
        normalization="l2",
    )
    service = QueryEmbeddingService(
        embedder,
        config=QueryEmbeddingConfig(
            identity=identity,
            query_prefix="query: ",
        ),
    )

    result = await service.embed_query("  deadlock\nprevention  ")

    assert embedder.requests == [("query: deadlock prevention",)]
    assert result.model_version == identity.version
    assert len(result.vector) == 768
    assert math.isclose(result.vector[0], 0.6)
    assert math.isclose(result.vector[1], 0.8)
    assert math.isclose(sum(value * value for value in result.vector), 1.0)
    assert result.latency_ms >= 0


def test_query_prefix_requires_a_versioned_instruction_identity() -> None:
    with pytest.raises(ValueError, match="versioned query instruction"):
        QueryEmbeddingConfig(query_prefix="query: ")


@pytest.mark.parametrize(
    ("vectors", "error_code"),
    [
        ((), RetrievalErrorCode.EMBEDDING_UNAVAILABLE),
        ((_vector(), _vector()), RetrievalErrorCode.EMBEDDING_UNAVAILABLE),
        (((0.0,) * 767,), RetrievalErrorCode.EMBEDDING_DIMENSION_MISMATCH),
        ((_vector(float("nan")),), RetrievalErrorCode.EMBEDDING_DIMENSION_MISMATCH),
        (((0.0,) * 768,), RetrievalErrorCode.EMBEDDING_DIMENSION_MISMATCH),
    ],
)
async def test_query_embedding_rejects_invalid_model_output(
    vectors: tuple[tuple[float, ...], ...],
    error_code: RetrievalErrorCode,
) -> None:
    service = QueryEmbeddingService(
        _TextEmbedder(vectors=vectors),
        config=QueryEmbeddingConfig(
            identity=EmbeddingIdentity(normalization="l2"),
        ),
    )
    with pytest.raises(RetrievalError) as captured:
        await service.embed_query("query")
    assert captured.value.code is error_code


async def test_query_embedding_has_an_independent_timeout() -> None:
    service = QueryEmbeddingService(
        _TextEmbedder(delay_seconds=0.05),
        config=QueryEmbeddingConfig(timeout_seconds=0.01),
    )
    with pytest.raises(TimeoutError):
        await service.embed_query("query")


async def test_batch_runner_preserves_order_and_bounds_concurrency() -> None:
    active = 0
    max_active = 0

    async def embed_query(query: str) -> QueryEmbedding:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.01)
            return QueryEmbedding(
                vector=_vector(float(query)),
                model_version="synthetic-v1",
                latency_ms=1.0,
            )
        finally:
            active -= 1

    runner = QueryEmbeddingBatchRunner(embed_query, max_concurrency=2)
    result = await runner.run(["1", "2", "3", "4", "5"])

    assert [embedding.vector[0] for embedding in result.embeddings] == [1, 2, 3, 4, 5]
    assert max_active == 2
    assert result.latency_ms >= 0


async def test_batch_runner_cancels_in_flight_and_queued_queries() -> None:
    active = 0
    two_started = asyncio.Event()
    never_release = asyncio.Event()

    async def blocking_embed(_query: str) -> QueryEmbedding:
        nonlocal active
        active += 1
        if active == 2:
            two_started.set()
        try:
            await never_release.wait()
            raise AssertionError("unreachable")
        finally:
            active -= 1

    runner = QueryEmbeddingBatchRunner(blocking_embed, max_concurrency=2)
    task = asyncio.create_task(runner.run(["1", "2", "3", "4"]))
    await asyncio.wait_for(two_started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active == 0


async def test_batch_runner_enforces_per_query_timeout() -> None:
    async def slow_embed(_query: str) -> QueryEmbedding:
        await asyncio.sleep(0.05)
        return QueryEmbedding(vector=_vector(), model_version="synthetic-v1", latency_ms=50)

    runner = QueryEmbeddingBatchRunner(
        slow_embed,
        max_concurrency=1,
        timeout_seconds=0.01,
    )
    with pytest.raises(TimeoutError):
        await runner.run(["query"])
