"""OpenAI-compatible HTTP ModelGateway adapter."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from dataclasses import dataclass
from time import perf_counter
from typing import Any

import httpx
from domain.reasoning import ReasoningEffort, ReasoningMode
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
        fast_chat_timeout_seconds: float = 120.0,
        fast_chat_reasoning_enabled: bool = False,
        fast_chat_prompt_caching: bool = True,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.1,
        reranker_batch_size: int = 32,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if (
            timeout_seconds <= 0
            or fast_chat_timeout_seconds <= 0
            or max_retries < 0
            or retry_backoff_seconds < 0
            or reranker_batch_size < 1
        ):
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
        self.reranker_batch_size = reranker_batch_size
        self.fast_chat_timeout_seconds = fast_chat_timeout_seconds
        self.fast_chat_reasoning_enabled = fast_chat_reasoning_enabled
        self.fast_chat_prompt_caching = fast_chat_prompt_caching
        self._owns_client = client is None
        self.client = client

    @property
    def status(self) -> GatewayStatus:
        statuses = tuple(
            CapabilityStatus(
                capability=capability,
                available=config.available,
                code="MODEL_CAPABILITY_CONFIGURED" if config.available else config.status_code,
                supports_native_tool_use=(
                    config.available and capability is CapabilityAlias.FAST_CHAT
                ),
                supports_prompt_caching=(
                    config.available
                    and capability is CapabilityAlias.FAST_CHAT
                    and self.fast_chat_prompt_caching
                ),
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
            model_identity=self.fast_chat_model or "unconfigured",
            reasoning_enabled_by_default=self.fast_chat_reasoning_enabled,
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
            payload: dict[str, Any] = {
                "model": config.model,
                "messages": [
                    {"role": message.role.value, "content": message.content}
                    for message in request.messages
                ]
                + self._tool_result_messages(request),
                "temperature": request.temperature,
                **({"max_tokens": request.max_tokens} if request.max_tokens is not None else {}),
                "thinking": self._thinking_value(request),
            }
            if self.fast_chat_prompt_caching and request.cache_key is not None:
                payload["extra_body"] = {"cache_key": request.cache_key}
            if request.tools:
                payload["tools"] = [
                    {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": dict(tool.input_schema),
                        },
                    }
                    for tool in request.tools
                ]
                payload["tool_choice"] = "auto"
            reasoning_effort = self._native_reasoning_effort(request)
            if reasoning_effort is not None:
                payload["reasoning_effort"] = reasoning_effort
            data, retries = await self._request_json(
                "chat/completions",
                payload,
                capability=capability,
                timeout_seconds=self.fast_chat_timeout_seconds,
                retry_read_timeouts=False,
            )
            text, finish_reason, tool_calls = self._parse_chat(
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
                tool_calls=tool_calls,
            )

    @staticmethod
    def _tool_result_messages(request: ChatRequest) -> list[dict[str, Any]]:
        """Replay server-owned Tool results without retaining provider SDK objects."""
        messages: list[dict[str, Any]] = []
        call_by_id = {call.call_id: call for call in request.tool_call_history}
        for result in request.tool_results:
            call = call_by_id.get(result.call_id)
            if call is None or call.tool_name != result.tool_name:
                raise ModelGatewayError(
                    ModelErrorCode.INVALID_RESPONSE,
                    "Tool result replay does not match a prior Tool call.",
                    retryable=False,
                    capability=CapabilityAlias.FAST_CHAT,
                )
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": result.call_id,
                            "type": "function",
                            "function": {
                                "name": result.tool_name,
                                "arguments": json.dumps(
                                    call.arguments,
                                    ensure_ascii=False,
                                    allow_nan=False,
                                    separators=(",", ":"),
                                    sort_keys=True,
                                ),
                            },
                        }
                    ],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": result.call_id,
                    "name": result.tool_name,
                    "content": json.dumps(
                        result.observation,
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                }
            )
        return messages

    def _thinking_value(self, request: ChatRequest) -> dict[str, str]:
        profile = request.reasoning_profile
        if profile is None:
            return {"type": "enabled" if self.fast_chat_reasoning_enabled else "disabled"}
        return {"type": "disabled" if profile.mode.value == "disabled" else "enabled"}

    @staticmethod
    def _native_reasoning_effort(request: ChatRequest) -> str | None:
        profile = request.reasoning_profile
        if profile is None or profile.mode is not ReasoningMode.NATIVE:
            return None
        if profile.requested_effort in {
            ReasoningEffort.LOW,
            ReasoningEffort.MEDIUM,
            ReasoningEffort.HIGH,
            ReasoningEffort.XHIGH,
            ReasoningEffort.MAX,
        }:
            return str(profile.requested_effort.value)
        return str(profile.effective_effort.value)

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
                    {
                        "model": config.model,
                        "input": list(request.texts),
                        **(
                            {"dimensions": request.dimensions}
                            if request.dimensions is not None
                            else {}
                        ),
                    }
                    if self._embedding_protocol == "openai-compatible"
                    else {
                        "inputs": list(request.texts),
                        **(
                            {"dimensions": request.dimensions}
                            if request.dimensions is not None
                            else {}
                        ),
                    }
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
            scores: list[RerankScore] = []
            total_usage = ModelUsage()
            retries = 0
            for offset in range(0, len(request.documents), self.reranker_batch_size):
                batch = request.documents[offset : offset + self.reranker_batch_size]
                data, batch_retries = await self._request_json(
                    "rerank",
                    {
                        "query": request.query,
                        "texts": list(batch),
                        "raw_scores": False,
                        **(
                            {}
                            if self._provider is ModelProvider.TEXT_EMBEDDINGS_INFERENCE
                            else {"model": config.model}
                        ),
                    },
                    capability=capability,
                )
                batch_scores = self._parse_rerank(data, len(batch), capability)
                scores.extend(
                    RerankScore(index=offset + score.index, score=score.score)
                    for score in batch_scores
                )
                batch_usage = self._parse_usage(
                    data if isinstance(data, dict) else {}, embedding=False
                )
                total_usage = ModelUsage(
                    input_tokens=total_usage.input_tokens + batch_usage.input_tokens,
                    output_tokens=total_usage.output_tokens + batch_usage.output_tokens,
                )
                retries += batch_retries
            usage = total_usage
            latency_ms = (perf_counter() - started_at) * 1000
            span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
            self._log_success(capability, latency_ms, usage, retries)
            return RerankResponse(
                scores=tuple(scores),
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
        timeout_seconds: float | None = None,
        retry_read_timeouts: bool = True,
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
                    timeout=timeout_seconds or self.timeout_seconds,
                )
                error = self._http_error(response.status_code, capability)
                if error is not None:
                    raise error
                parsed = response.json()
                if not isinstance(parsed, (dict, list)):
                    raise self._invalid_response(capability)
                return parsed, attempt
            except httpx.ReadTimeout as exc:
                error = ModelGatewayError(
                    ModelErrorCode.TIMEOUT,
                    "The model request timed out.",
                    retryable=True,
                    capability=capability,
                )
                cause: Exception = exc
                should_retry = retry_read_timeouts
            except httpx.TimeoutException as exc:
                error = ModelGatewayError(
                    ModelErrorCode.TIMEOUT,
                    "The model request timed out.",
                    retryable=True,
                    capability=capability,
                )
                cause = exc
                should_retry = True
            except httpx.TransportError as exc:
                error = ModelGatewayError(
                    ModelErrorCode.UNAVAILABLE,
                    "The model provider is unavailable.",
                    retryable=True,
                    capability=capability,
                )
                cause = exc
                should_retry = True
            except ModelGatewayError as exc:
                error = exc
                cause = exc
                should_retry = error.retryable
            except ValueError as exc:
                error = self._invalid_response(capability)
                cause = exc
                should_retry = False

            if not error.retryable or not should_retry or attempt >= self.max_retries:
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
    def _invalid_response(
        capability: CapabilityAlias, *, debug_details: Any = None
    ) -> ModelGatewayError:
        return ModelGatewayError(
            ModelErrorCode.INVALID_RESPONSE,
            "The model provider returned an invalid response.",
            retryable=False,
            capability=capability,
            debug_details=debug_details,
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
    ) -> tuple[str, str | None, tuple[ChatToolCall, ...]]:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise cls._invalid_response(capability, debug_details=data)
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise cls._invalid_response(capability, debug_details=data)
        content = message.get("content", "")
        if content is None:
            content = ""
        if not isinstance(content, str):
            raise cls._invalid_response(capability, debug_details=data)
        raw_calls = message.get("tool_calls", [])
        if not isinstance(raw_calls, list):
            raise cls._invalid_response(capability, debug_details=data)
        calls: list[ChatToolCall] = []
        for item in raw_calls:
            if not isinstance(item, dict) or item.get("type") != "function":
                raise cls._invalid_response(capability, debug_details=data)
            function = item.get("function")
            if not isinstance(function, dict):
                raise cls._invalid_response(capability, debug_details=data)
            call_id = item.get("id")
            name = function.get("name")
            arguments = function.get("arguments")
            if (
                not isinstance(call_id, str)
                or not isinstance(name, str)
                or not isinstance(arguments, str)
            ):
                raise cls._invalid_response(capability, debug_details=data)
            try:
                parsed_arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise cls._invalid_response(capability, debug_details=data) from exc
            if not isinstance(parsed_arguments, dict):
                raise cls._invalid_response(capability, debug_details=data)
            try:
                calls.append(
                    ChatToolCall(call_id=call_id, tool_name=name, arguments=parsed_arguments)
                )
            except ValueError as exc:
                raise cls._invalid_response(capability, debug_details=data) from exc
        if not content and not calls:
            raise cls._invalid_response(capability, debug_details=data)
        finish_reason = choices[0].get("finish_reason")
        if finish_reason is not None and not isinstance(finish_reason, str):
            raise cls._invalid_response(capability, debug_details=data)
        return content, finish_reason, tuple(calls)

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
        raw_details = usage.get("prompt_tokens_details", {})
        if raw_details is None:
            raw_details = {}
        if not isinstance(raw_details, dict):
            raise cls._invalid_response(capability)
        cached_input_tokens = cls._token_count(raw_details.get("cached_tokens", 0), capability)
        cache_write_input_tokens = cls._token_count(
            usage.get("cache_creation_input_tokens", 0), capability
        )
        return ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_write_input_tokens=cache_write_input_tokens,
        )

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
