"""Provider-neutral ModelGateway contracts."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from domain.reasoning import ReasoningProfile

type JSONValue = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]

_TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_TOOL_CALL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class CapabilityAlias(StrEnum):
    FAST_CHAT = "fast_chat"
    EMBEDDING_ZH = "embedding_zh"
    RERANKER_MULTILINGUAL = "reranker_multilingual"


class ChatRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True)
class ChatToolDefinition:
    """A self-contained native function Tool exposed for one chat turn."""

    name: str
    description: str
    input_schema: Mapping[str, JSONValue]

    def __post_init__(self) -> None:
        if _TOOL_NAME_PATTERN.fullmatch(self.name) is None:
            raise ValueError("Chat Tool name is invalid")
        if not self.description.strip() or len(self.description) > 2_000:
            raise ValueError("Chat Tool description must be non-empty and bounded")
        if not isinstance(self.input_schema, Mapping):
            raise ValueError("Chat Tool input schema must be an object")
        try:
            json.dumps(self.input_schema, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("Chat Tool input schema must be JSON") from exc
        if _contains_ref(self.input_schema):
            raise ValueError("Chat Tool input schema must be self-contained")


@dataclass(frozen=True)
class ChatToolCall:
    """One provider-native Tool request returned by a model."""

    call_id: str
    tool_name: str
    arguments: Mapping[str, JSONValue]

    def __post_init__(self) -> None:
        if _TOOL_CALL_ID_PATTERN.fullmatch(self.call_id) is None:
            raise ValueError("Chat Tool call ID is invalid")
        if _TOOL_NAME_PATTERN.fullmatch(self.tool_name) is None:
            raise ValueError("Chat Tool call name is invalid")
        if not isinstance(self.arguments, Mapping):
            raise ValueError("Chat Tool call arguments must be an object")
        _validate_json_object(self.arguments, "Chat Tool call arguments")


@dataclass(frozen=True)
class ChatToolResult:
    """A server-validated Tool result replayed to the provider on the next turn."""

    call_id: str
    tool_name: str
    observation: Mapping[str, JSONValue]

    def __post_init__(self) -> None:
        if _TOOL_CALL_ID_PATTERN.fullmatch(self.call_id) is None:
            raise ValueError("Chat Tool result call ID is invalid")
        if _TOOL_NAME_PATTERN.fullmatch(self.tool_name) is None:
            raise ValueError("Chat Tool result name is invalid")
        if not isinstance(self.observation, Mapping):
            raise ValueError("Chat Tool result observation must be an object")
        _validate_json_object(self.observation, "Chat Tool result observation")


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
    reasoning_profile: ReasoningProfile | None = None
    tools: tuple[ChatToolDefinition, ...] = ()
    tool_call_history: tuple[ChatToolCall, ...] = ()
    tool_results: tuple[ChatToolResult, ...] = ()

    def __post_init__(self) -> None:
        if not self.messages:
            raise ValueError("Chat request must contain at least one message")
        if not 0 <= self.temperature <= 2:
            raise ValueError("Chat temperature must be between 0 and 2")
        if self.max_tokens is not None and self.max_tokens < 1:
            raise ValueError("Chat max_tokens must be positive")
        names = tuple(tool.name for tool in self.tools)
        if len(names) != len(set(names)):
            raise ValueError("Chat Tool names must be unique")
        history_call_ids = tuple(call.call_id for call in self.tool_call_history)
        if len(history_call_ids) != len(set(history_call_ids)):
            raise ValueError("Chat Tool call history IDs must be unique")
        result_call_ids = tuple(result.call_id for result in self.tool_results)
        if len(result_call_ids) != len(set(result_call_ids)):
            raise ValueError("Chat Tool result call IDs must be unique")
        calls_by_id = {call.call_id: call for call in self.tool_call_history}
        if not set(result_call_ids).issubset(calls_by_id):
            raise ValueError("Chat Tool results must reference prior Tool calls")
        if any(
            calls_by_id[result.call_id].tool_name != result.tool_name
            for result in self.tool_results
        ):
            raise ValueError("Chat Tool result name does not match the prior Tool call")


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
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0

    def __post_init__(self) -> None:
        if (
            min(
                self.input_tokens,
                self.output_tokens,
                self.cached_input_tokens,
                self.cache_write_input_tokens,
            )
            < 0
        ):
            raise ValueError("Model usage counts cannot be negative")

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
    tool_calls: tuple[ChatToolCall, ...] = ()

    def __post_init__(self) -> None:
        if self.latency_ms < 0 or not math.isfinite(self.latency_ms):
            raise ValueError("Chat response latency must be finite and non-negative")
        call_ids = tuple(call.call_id for call in self.tool_calls)
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("Chat response Tool call IDs must be unique")


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
    supports_native_tool_use: bool = False
    supports_prompt_caching: bool = False


@dataclass(frozen=True)
class GatewayStatus:
    available: bool
    code: str
    provider: ModelProvider
    capabilities: tuple[CapabilityAlias, ...]
    capability_statuses: tuple[CapabilityStatus, ...] = ()
    model_identity: str = "unconfigured"
    reasoning_enabled_by_default: bool | None = None

    def supports_native_tool_use(self, capability: CapabilityAlias) -> bool:
        return self.for_capability(capability).supports_native_tool_use

    def supports_prompt_caching(self, capability: CapabilityAlias) -> bool:
        return self.for_capability(capability).supports_prompt_caching

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


def _contains_ref(value: object) -> bool:
    if isinstance(value, Mapping):
        return "$ref" in value or any(_contains_ref(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_ref(item) for item in value)
    return False


def _validate_json_object(value: Mapping[str, JSONValue], label: str) -> None:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be JSON") from exc
