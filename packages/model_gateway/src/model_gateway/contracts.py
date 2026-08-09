"""Provider-neutral ModelGateway contracts."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class CapabilityAlias(StrEnum):
    FAST_CHAT = "fast_chat"
    EMBEDDING_ZH = "embedding_zh"
    RERANKER_MULTILINGUAL = "reranker_multilingual"


class ChatRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class ModelProvider(StrEnum):
    FAKE = "fake"
    OPENAI_COMPATIBLE = "openai-compatible"
    TEXT_EMBEDDINGS_INFERENCE = "text-embeddings-inference"
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
class ChatContinuation:
    """Provider-neutral continuation metadata for a bounded chat request.

    A Provider that supports a native continuation can consume ``continuation_id``.
    Other Providers receive the explicit, structured replay messages.  Neither field
    is a Provider SDK object, so callers can persist this value safely with a Run.
    """

    provider: ModelProvider
    continuation_id: str | None = None
    replay_messages: tuple[ChatMessage, ...] = ()
    context_digest: str | None = None

    def __post_init__(self) -> None:
        if self.continuation_id is not None and (
            not self.continuation_id.strip() or len(self.continuation_id) > 1_024
        ):
            raise ValueError("Chat continuation ID must be bounded and non-empty")
        if len(self.replay_messages) > 32:
            raise ValueError("Chat continuation replay is too long")
        if self.continuation_id is None and not self.replay_messages:
            raise ValueError("Chat continuation requires a native ID or replay messages")
        if self.context_digest is not None and (
            not self.context_digest.startswith("sha256:") or len(self.context_digest) != 71
        ):
            raise ValueError("Chat continuation context digest is invalid")


@dataclass(frozen=True)
class ChatRequest:
    messages: tuple[ChatMessage, ...]
    temperature: float = 0.0
    max_tokens: int | None = None
    continuation: ChatContinuation | None = None

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("Chat request must contain at least one message")
        if not 0 <= self.temperature <= 2:
            raise ValueError("Chat temperature must be between 0 and 2")
        if self.max_tokens is not None and self.max_tokens < 1:
            raise ValueError("Chat max_tokens must be positive")


ContinuationMetadata = ChatContinuation


@dataclass(frozen=True)
class EmbeddingRequest:
    texts: tuple[str, ...]
    dimensions: int | None = None

    def __post_init__(self) -> None:
        if not self.texts or any(not text for text in self.texts):
            raise ValueError("Embedding request must contain non-empty texts")
        if self.dimensions is not None and self.dimensions < 1:
            raise ValueError("Embedding dimensions must be positive")


@dataclass(frozen=True)
class RerankRequest:
    query: str
    documents: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValueError("Rerank query must not be blank")
        if not self.documents or any(not document for document in self.documents):
            raise ValueError("Rerank request must contain non-empty documents")


@dataclass(frozen=True)
class RerankScore:
    index: int
    score: float

    def __post_init__(self) -> None:
        if self.index < 0 or not math.isfinite(self.score):
            raise ValueError("Rerank score index and value must be valid")


@dataclass(frozen=True)
class RerankResponse:
    scores: tuple[RerankScore, ...]
    model_version: str
    usage: ModelUsage
    capability: CapabilityAlias
    latency_ms: float

    def __post_init__(self) -> None:
        if not self.model_version:
            raise ValueError("Rerank model_version must not be empty")
        if self.capability is not CapabilityAlias.RERANKER_MULTILINGUAL:
            raise ValueError("Rerank response capability must be reranker_multilingual")
        if self.latency_ms < 0 or not math.isfinite(self.latency_ms):
            raise ValueError("Rerank latency must be finite and non-negative")
        indices = tuple(score.index for score in self.scores)
        if len(indices) != len(set(indices)):
            raise ValueError("Rerank score indices must be unique")


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
class CapabilityStatus:
    capability: CapabilityAlias
    available: bool
    code: str


@dataclass(frozen=True)
class GatewayStatus:
    available: bool
    code: str
    provider: ModelProvider
    capabilities: tuple[CapabilityAlias, ...]
    capability_statuses: tuple[CapabilityStatus, ...] = ()

    def for_capability(self, capability: CapabilityAlias) -> CapabilityStatus:
        for status in self.capability_statuses:
            if status.capability is capability:
                return status
        available = capability in self.capabilities
        return CapabilityStatus(
            capability=capability,
            available=available,
            code=self.code if available else "MODEL_UNSUPPORTED_CAPABILITY",
        )


class ModelGatewayError(Exception):
    """Stable error returned by every ModelGateway implementation."""

    def __init__(
        self,
        code: ModelErrorCode,
        message: str,
        *,
        retryable: bool,
        capability: CapabilityAlias,
        debug_details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.capability = capability
        self.debug_details = debug_details


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

    async def rerank(
        self,
        request: RerankRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.RERANKER_MULTILINGUAL,
    ) -> RerankResponse: ...

    async def aclose(self) -> None: ...
