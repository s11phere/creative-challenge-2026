"""Provider-neutral model capabilities and adapters."""

from .contracts import (
    CapabilityAlias,
    CapabilityStatus,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatRole,
    EmbeddingRequest,
    EmbeddingResponse,
    GatewayStatus,
    ModelErrorCode,
    ModelGateway,
    ModelGatewayError,
    ModelProvider,
    ModelUsage,
    RerankRequest,
    RerankResponse,
    RerankScore,
)
from .factory import GatewayConfig, create_model_gateway
from .fake import FakeModelGateway, FakeScenario
from .openai_compatible import OpenAICompatibleGateway
from .unavailable import UnavailableModelGateway

__all__ = [
    "CapabilityAlias",
    "CapabilityStatus",
    "ChatMessage",
    "ChatRequest",
    "ChatResponse",
    "ChatRole",
    "EmbeddingRequest",
    "EmbeddingResponse",
    "FakeModelGateway",
    "FakeScenario",
    "GatewayConfig",
    "GatewayStatus",
    "ModelErrorCode",
    "ModelGateway",
    "ModelGatewayError",
    "ModelProvider",
    "ModelUsage",
    "OpenAICompatibleGateway",
    "RerankRequest",
    "RerankResponse",
    "RerankScore",
    "UnavailableModelGateway",
    "create_model_gateway",
]
