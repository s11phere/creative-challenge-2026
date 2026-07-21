"""Non-operational gateway used for disabled or policy-denied configuration."""

from __future__ import annotations

from .contracts import (
    CapabilityAlias,
    CapabilityStatus,
    ChatRequest,
    ChatResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    GatewayStatus,
    ModelErrorCode,
    ModelGatewayError,
    ModelProvider,
    RerankRequest,
    RerankResponse,
)


class UnavailableModelGateway:
    def __init__(
        self,
        *,
        provider: ModelProvider,
        status_code: str,
        error_code: ModelErrorCode,
        message: str,
    ) -> None:
        self._status = GatewayStatus(
            available=False,
            code=status_code,
            provider=provider,
            capabilities=(),
            capability_statuses=tuple(
                CapabilityStatus(
                    capability=capability,
                    available=False,
                    code=status_code,
                )
                for capability in (
                    CapabilityAlias.FAST_CHAT,
                    CapabilityAlias.EMBEDDING_ZH,
                    CapabilityAlias.RERANKER_MULTILINGUAL,
                )
            ),
        )
        self.error_code = error_code
        self.message = message

    @property
    def status(self) -> GatewayStatus:
        return self._status

    def _error(self, capability: CapabilityAlias) -> ModelGatewayError:
        return ModelGatewayError(
            self.error_code,
            self.message,
            retryable=False,
            capability=capability,
        )

    async def chat(
        self,
        request: ChatRequest,  # noqa: ARG002
        *,
        capability: CapabilityAlias = CapabilityAlias.FAST_CHAT,
    ) -> ChatResponse:
        raise self._error(capability)

    async def embed(
        self,
        request: EmbeddingRequest,  # noqa: ARG002
        *,
        capability: CapabilityAlias = CapabilityAlias.EMBEDDING_ZH,
    ) -> EmbeddingResponse:
        raise self._error(capability)

    async def rerank(
        self,
        _request: RerankRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.RERANKER_MULTILINGUAL,
    ) -> RerankResponse:
        raise self._error(capability)

    async def aclose(self) -> None:
        return None
