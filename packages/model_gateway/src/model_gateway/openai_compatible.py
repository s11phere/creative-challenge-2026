"""OpenAI-compatible HTTP ModelGateway adapter."""

from __future__ import annotations

import asyncio
import logging
import math
from time import perf_counter
from typing import Any

import httpx
from opentelemetry import trace
from opentelemetry.trace import SpanKind

from .contracts import (
    CapabilityAlias,
    ChatRequest,
    ChatResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    GatewayStatus,
    ModelErrorCode,
    ModelGatewayError,
    ModelProvider,
    ModelUsage,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("model_gateway.openai_compatible")


class OpenAICompatibleGateway:
    def __init__(
        self,
        *,
        endpoint: str,
        fast_chat_model: str,
        embedding_model: str,
        api_key: str | None = None,
        timeout_seconds: float = 15.0,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.1,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if timeout_seconds <= 0 or max_retries < 0 or retry_backoff_seconds < 0:
            raise ValueError("Model timeout must be positive and retry settings non-negative")
        if not fast_chat_model or not embedding_model:
            raise ValueError("Configured model names must not be empty")
        self.endpoint = httpx.URL(endpoint.rstrip("/") + "/")
        self.fast_chat_model = fast_chat_model
        self.embedding_model = embedding_model
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.timeout_seconds = timeout_seconds
        self._api_key = api_key
        self._owns_client = client is None
        self.client = client

    @property
    def status(self) -> GatewayStatus:
        return GatewayStatus(
            available=True,
            code="MODEL_PROVIDER_CONFIGURED",
            provider=ModelProvider.OPENAI_COMPATIBLE,
            capabilities=(CapabilityAlias.FAST_CHAT, CapabilityAlias.EMBEDDING_ZH),
        )

    async def __aenter__(self) -> OpenAICompatibleGateway:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client and self.client is not None:
            await self.client.aclose()
            self.client = None

    async def chat(
        self,
        request: ChatRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.FAST_CHAT,
    ) -> ChatResponse:
        if capability is not CapabilityAlias.FAST_CHAT:
            raise self._unsupported(capability)
        started_at = perf_counter()
        with tracer.start_as_current_span(
            "model.chat",
            kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": "openai-compatible",
                "model.capability": capability.value,
            },
        ) as span:
            data, retries = await self._request_json(
                "chat/completions",
                {
                    "model": self.fast_chat_model,
                    "messages": [
                        {"role": message.role.value, "content": message.content}
                        for message in request.messages
                    ],
                    "temperature": request.temperature,
                    **(
                        {"max_tokens": request.max_tokens} if request.max_tokens is not None else {}
                    ),
                },
                capability=capability,
            )
            text, finish_reason = self._parse_chat(data, capability)
            usage = self._parse_usage(data, embedding=False)
            latency_ms = (perf_counter() - started_at) * 1000
            span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
            span.set_attribute("gen_ai.usage.output_tokens", usage.output_tokens)
            self._log_success(capability, latency_ms, usage, retries)
            return ChatResponse(
                text=text,
                finish_reason=finish_reason,
                usage=usage,
                capability=capability,
                latency_ms=latency_ms,
            )

    async def embed(
        self,
        request: EmbeddingRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.EMBEDDING_ZH,
    ) -> EmbeddingResponse:
        if capability is not CapabilityAlias.EMBEDDING_ZH:
            raise self._unsupported(capability)
        started_at = perf_counter()
        with tracer.start_as_current_span(
            "model.embedding",
            kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.operation.name": "embeddings",
                "gen_ai.provider.name": "openai-compatible",
                "model.capability": capability.value,
            },
        ) as span:
            data, retries = await self._request_json(
                "embeddings",
                {"model": self.embedding_model, "input": list(request.texts)},
                capability=capability,
            )
            vectors = self._parse_embeddings(data, len(request.texts), capability)
            usage = self._parse_usage(data, embedding=True)
            latency_ms = (perf_counter() - started_at) * 1000
            span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
            self._log_success(capability, latency_ms, usage, retries)
            return EmbeddingResponse(
                vectors=vectors,
                usage=usage,
                capability=capability,
                latency_ms=latency_ms,
            )

    async def _request_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        capability: CapabilityAlias,
    ) -> tuple[dict[str, Any], int]:
        client = self._client()
        for attempt in range(self.max_retries + 1):
            try:
                response = await client.post(self.endpoint.join(path), json=payload)
                error = self._http_error(response.status_code, capability)
                if error is not None:
                    raise error
                parsed = response.json()
                if not isinstance(parsed, dict):
                    raise self._invalid_response(capability)
                return parsed, attempt
            except httpx.TimeoutException as exc:
                error = ModelGatewayError(
                    ModelErrorCode.TIMEOUT,
                    "The model request timed out.",
                    retryable=True,
                    capability=capability,
                )
                cause: Exception = exc
            except httpx.TransportError as exc:
                error = ModelGatewayError(
                    ModelErrorCode.UNAVAILABLE,
                    "The model provider is unavailable.",
                    retryable=True,
                    capability=capability,
                )
                cause = exc
            except ModelGatewayError as exc:
                error = exc
                cause = exc
            except ValueError as exc:
                error = self._invalid_response(capability)
                cause = exc

            if not error.retryable or attempt >= self.max_retries:
                raise error from cause
            logger.warning(
                "model_request_retry",
                extra={
                    "capability": capability.value,
                    "provider": ModelProvider.OPENAI_COMPATIBLE.value,
                    "retry_count": attempt + 1,
                    "error_type": error.code.value,
                },
            )
            await asyncio.sleep(self.retry_backoff_seconds * (2**attempt))
        raise AssertionError("Retry loop exhausted without returning or raising")

    def _client(self) -> httpx.AsyncClient:
        if self.client is None:
            headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else None
            self.client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds),
                headers=headers,
            )
        return self.client

    @staticmethod
    def _http_error(status_code: int, capability: CapabilityAlias) -> ModelGatewayError | None:
        if 200 <= status_code < 300:
            return None
        if status_code == 429:
            return ModelGatewayError(
                ModelErrorCode.RATE_LIMITED,
                "The model provider rate limit was reached.",
                retryable=True,
                capability=capability,
            )
        if status_code == 408:
            return ModelGatewayError(
                ModelErrorCode.TIMEOUT,
                "The model request timed out.",
                retryable=True,
                capability=capability,
            )
        if status_code in {401, 403}:
            return ModelGatewayError(
                ModelErrorCode.AUTHENTICATION,
                "The model provider rejected authentication.",
                retryable=False,
                capability=capability,
            )
        if status_code >= 500:
            return ModelGatewayError(
                ModelErrorCode.UNAVAILABLE,
                "The model provider is unavailable.",
                retryable=True,
                capability=capability,
            )
        return ModelGatewayError(
            ModelErrorCode.PROVIDER_ERROR,
            "The model provider rejected the request.",
            retryable=False,
            capability=capability,
        )

    @staticmethod
    def _invalid_response(capability: CapabilityAlias) -> ModelGatewayError:
        return ModelGatewayError(
            ModelErrorCode.INVALID_RESPONSE,
            "The model provider returned an invalid response.",
            retryable=False,
            capability=capability,
        )

    @staticmethod
    def _unsupported(capability: CapabilityAlias) -> ModelGatewayError:
        return ModelGatewayError(
            ModelErrorCode.UNSUPPORTED_CAPABILITY,
            "The requested model capability is not supported by this operation.",
            retryable=False,
            capability=capability,
        )

    @classmethod
    def _parse_chat(
        cls, data: dict[str, Any], capability: CapabilityAlias
    ) -> tuple[str, str | None]:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise cls._invalid_response(capability)
        message = choices[0].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise cls._invalid_response(capability)
        content = message["content"]
        if not content:
            raise cls._invalid_response(capability)
        finish_reason = choices[0].get("finish_reason")
        if finish_reason is not None and not isinstance(finish_reason, str):
            raise cls._invalid_response(capability)
        return content, finish_reason

    @classmethod
    def _parse_embeddings(
        cls,
        payload: dict[str, Any],
        expected_count: int,
        capability: CapabilityAlias,
    ) -> tuple[tuple[float, ...], ...]:
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != expected_count:
            raise cls._invalid_response(capability)
        indexed_vectors: dict[int, tuple[float, ...]] = {}
        for item in data:
            if not isinstance(item, dict):
                raise cls._invalid_response(capability)
            index = item.get("index")
            embedding = item.get("embedding")
            if isinstance(index, bool) or not isinstance(index, int) or index in indexed_vectors:
                raise cls._invalid_response(capability)
            if not isinstance(embedding, list) or not embedding:
                raise cls._invalid_response(capability)
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                for value in embedding
            ):
                raise cls._invalid_response(capability)
            indexed_vectors[index] = tuple(float(value) for value in embedding)
        if set(indexed_vectors) != set(range(expected_count)):
            raise cls._invalid_response(capability)
        dimensions = {len(vector) for vector in indexed_vectors.values()}
        if len(dimensions) != 1:
            raise cls._invalid_response(capability)
        return tuple(indexed_vectors[index] for index in range(expected_count))

    @classmethod
    def _parse_usage(cls, payload: dict[str, Any], *, embedding: bool) -> ModelUsage:
        usage = payload.get("usage", {})
        if not isinstance(usage, dict):
            raise cls._invalid_response(
                CapabilityAlias.EMBEDDING_ZH if embedding else CapabilityAlias.FAST_CHAT
            )
        capability = CapabilityAlias.EMBEDDING_ZH if embedding else CapabilityAlias.FAST_CHAT
        input_tokens = cls._token_count(
            usage.get("prompt_tokens", usage.get("total_tokens", 0)), capability
        )
        output_tokens = (
            0 if embedding else cls._token_count(usage.get("completion_tokens", 0), capability)
        )
        return ModelUsage(input_tokens=input_tokens, output_tokens=output_tokens)

    @classmethod
    def _token_count(cls, value: Any, capability: CapabilityAlias) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise cls._invalid_response(capability)
        return value

    @staticmethod
    def _log_success(
        capability: CapabilityAlias,
        latency_ms: float,
        usage: ModelUsage,
        retries: int,
    ) -> None:
        logger.info(
            "model_request_completed",
            extra={
                "capability": capability.value,
                "provider": ModelProvider.OPENAI_COMPATIBLE.value,
                "duration_ms": round(latency_ms, 3),
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "retry_count": retries,
            },
        )
