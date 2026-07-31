"""Capability-level routing across ModelGateway implementations."""

from __future__ import annotations

from .contracts import (
    CapabilityAlias,
    CapabilityStatus,
    ChatRequest,
    ChatResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    GatewayStatus,
    ModelGateway,
    RerankRequest,
    RerankResponse,
)


class CapabilityRoutedModelGateway:
    """Route selected capabilities to a fallback without exposing providers to callers."""

    def __init__(
        self,
        *,
        primary: ModelGateway,
        fallback: ModelGateway,
        fallback_capabilities: frozenset[CapabilityAlias],
    ) -> None:
        if not fallback_capabilities:
            raise ValueError("At least one fallback capability is required")
        self._primary = primary
        self._fallback = fallback
        self._fallback_capabilities = fallback_capabilities

    @property
    def status(self) -> GatewayStatus:
        capabilities = tuple(CapabilityAlias)
        statuses = tuple(
            self._gateway(capability).status.for_capability(capability)
            for capability in capabilities
        )
        available = tuple(status.capability for status in statuses if status.available)
        return GatewayStatus(
            available=bool(available),
            code=(
                "MODEL_CAPABILITIES_ROUTED"
                if len(available) == len(capabilities)
                else "MODEL_CONFIGURATION_MISSING"
            ),
            provider=self._primary.status.provider,
            capabilities=available,
            capability_statuses=tuple(
                CapabilityStatus(item.capability, item.available, item.code) for item in statuses
            ),
        )

    async def chat(
        self,
        request: ChatRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.FAST_CHAT,
    ) -> ChatResponse:
        return await self._gateway(capability).chat(request, capability=capability)

    async def embed(
        self,
        request: EmbeddingRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.EMBEDDING_ZH,
    ) -> EmbeddingResponse:
        return await self._gateway(capability).embed(request, capability=capability)

    async def rerank(
        self,
        request: RerankRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.RERANKER_MULTILINGUAL,
    ) -> RerankResponse:
        return await self._gateway(capability).rerank(request, capability=capability)

    async def aclose(self) -> None:
        await self._primary.aclose()
        await self._fallback.aclose()

    def _gateway(self, capability: CapabilityAlias) -> ModelGateway:
        return self._fallback if capability in self._fallback_capabilities else self._primary


__all__ = ["CapabilityRoutedModelGateway"]
