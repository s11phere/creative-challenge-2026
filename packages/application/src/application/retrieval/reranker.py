"""Provider-neutral adapter from ModelGateway reranking to retrieval contracts."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass

from domain.retrieval import (
    RerankRequest,
    RerankResponse,
    RerankScore,
    RetrievalError,
    RetrievalErrorCode,
)
from model_gateway import (
    CapabilityAlias,
    ModelGateway,
    ModelGatewayError,
)
from model_gateway import (
    RerankRequest as GatewayRerankRequest,
)


@dataclass(frozen=True)
class GatewayRerankerConfig:
    capability: CapabilityAlias = CapabilityAlias.RERANKER_MULTILINGUAL
    timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if self.capability is not CapabilityAlias.RERANKER_MULTILINGUAL:
            raise ValueError("GatewayReranker requires reranker_multilingual capability")
        if self.timeout_seconds <= 0 or not math.isfinite(self.timeout_seconds):
            raise ValueError("Reranker timeout must be finite and positive")


class GatewayReranker:
    """Map ModelGateway errors and scores into the retrieval application port."""

    def __init__(
        self,
        gateway: ModelGateway,
        *,
        config: GatewayRerankerConfig | None = None,
    ) -> None:
        self._gateway = gateway
        self._config = config or GatewayRerankerConfig()

    async def rerank(self, request: RerankRequest) -> RerankResponse:
        try:
            async with asyncio.timeout(self._config.timeout_seconds):
                response = await self._gateway.rerank(
                    GatewayRerankRequest(
                        query=request.query,
                        documents=tuple(document.text for document in request.documents),
                    ),
                    capability=self._config.capability,
                )
        except TimeoutError as exc:
            raise RetrievalError(
                RetrievalErrorCode.RERANKER_UNAVAILABLE,
                "Reranking exceeded the active timeout.",
                retryable=True,
            ) from exc
        except ModelGatewayError as exc:
            raise RetrievalError(
                RetrievalErrorCode.RERANKER_UNAVAILABLE,
                "The configured reranker is unavailable.",
                retryable=exc.retryable,
            ) from exc

        if response.capability is not self._config.capability:
            raise RetrievalError(
                RetrievalErrorCode.RERANKER_UNAVAILABLE,
                "The reranker returned an incompatible capability.",
            )
        scores = tuple(
            RerankScore(index=score.index, score=score.score) for score in response.scores
        )
        return RerankResponse(
            scores=scores,
            model_version=response.model_version,
            latency_ms=response.latency_ms,
        )
