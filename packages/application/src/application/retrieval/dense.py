"""Provider-neutral query embedding and bounded dense evaluation helpers."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from time import perf_counter
from typing import Protocol

from domain.embedding import EmbeddingIdentity
from domain.retrieval import (
    RETRIEVAL_EMBEDDING_DIMENSIONS,
    QueryEmbedding,
    RetrievalError,
    RetrievalErrorCode,
    normalize_search_query,
)


class QueryTextEmbedder(Protocol):
    """Structural port for a ModelGateway-backed text embedding adapter."""

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]: ...


@dataclass(frozen=True)
class QueryEmbeddingConfig:
    identity: EmbeddingIdentity = field(default_factory=EmbeddingIdentity)
    query_prefix: str = ""
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if self.identity.dimensions != RETRIEVAL_EMBEDDING_DIMENSIONS:
            raise ValueError("Query embedding dimensions must be fixed at 768")
        if self.query_prefix and self.identity.query_instruction_version == "none-v1":
            raise ValueError("A query prefix requires a versioned query instruction identity")
        if self.timeout_seconds <= 0 or not math.isfinite(self.timeout_seconds):
            raise ValueError("Query embedding timeout must be finite and positive")


class QueryEmbeddingService:
    """Turn one normalized query into a versioned, validated QueryEmbedding."""

    def __init__(
        self,
        embedder: QueryTextEmbedder,
        *,
        config: QueryEmbeddingConfig | None = None,
    ) -> None:
        self._embedder = embedder
        self._config = config or QueryEmbeddingConfig()

    async def embed_query(self, query: str) -> QueryEmbedding:
        normalized = normalize_search_query(query)
        text = f"{self._config.query_prefix}{normalized}"
        started = perf_counter()
        async with asyncio.timeout(self._config.timeout_seconds):
            vectors = await self._embedder.embed((text,))
        if len(vectors) != 1:
            raise RetrievalError(
                RetrievalErrorCode.EMBEDDING_UNAVAILABLE,
                "Query embedder returned an invalid vector count.",
            )
        vector = vectors[0]
        if len(vector) != RETRIEVAL_EMBEDDING_DIMENSIONS:
            raise RetrievalError(
                RetrievalErrorCode.EMBEDDING_DIMENSION_MISMATCH,
                "Query embedding dimensions must be fixed at 768.",
            )
        if any(not math.isfinite(value) for value in vector):
            raise RetrievalError(
                RetrievalErrorCode.EMBEDDING_DIMENSION_MISMATCH,
                "Query embedding must contain finite values.",
            )
        try:
            normalized_vector = self._config.identity.normalize_vectors((vector,))[0]
        except ValueError as exc:
            raise RetrievalError(
                RetrievalErrorCode.EMBEDDING_DIMENSION_MISMATCH,
                "Query embedding cannot be normalized by the active identity.",
            ) from exc
        return QueryEmbedding(
            vector=normalized_vector,
            model_version=self._config.identity.version,
            latency_ms=(perf_counter() - started) * 1000,
        )


@dataclass(frozen=True)
class QueryEmbeddingBatchResult:
    embeddings: tuple[QueryEmbedding, ...]
    latency_ms: float


class QueryEmbeddingBatchRunner:
    """Run embedding-only evaluation queries with bounded concurrency."""

    def __init__(
        self,
        embed_query: Callable[[str], Awaitable[QueryEmbedding]],
        *,
        max_concurrency: int = 4,
        timeout_seconds: float = 30.0,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        if timeout_seconds <= 0 or not math.isfinite(timeout_seconds):
            raise ValueError("timeout_seconds must be finite and positive")
        self._embed_query = embed_query
        self._max_concurrency = max_concurrency
        self._timeout_seconds = timeout_seconds

    async def run(self, queries: Sequence[str]) -> QueryEmbeddingBatchResult:
        started = perf_counter()
        semaphore = asyncio.Semaphore(self._max_concurrency)

        async def one(query: str) -> QueryEmbedding:
            async with semaphore:
                async with asyncio.timeout(self._timeout_seconds):
                    return await self._embed_query(query)

        tasks = [asyncio.create_task(one(query)) for query in queries]
        try:
            embeddings = tuple(await asyncio.gather(*tasks))
        except (Exception, asyncio.CancelledError):
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        return QueryEmbeddingBatchResult(
            embeddings=embeddings,
            latency_ms=(perf_counter() - started) * 1000,
        )
