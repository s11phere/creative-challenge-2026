"""OpenAI-compatible HTTP ModelGateway adapter."""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from time import perf_counter
from typing import Any

import httpx
from opentelemetry import trace
from opentelemetry.trace import SpanKind

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
    ModelUsage,
    RerankRequest,
    RerankResponse,
    RerankScore,
)

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("model_gateway.openai_compatible")


@dataclass(frozen=True)
class _CapabilityConfig:
    endpoint: httpx.URL | None
    model: str | None
    api_key: str | None
    status_code: str
    error_code: ModelErrorCode

    @property
    def available(self) -> bool:
        return self.endpoint is not None and self.model is not None


class OpenAICompatibleGateway:
    def __init__(
        self,
        *,
        endpoint: str | None = None,
        fast_chat_model: str | None = None,
        embedding_model: str | None = None,
        api_key: str | None = None,
        fast_chat_endpoint: str | None = None,
        embedding_endpoint: str | None = None,
        fast_chat_api_key: str | None = None,
        embedding_api_key: str | None = None,
        reranker_endpoint: str | None = None,
        reranker_model: str | None = None,
        reranker_api_key: str | None = None,
        fast_chat_status_code: str = "MODEL_CONFIGURATION_MISSING",
        embedding_status_code: str = "MODEL_CONFIGURATION_MISSING",
        fast_chat_error_code: ModelErrorCode = ModelErrorCode.UNAVAILABLE,
        embedding_error_code: ModelErrorCode = ModelErrorCode.UNAVAILABLE,
        reranker_status_code: str = "MODEL_CONFIGURATION_MISSING",
        reranker_error_code: ModelErrorCode = ModelErrorCode.UNAVAILABLE,
        provider: ModelProvider = ModelProvider.OPENAI_COMPATIBLE,
        embedding_protocol: str = "openai-compatible",
        timeout_seconds: float = 15.0,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.1,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if timeout_seconds <= 0 or max_retries < 0 or retry_backoff_seconds < 0:
            raise ValueError("Model timeout must be positive and retry settings non-negative")
        resolved_chat_endpoint = fast_chat_endpoint or endpoint
        resolved_embedding_endpoint = embedding_endpoint or endpoint
        self._capability_configs = {
            CapabilityAlias.FAST_CHAT: self._capability_config(
                resolved_chat_endpoint,
                fast_chat_model,
                fast_chat_api_key if fast_chat_api_key is not None else api_key,
                fast_chat_status_code,
                fast_chat_error_code,
            ),
            CapabilityAlias.EMBEDDING_ZH: self._capability_config(
                resolved_embedding_endpoint,
                embedding_model,
                embedding_api_key if embedding_api_key is not None else api_key,
                embedding_status_code,
                embedding_error_code,
            ),
            CapabilityAlias.RERANKER_MULTILINGUAL: self._capability_config(
                reranker_endpoint or resolved_chat_endpoint,
                reranker_model,
                reranker_api_key if reranker_api_key is not None else api_key,
                reranker_status_code,
                reranker_error_code,
            ),
        }
        self.endpoint = httpx.URL(endpoint.rstrip("/") + "/") if endpoint else None
        self.fast_chat_model = fast_chat_model
        self.embedding_model = embedding_model
        if embedding_protocol not in {"openai-compatible", "tei"}:
            raise ValueError("Unsupported embedding protocol")
        self._provider = provider
        self._embedding_protocol = embedding_protocol
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.timeout_seconds = timeout_seconds
        self._owns_client = client is None
        self.client = client

    @property
    def status(self) -> GatewayStatus:
        statuses = tuple(
            CapabilityStatus(
                capability=capability,
                available=config.available,
                code="MODEL_CAPABILITY_CONFIGURED" if config.available else config.status_code,
            )
            for capability, config in self._capability_configs.items()
        )
        capabilities = tuple(status.capability for status in statuses if status.available)
        if len(capabilities) == len(self._capability_configs):
            code = "MODEL_PROVIDER_CONFIGURED"
        elif capabilities == (CapabilityAlias.EMBEDDING_ZH,):
            code = "MODEL_EMBEDDING_CONFIGURED"
        elif capabilities == (CapabilityAlias.FAST_CHAT,):
            code = "MODEL_CHAT_CONFIGURED"
        elif capabilities == (CapabilityAlias.RERANKER_MULTILINGUAL,):
            code = "MODEL_RERANKER_CONFIGURED"
        else:
            code = (
                "MODEL_POLICY_DENIED"
                if any(status.code == "MODEL_POLICY_DENIED" for status in statuses)
                else "MODEL_CONFIGURATION_MISSING"
            )
        return GatewayStatus(
            available=bool(capabilities),
            code=code,
            provider=self._provider,
            capabilities=capabilities,
            capability_statuses=statuses,
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
        config = self._require_capability(capability)
        started_at = perf_counter()
        with tracer.start_as_current_span(
            "model.chat",
            kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": self._provider.value,
                "model.capability": capability.value,
            },
        ) as span:
            data, retries = await self._request_json(
                "chat/completions",
                {
                    "model": config.model,
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
            text, finish_reason = self._parse_chat(
                data if isinstance(data, dict) else {},
                capability,
            )
            usage = self._parse_usage(
                data if isinstance(data, dict) else {},
                embedding=False,
            )
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
        config = self._require_capability(capability)
        started_at = perf_counter()
        with tracer.start_as_current_span(
            "model.embedding",
            kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.operation.name": "embeddings",
                "gen_ai.provider.name": self._provider.value,
                "model.capability": capability.value,
            },
        ) as span:
            data, retries = await self._request_json(
                "embeddings" if self._embedding_protocol == "openai-compatible" else "embed",
                (
                    {"model": config.model, "input": list(request.texts)}
                    if self._embedding_protocol == "openai-compatible"
                    else {"inputs": list(request.texts)}
                ),
                capability=capability,
            )
            vectors = (
                self._parse_embeddings(
                    data if isinstance(data, dict) else {},
                    len(request.texts),
                    capability,
                )
                if self._embedding_protocol == "openai-compatible"
                else self._parse_tei_embeddings(data, len(request.texts), capability)
            )
            usage = self._parse_usage(
                data if isinstance(data, dict) else {},
                embedding=True,
            )
            latency_ms = (perf_counter() - started_at) * 1000
            span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
            self._log_success(capability, latency_ms, usage, retries)
            return EmbeddingResponse(
                vectors=vectors,
                usage=usage,
                capability=capability,
                latency_ms=latency_ms,
            )

    async def rerank(
        self,
        request: RerankRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.RERANKER_MULTILINGUAL,
    ) -> RerankResponse:
        if capability is not CapabilityAlias.RERANKER_MULTILINGUAL:
            raise self._unsupported(capability)
        config = self._require_capability(capability)
        started_at = perf_counter()
        with tracer.start_as_current_span(
            "model.rerank",
            kind=SpanKind.CLIENT,
            attributes={
                "gen_ai.operation.name": "rerank",
                "gen_ai.provider.name": self._provider.value,
                "model.capability": capability.value,
            },
        ) as span:
            data, retries = await self._request_json(
                "rerank",
                {
                    "query": request.query,
                    "texts": list(request.documents),
                    "raw_scores": False,
                    **(
                        {}
                        if self._provider is ModelProvider.TEXT_EMBEDDINGS_INFERENCE
                        else {"model": config.model}
                    ),
                },
                capability=capability,
            )
            scores = self._parse_rerank(data, len(request.documents), capability)
            usage = self._parse_usage(data if isinstance(data, dict) else {}, embedding=False)
            latency_ms = (perf_counter() - started_at) * 1000
            span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
            self._log_success(capability, latency_ms, usage, retries)
            return RerankResponse(
                scores=scores,
                model_version=config.model or "unknown",
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
    ) -> tuple[dict[str, Any] | list[Any], int]:
        config = self._require_capability(capability)
        assert config.endpoint is not None
        client = self._client()
        headers = {"Authorization": f"Bearer {config.api_key}"} if config.api_key else None
        for attempt in range(self.max_retries + 1):
            try:
                response = await client.post(
                    config.endpoint.join(path),
                    json=payload,
                    headers=headers,
                )
                error = self._http_error(response.status_code, capability)
                if error is not None:
                    raise error
                parsed = response.json()
                if not isinstance(parsed, (dict, list)):
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
                    "provider": self._provider.value,
                    "retry_count": attempt + 1,
                    "error_type": error.code.value,
                },
            )
            await asyncio.sleep(self.retry_backoff_seconds * (2**attempt))
        raise AssertionError("Retry loop exhausted without returning or raising")

    def _client(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_seconds),
            )
        return self.client

    @staticmethod
    def _capability_config(
        endpoint: str | None,
        model: str | None,
        api_key: str | None,
        status_code: str,
        error_code: ModelErrorCode,
    ) -> _CapabilityConfig:
        configured_endpoint = httpx.URL(endpoint.rstrip("/") + "/") if endpoint and model else None
        return _CapabilityConfig(
            endpoint=configured_endpoint,
            model=model if configured_endpoint is not None else None,
            api_key=api_key,
            status_code=status_code,
            error_code=error_code,
        )

    def _require_capability(self, capability: CapabilityAlias) -> _CapabilityConfig:
        config = self._capability_configs[capability]
        if config.available:
            return config
        message = (
            "The model capability is blocked by the data policy."
            if config.error_code is ModelErrorCode.POLICY_DENIED
            else "The model capability is not configured."
        )
        raise ModelGatewayError(
            config.error_code,
            message,
            retryable=False,
            capability=capability,
        )

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
    def _parse_tei_embeddings(
        cls,
        payload: dict[str, Any] | list[Any],
        expected_count: int,
        capability: CapabilityAlias,
    ) -> tuple[tuple[float, ...], ...]:
        values: Any = payload.get("embeddings") if isinstance(payload, dict) else payload
        if not isinstance(values, list) or len(values) != expected_count:
            raise cls._invalid_response(capability)
        vectors: list[tuple[float, ...]] = []
        for value in values:
            if not isinstance(value, list) or not value:
                raise cls._invalid_response(capability)
            if any(
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or not math.isfinite(float(item))
                for item in value
            ):
                raise cls._invalid_response(capability)
            vectors.append(tuple(float(item) for item in value))
        if len({len(vector) for vector in vectors}) != 1:
            raise cls._invalid_response(capability)
        return tuple(vectors)

    @classmethod
    def _parse_rerank(
        cls,
        payload: dict[str, Any] | list[Any],
        expected_count: int,
        capability: CapabilityAlias,
    ) -> tuple[RerankScore, ...]:
        values: Any = (
            payload.get("results", payload.get("data")) if isinstance(payload, dict) else payload
        )
        if not isinstance(values, list) or len(values) != expected_count:
            raise cls._invalid_response(capability)
        scores: list[RerankScore] = []
        for value in values:
            if not isinstance(value, dict):
                raise cls._invalid_response(capability)
            index = value.get("index")
            score = value.get("relevance_score", value.get("score"))
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(float(score))
            ):
                raise cls._invalid_response(capability)
            scores.append(RerankScore(index=index, score=float(score)))
        if {score.index for score in scores} != set(range(expected_count)):
            raise cls._invalid_response(capability)
        return tuple(scores)

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

    def _log_success(
        self,
        capability: CapabilityAlias,
        latency_ms: float,
        usage: ModelUsage,
        retries: int,
    ) -> None:
        logger.info(
            "model_request_completed",
            extra={
                "capability": capability.value,
                "provider": self._provider.value,
                "duration_ms": round(latency_ms, 3),
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "retry_count": retries,
            },
        )
