"""Provider-neutral model capabilities and adapters."""

from domain.reasoning import (
    ReasoningDowngradeReason,
    ReasoningEffort,
    ReasoningMode,
    ReasoningProfile,
)

from .contracts import (
    CapabilityAlias,
    CapabilityStatus,
    ChatContinuation,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatRole,
    ChatToolCall,
    ChatToolDefinition,
    ChatToolResult,
    ContinuationMetadata,
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
from .reasoning import (
    ModelCapabilities,
    ModelCapabilityRegistry,
    ReasoningMappingError,
    default_model_capability_registry,
)
from .routed import CapabilityRoutedModelGateway
from .unavailable import UnavailableModelGateway

__all__ = [
    "CapabilityAlias",
    "CapabilityStatus",
    "ChatContinuation",
    "ChatMessage",
    "ChatRequest",
    "ChatResponse",
    "ChatRole",
    "ChatToolCall",
    "ChatToolDefinition",
    "ChatToolResult",
    "ContinuationMetadata",
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
    "ModelCapabilities",
    "ModelCapabilityRegistry",
    "ReasoningMappingError",
    "default_model_capability_registry",
    "CapabilityRoutedModelGateway",
    "RerankRequest",
    "RerankResponse",
    "RerankScore",
    "ReasoningDowngradeReason",
    "ReasoningEffort",
    "ReasoningMode",
    "ReasoningProfile",
    "UnavailableModelGateway",
    "create_model_gateway",
]
