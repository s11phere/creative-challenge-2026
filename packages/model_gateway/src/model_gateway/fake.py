"""Deterministic local ModelGateway test double."""

from __future__ import annotations

import hashlib
import json
import logging
from enum import StrEnum

from opentelemetry import trace
from opentelemetry.trace import SpanKind

from .contracts import (
    CapabilityAlias,
    CapabilityStatus,
    ChatRequest,
    ChatResponse,
    ChatToolCall,
    EmbeddingRequest,
    EmbeddingResponse,
    GatewayStatus,
    ModelErrorCode,
    ModelGatewayError,
    ModelProvider,
    ModelUsage,
    RerankRequest,
    RerankResponse,
    RerankScore,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("model_gateway.fake")


class FakeScenario(StrEnum):
    NORMAL = "normal"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    INVALID_RESPONSE = "invalid_response"
    UNAVAILABLE = "unavailable"


class FakeModelGateway:
    def __init__(
        self,
        *,
        scenario: FakeScenario = FakeScenario.NORMAL,
        embedding_dimensions: int = 768,
    ) -> None:
        if embedding_dimensions < 1:
            raise ValueError("Embedding dimensions must be positive")
        self.scenario = scenario
        self.embedding_dimensions = embedding_dimensions

    @property
    def status(self) -> GatewayStatus:
        available = self.scenario is not FakeScenario.UNAVAILABLE
        code = "MODEL_FAKE_READY" if available else "MODEL_FAKE_UNAVAILABLE"
        return GatewayStatus(
            available=available,
            code=code,
            provider=ModelProvider.FAKE,
            capabilities=(
                (
                    CapabilityAlias.FAST_CHAT,
                    CapabilityAlias.EMBEDDING_ZH,
                    CapabilityAlias.RERANKER_MULTILINGUAL,
                )
                if available
                else ()
            ),
            capability_statuses=tuple(
                CapabilityStatus(
                    capability=capability,
                    available=available,
                    code=code,
                    supports_native_tool_use=(
                        available and capability is CapabilityAlias.FAST_CHAT
                    ),
                )
                for capability in (
                    CapabilityAlias.FAST_CHAT,
                    CapabilityAlias.EMBEDDING_ZH,
                    CapabilityAlias.RERANKER_MULTILINGUAL,
                )
            ),
            model_identity="fake-fast-chat-v1",
        )

    def _raise_scenario(self, capability: CapabilityAlias) -> None:
        errors = {
            FakeScenario.TIMEOUT: (
                ModelErrorCode.TIMEOUT,
                "The model request timed out.",
                True,
            ),
            FakeScenario.RATE_LIMITED: (
                ModelErrorCode.RATE_LIMITED,
                "The model provider rate limit was reached.",
                True,
            ),
            FakeScenario.INVALID_RESPONSE: (
                ModelErrorCode.INVALID_RESPONSE,
                "The model provider returned an invalid response.",
                False,
            ),
            FakeScenario.UNAVAILABLE: (
                ModelErrorCode.UNAVAILABLE,
                "The model provider is unavailable.",
                True,
            ),
        }
        if self.scenario in errors:
            code, message, retryable = errors[self.scenario]
            raise ModelGatewayError(
                code,
                message,
                retryable=retryable,
                capability=capability,
            )

    async def chat(
        self,
        request: ChatRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.FAST_CHAT,
    ) -> ChatResponse:
        if capability is not CapabilityAlias.FAST_CHAT:
            raise self._unsupported(capability)
        with tracer.start_as_current_span(
            "model.chat",
            kind=SpanKind.CLIENT,
            attributes={"model.capability": capability.value, "model.provider": "fake"},
        ) as span:
            self._raise_scenario(capability)
            digest = hashlib.sha256(
                "\n".join(
                    f"{message.role}:{message.content}" for message in request.messages
                ).encode()
            ).hexdigest()
            input_tokens = sum(max(1, len(message.content.split())) for message in request.messages)
            tool_calls: tuple[ChatToolCall, ...]
            if request.tools and not request.tool_results:
                first_tool = request.tools[0]
                text = ""
                tool_calls = (
                    ChatToolCall(
                        call_id=f"fake_{digest[:16]}",
                        tool_name=first_tool.name,
                        arguments={},
                    ),
                )
                finish_reason = "tool_calls"
            elif any(
                "assistant router decision v1" in message.content.lower()
                for message in request.messages
                if message.role.value == "system"
            ):
                text = json.dumps(
                    {
                        "schema_version": "assistant-router-decision-v1",
                        "action": "respond",
                        "assistant_message": f"fake-response-{digest[:16]}",
                    },
                    separators=(",", ":"),
                )
                tool_calls = ()
                finish_reason = "stop"
            else:
                text = f"fake-response-{digest[:16]}"
                tool_calls = ()
                finish_reason = "stop"
            usage = ModelUsage(input_tokens=input_tokens, output_tokens=max(1, len(text.split())))
            span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", usage.output_tokens)
            self._log_success(capability, usage)
            return ChatResponse(
                text=text,
                finish_reason=finish_reason,
                usage=usage,
                capability=capability,
                latency_ms=0.0,
                tool_calls=tool_calls,
            )

    async def embed(
        self,
        request: EmbeddingRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.EMBEDDING_ZH,
    ) -> EmbeddingResponse:
        if capability is not CapabilityAlias.EMBEDDING_ZH:
            raise self._unsupported(capability)
        with tracer.start_as_current_span(
            "model.embedding",
            kind=SpanKind.CLIENT,
            attributes={"model.capability": capability.value, "model.provider": "fake"},
        ) as span:
            self._raise_scenario(capability)
            vectors = tuple(self._embedding(text) for text in request.texts)
            input_tokens = sum(max(1, len(text.split())) for text in request.texts)
            usage = ModelUsage(input_tokens=input_tokens)
            span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
            self._log_success(capability, usage)
            return EmbeddingResponse(
                vectors=vectors,
                usage=usage,
                capability=capability,
                latency_ms=0.0,
            )

    async def rerank(
        self,
        request: RerankRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.RERANKER_MULTILINGUAL,
    ) -> RerankResponse:
        if capability is not CapabilityAlias.RERANKER_MULTILINGUAL:
            raise self._unsupported(capability)
        with tracer.start_as_current_span(
            "model.rerank",
            kind=SpanKind.CLIENT,
            attributes={"model.capability": capability.value, "model.provider": "fake"},
        ) as span:
            self._raise_scenario(capability)
            scores = tuple(
                RerankScore(index=index, score=-float(index))
                for index, _document in enumerate(request.documents)
            )
            usage = ModelUsage(
                input_tokens=max(1, len(request.query.split()))
                + sum(max(1, len(document.split())) for document in request.documents)
            )
            span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
            self._log_success(capability, usage)
            return RerankResponse(
                scores=scores,
                model_version="fake-reranker-v1",
                usage=usage,
                capability=capability,
                latency_ms=0.0,
            )

    async def aclose(self) -> None:
        return None

    def _embedding(self, text: str) -> tuple[float, ...]:
        seed = hashlib.sha256(text.encode()).digest()
        return tuple(
            round((seed[index % len(seed)] / 127.5) - 1, 6)
            for index in range(self.embedding_dimensions)
        )

    @staticmethod
    def _unsupported(capability: CapabilityAlias) -> ModelGatewayError:
        return ModelGatewayError(
            ModelErrorCode.UNSUPPORTED_CAPABILITY,
            "The requested model capability is not supported by this operation.",
            retryable=False,
            capability=capability,
        )

    @staticmethod
    def _log_success(capability: CapabilityAlias, usage: ModelUsage) -> None:
        logger.info(
            "model_request_completed",
            extra={
                "capability": capability.value,
                "provider": ModelProvider.FAKE.value,
                "duration_ms": 0.0,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "retry_count": 0,
            },
        )
