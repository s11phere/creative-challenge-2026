"""Provider-neutral ModelGateway contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class CapabilityAlias(StrEnum):
    FAST_CHAT = "fast_chat"
    EMBEDDING_ZH = "embedding_zh"


class ChatRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class ModelProvider(StrEnum):
    FAKE = "fake"
    OPENAI_COMPATIBLE = "openai-compatible"
    DISABLED = "disabled"


class ModelErrorCode(StrEnum):
    UNSUPPORTED_CAPABILITY = "MODEL_UNSUPPORTED_CAPABILITY"
    TIMEOUT = "MODEL_TIMEOUT"
    RATE_LIMITED = "MODEL_RATE_LIMITED"
    UNAVAILABLE = "MODEL_UNAVAILABLE"
    INVALID_RESPONSE = "MODEL_INVALID_RESPONSE"
    POLICY_DENIED = "MODEL_POLICY_DENIED"
    AUTHENTICATION = "MODEL_AUTHENTICATION_FAILED"
    PROVIDER_ERROR = "MODEL_PROVIDER_ERROR"


@dataclass(frozen=True)
class ChatMessage:
    role: ChatRole
    content: str

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("Chat message content must not be empty")


@dataclass(frozen=True)
class ChatRequest:
    messages: tuple[ChatMessage, ...]
    temperature: float = 0.0
    max_tokens: int | None = None

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("Chat request must contain at least one message")
        if not 0 <= self.temperature <= 2:
            raise ValueError("Chat temperature must be between 0 and 2")
        if self.max_tokens is not None and self.max_tokens < 1:
            raise ValueError("Chat max_tokens must be positive")


@dataclass(frozen=True)
class EmbeddingRequest:
    texts: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.texts or any(not text for text in self.texts):
            raise ValueError("Embedding request must contain non-empty texts")


@dataclass(frozen=True)
class ModelUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class ChatResponse:
    text: str
    finish_reason: str | None
    usage: ModelUsage
    capability: CapabilityAlias
    latency_ms: float


@dataclass(frozen=True)
class EmbeddingResponse:
    vectors: tuple[tuple[float, ...], ...]
    usage: ModelUsage
    capability: CapabilityAlias
    latency_ms: float


@dataclass(frozen=True)
class GatewayStatus:
    available: bool
    code: str
    provider: ModelProvider
    capabilities: tuple[CapabilityAlias, ...]


class ModelGatewayError(Exception):
    """Stable error returned by every ModelGateway implementation."""

    def __init__(
        self,
        code: ModelErrorCode,
        message: str,
        *,
        retryable: bool,
        capability: CapabilityAlias,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.capability = capability


class ModelGateway(Protocol):
    @property
    def status(self) -> GatewayStatus: ...

    async def chat(
        self,
        request: ChatRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.FAST_CHAT,
    ) -> ChatResponse: ...

    async def embed(
        self,
        request: EmbeddingRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.EMBEDDING_ZH,
    ) -> EmbeddingResponse: ...
