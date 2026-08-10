"""ModelGateway policy, failure, retry, and privacy tests."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from model_gateway import (
    CapabilityAlias,
    ChatMessage,
    ChatRequest,
    ChatRole,
    EmbeddingRequest,
    FakeModelGateway,
    FakeScenario,
    GatewayConfig,
    ModelErrorCode,
    ModelGatewayError,
    ModelProvider,
    OpenAICompatibleGateway,
    ReasoningEffort,
    ReasoningMode,
    ReasoningProfile,
    RerankRequest,
    create_model_gateway,
)
from model_gateway import openai_compatible as provider_module
from model_gateway.factory import _endpoint_allowed
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pytest import MonkeyPatch


def chat_request(content: str = "synthetic prompt") -> ChatRequest:
    return ChatRequest(messages=(ChatMessage(role=ChatRole.USER, content=content),))


@pytest.mark.parametrize(
    ("scenario", "code", "retryable"),
    [
        (FakeScenario.TIMEOUT, ModelErrorCode.TIMEOUT, True),
        (FakeScenario.RATE_LIMITED, ModelErrorCode.RATE_LIMITED, True),
        (FakeScenario.INVALID_RESPONSE, ModelErrorCode.INVALID_RESPONSE, False),
        (FakeScenario.UNAVAILABLE, ModelErrorCode.UNAVAILABLE, True),
    ],
)
async def test_fake_supports_deterministic_failure_scenarios(
    scenario: FakeScenario,
    code: ModelErrorCode,
    retryable: bool,
) -> None:
    gateway = FakeModelGateway(scenario=scenario)

    with pytest.raises(ModelGatewayError) as captured:
        await gateway.chat(chat_request())

    assert captured.value.code is code
    assert captured.value.retryable is retryable


async def test_fake_outputs_are_deterministic() -> None:
    gateway = FakeModelGateway(embedding_dimensions=4)

    first_chat = await gateway.chat(chat_request())
    second_chat = await gateway.chat(chat_request())
    first_embedding = await gateway.embed(EmbeddingRequest(texts=("合成文本",)))
    second_embedding = await gateway.embed(EmbeddingRequest(texts=("合成文本",)))

    assert first_chat == second_chat
    assert first_embedding == second_embedding


def test_fake_default_embedding_matches_initial_vector_schema() -> None:
    assert FakeModelGateway().embedding_dimensions == 768


@pytest.mark.parametrize(("enabled", "expected"), [(False, "disabled"), (True, "enabled")])
async def test_provider_sends_explicit_chat_reasoning_mode(enabled: bool, expected: str) -> None:
    payload: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = OpenAICompatibleGateway(
        endpoint="http://localhost:11434/v1",
        fast_chat_model="chat-model",
        embedding_model="embedding-model",
        fast_chat_reasoning_enabled=enabled,
        client=client,
    )

    await gateway.chat(chat_request())

    assert payload["thinking"] == {"type": expected}
    await client.aclose()


async def test_provider_profile_preserves_coarse_reasoning_mapping() -> None:
    payload: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = OpenAICompatibleGateway(
        endpoint="http://localhost:11434/v1",
        fast_chat_model="chat-model",
        embedding_model="embedding-model",
        client=client,
    )
    await gateway.chat(
        ChatRequest(
            messages=(ChatMessage(role=ChatRole.USER, content="synthetic prompt"),),
            reasoning_profile=ReasoningProfile(
                requested_effort=ReasoningEffort.HIGH,
                effective_effort=ReasoningEffort.HIGH,
                provider="openai-compatible",
                model="chat-model",
                mode=ReasoningMode.COARSE,
            ),
        )
    )
    assert payload["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in payload
    await client.aclose()


@pytest.mark.parametrize(
    ("requested_effort", "effective_effort"),
    (
        (ReasoningEffort.LOW, ReasoningEffort.LOW),
        (ReasoningEffort.MEDIUM, ReasoningEffort.MEDIUM),
        (ReasoningEffort.HIGH, ReasoningEffort.HIGH),
        (ReasoningEffort.XHIGH, ReasoningEffort.HIGH),
        (ReasoningEffort.MAX, ReasoningEffort.MAX),
    ),
)
async def test_provider_sends_native_deepseek_reasoning_effort(
    requested_effort: ReasoningEffort,
    effective_effort: ReasoningEffort,
) -> None:
    payload: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = OpenAICompatibleGateway(
        endpoint="http://localhost:11434/v1",
        fast_chat_model="deepseek-v4-flash",
        embedding_model="embedding-model",
        client=client,
    )
    await gateway.chat(
        ChatRequest(
            messages=(ChatMessage(role=ChatRole.USER, content="synthetic prompt"),),
            reasoning_profile=ReasoningProfile(
                requested_effort=requested_effort,
                effective_effort=effective_effort,
                provider="openai-compatible",
                model="deepseek-v4-flash",
                mapping_version="reasoning-mapping-v2",
                mode=ReasoningMode.NATIVE,
            ),
        )
    )
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == requested_effort.value
    await client.aclose()


async def test_provider_retries_rate_limit_then_succeeds() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(429, json={"error": "synthetic limit"})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = OpenAICompatibleGateway(
        endpoint="http://localhost:11434/v1",
        fast_chat_model="chat-model",
        embedding_model="embedding-model",
        max_retries=2,
        retry_backoff_seconds=0,
        client=client,
    )

    response = await gateway.chat(chat_request())

    assert response.text == "ok"
    assert calls == 3
    await client.aclose()


async def test_provider_does_not_retry_authentication_failure() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, json={"error": "synthetic auth"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = OpenAICompatibleGateway(
        endpoint="http://localhost:11434/v1",
        fast_chat_model="chat-model",
        embedding_model="embedding-model",
        client=client,
    )

    with pytest.raises(ModelGatewayError) as captured:
        await gateway.chat(chat_request())

    assert captured.value.code is ModelErrorCode.AUTHENTICATION
    assert captured.value.retryable is False
    assert calls == 1
    await client.aclose()


async def test_provider_does_not_retry_chat_read_timeout() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = OpenAICompatibleGateway(
        endpoint="http://localhost:11434/v1",
        fast_chat_model="chat-model",
        embedding_model="embedding-model",
        max_retries=1,
        retry_backoff_seconds=0,
        client=client,
    )

    with pytest.raises(ModelGatewayError) as captured:
        await gateway.chat(chat_request())

    assert captured.value.code is ModelErrorCode.TIMEOUT
    assert captured.value.retryable is True
    assert calls == 1
    await client.aclose()


async def test_provider_retries_embedding_read_timeout_with_a_finite_limit() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = OpenAICompatibleGateway(
        endpoint="http://localhost:11434/v1",
        fast_chat_model="chat-model",
        embedding_model="embedding-model",
        max_retries=1,
        retry_backoff_seconds=0,
        client=client,
    )

    with pytest.raises(ModelGatewayError) as captured:
        await gateway.embed(EmbeddingRequest(texts=("synthetic",)))

    assert captured.value.code is ModelErrorCode.TIMEOUT
    assert captured.value.retryable is True
    assert calls == 2
    await client.aclose()


async def test_provider_rejects_non_finite_embedding_values() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": [0.1, float("nan")]}],
                "usage": {"prompt_tokens": 1},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = OpenAICompatibleGateway(
        endpoint="http://localhost:11434/v1",
        fast_chat_model="chat-model",
        embedding_model="embedding-model",
        client=client,
    )

    with pytest.raises(ModelGatewayError) as captured:
        await gateway.embed(EmbeddingRequest(texts=("synthetic",)))

    assert captured.value.code is ModelErrorCode.INVALID_RESPONSE
    await client.aclose()


async def test_invalid_provider_response_does_not_leak_payload() -> None:
    private_response = "synthetic-private-provider-response"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": private_response})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = OpenAICompatibleGateway(
        endpoint="http://localhost:11434/v1",
        fast_chat_model="chat-model",
        embedding_model="embedding-model",
        client=client,
    )

    with pytest.raises(ModelGatewayError) as captured:
        await gateway.chat(chat_request("synthetic-private-prompt"))

    assert captured.value.code is ModelErrorCode.INVALID_RESPONSE
    assert private_response not in str(captured.value)
    await client.aclose()


async def test_model_logs_exclude_input_and_output(
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_input = "synthetic-private-model-input"
    private_output = "synthetic-private-model-output"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": private_output}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = OpenAICompatibleGateway(
        endpoint="http://localhost:11434/v1",
        fast_chat_model="chat-model",
        embedding_model="embedding-model",
        client=client,
    )

    with caplog.at_level(logging.INFO):
        await gateway.chat(chat_request(private_input))

    logs = "\n".join(record.getMessage() for record in caplog.records)
    assert private_input not in logs
    assert private_output not in logs
    assert "model_request_completed" in logs
    await client.aclose()


async def test_provider_span_contains_alias_and_usage_not_content(
    monkeypatch: MonkeyPatch,
) -> None:
    private_input = "synthetic-private-span-input"
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(provider_module, "tracer", provider.get_tracer("test.model"))

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = OpenAICompatibleGateway(
        endpoint="http://localhost:11434/v1",
        fast_chat_model="chat-model",
        embedding_model="embedding-model",
        client=client,
    )

    await gateway.chat(chat_request(private_input))

    spans = exporter.get_finished_spans()
    assert [span.name for span in spans] == ["model.chat"]
    attributes = dict(spans[0].attributes)
    assert attributes["model.capability"] == "fast_chat"
    assert attributes["gen_ai.usage.input_tokens"] == 3
    assert attributes["gen_ai.usage.output_tokens"] == 2
    assert private_input not in repr(attributes)
    await client.aclose()


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://models.example.test/v1",
        "ftp://localhost/models",
        "http://user:password@localhost:11434/v1",
        "http://localhost:11434/v1?api_key=secret",
    ],
)
async def test_external_or_unsafe_endpoint_is_denied_by_default(endpoint: str) -> None:
    gateway = create_model_gateway(
        GatewayConfig(
            provider=ModelProvider.OPENAI_COMPATIBLE,
            endpoint=endpoint,
            fast_chat_model="chat-model",
            embedding_model="embedding-model",
        )
    )

    assert gateway.status.available is False
    assert gateway.status.code == "MODEL_POLICY_DENIED"
    with pytest.raises(ModelGatewayError) as captured:
        await gateway.chat(chat_request())
    assert captured.value.code is ModelErrorCode.POLICY_DENIED


async def test_external_endpoint_requires_explicit_opt_in() -> None:
    gateway = create_model_gateway(
        GatewayConfig(
            provider=ModelProvider.OPENAI_COMPATIBLE,
            endpoint="https://models.example.test/v1",
            fast_chat_model="chat-model",
            embedding_model="embedding-model",
            allow_external=True,
        )
    )

    assert isinstance(gateway, OpenAICompatibleGateway)
    assert gateway.status.available is True
    await gateway.aclose()


async def test_real_chat_can_route_auxiliary_capabilities_to_fake() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://models.example.test/v1/chat/completions"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "real-chat"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = create_model_gateway(
        GatewayConfig(
            provider=ModelProvider.OPENAI_COMPATIBLE,
            fast_chat_endpoint="https://models.example.test/v1",
            fast_chat_model="chat-model",
            allow_external=True,
            fake_embedding=True,
            fake_reranker=True,
        ),
        client=client,
    )

    assert gateway.status.code == "MODEL_CAPABILITIES_ROUTED"
    assert gateway.status.provider is ModelProvider.OPENAI_COMPATIBLE
    assert gateway.status.capabilities == tuple(CapabilityAlias)
    assert (await gateway.chat(chat_request())).text == "real-chat"
    embedding = await gateway.embed(EmbeddingRequest(texts=("synthetic",)))
    assert len(embedding.vectors[0]) == 768
    reranked = await gateway.rerank(RerankRequest(query="synthetic", documents=("first", "second")))
    assert len(reranked.scores) == 2
    await gateway.aclose()
    await client.aclose()


async def test_real_chat_can_route_embedding_to_local_tei() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url == "https://models.example.test/v1/chat/completions":
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "real-chat"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                },
            )
        assert request.url == "http://tei/embed"
        assert request.content == b'{"inputs":["synthetic"],"dimensions":768}'
        return httpx.Response(200, json=[[0.0] * 768])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = create_model_gateway(
        GatewayConfig(
            provider=ModelProvider.OPENAI_COMPATIBLE,
            fast_chat_endpoint="https://models.example.test/v1",
            fast_chat_model="chat-model",
            embedding_provider=ModelProvider.TEXT_EMBEDDINGS_INFERENCE,
            embedding_endpoint="http://tei:80",
            embedding_model="Qwen/Qwen3-Embedding-0.6B",
            allow_external=True,
            fake_reranker=True,
        ),
        client=client,
    )

    assert gateway.status.code == "MODEL_CAPABILITIES_ROUTED"
    assert gateway.status.capabilities == tuple(CapabilityAlias)
    assert (await gateway.chat(chat_request())).text == "real-chat"
    embedding = await gateway.embed(EmbeddingRequest(texts=("synthetic",), dimensions=768))
    assert len(embedding.vectors[0]) == 768
    await gateway.aclose()
    await client.aclose()


async def test_local_endpoint_is_allowed_without_external_opt_in() -> None:
    gateway = create_model_gateway(
        GatewayConfig(
            provider=ModelProvider.OPENAI_COMPATIBLE,
            endpoint="http://127.0.0.1:11434/v1",
            fast_chat_model="chat-model",
            embedding_model="embedding-model",
        )
    )

    assert isinstance(gateway, OpenAICompatibleGateway)
    assert gateway.client is None
    await gateway.aclose()


def test_compose_reranker_endpoint_is_allowed_without_external_opt_in() -> None:
    assert _endpoint_allowed("http://tei-reranker:80", allow_external=False)


async def test_compose_tei_endpoint_is_allowed_without_external_opt_in() -> None:
    gateway = create_model_gateway(
        GatewayConfig(
            provider=ModelProvider.TEXT_EMBEDDINGS_INFERENCE,
            embedding_endpoint="http://tei:80",
            embedding_model="BAAI/bge-base-zh-v1.5",
        )
    )

    assert gateway.status.available is True
    assert gateway.status.code == "MODEL_EMBEDDING_CONFIGURED"
    await gateway.aclose()


async def test_missing_provider_configuration_is_explicitly_unavailable() -> None:
    gateway = create_model_gateway(GatewayConfig(provider=ModelProvider.OPENAI_COMPATIBLE))

    assert gateway.status.code == "MODEL_CONFIGURATION_MISSING"
    with pytest.raises(ModelGatewayError) as captured:
        await gateway.chat(chat_request())
    assert captured.value.code is ModelErrorCode.UNAVAILABLE


async def test_embedding_only_configuration_does_not_require_chat_model() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://127.0.0.1:11434/v1/embeddings"
        assert request.headers["authorization"] == "Bearer embedding-secret"
        return httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": [0.1, 0.2]}],
                "usage": {"prompt_tokens": 1},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = create_model_gateway(
        GatewayConfig(
            provider=ModelProvider.OPENAI_COMPATIBLE,
            embedding_endpoint="http://127.0.0.1:11434/v1",
            embedding_api_key="embedding-secret",
            embedding_model="embedding-model",
        ),
        client=client,
    )

    assert gateway.status.available is True
    assert gateway.status.code == "MODEL_EMBEDDING_CONFIGURED"
    assert gateway.status.capabilities == (CapabilityAlias.EMBEDDING_ZH,)
    assert gateway.status.for_capability(CapabilityAlias.FAST_CHAT).available is False
    response = await gateway.embed(EmbeddingRequest(texts=("synthetic",)))
    assert response.vectors == ((0.1, 0.2),)
    with pytest.raises(ModelGatewayError) as captured:
        await gateway.chat(chat_request())
    assert captured.value.code is ModelErrorCode.UNAVAILABLE
    await client.aclose()


async def test_text_embeddings_inference_provider_uses_embed_protocol() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://127.0.0.1:8080/embed"
        assert request.content == b'{"inputs":["synthetic"]}'
        return httpx.Response(200, json=[[0.1, 0.2]])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = create_model_gateway(
        GatewayConfig(
            provider=ModelProvider.TEXT_EMBEDDINGS_INFERENCE,
            embedding_endpoint="http://127.0.0.1:8080",
            embedding_model="BAAI/bge-base-zh-v1.5",
        ),
        client=client,
    )

    assert gateway.status.provider is ModelProvider.TEXT_EMBEDDINGS_INFERENCE
    assert gateway.status.capabilities == (CapabilityAlias.EMBEDDING_ZH,)
    response = await gateway.embed(EmbeddingRequest(texts=("synthetic",)))
    assert response.vectors == ((0.1, 0.2),)
    await client.aclose()


async def test_text_embeddings_inference_requests_explicit_dimensions() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.content == b'{"inputs":["synthetic"],"dimensions":768}'
        return httpx.Response(200, json=[[0.0] * 768])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = create_model_gateway(
        GatewayConfig(
            provider=ModelProvider.TEXT_EMBEDDINGS_INFERENCE,
            embedding_endpoint="http://127.0.0.1:8080",
            embedding_model="Qwen/Qwen3-Embedding-0.6B",
        ),
        client=client,
    )

    response = await gateway.embed(EmbeddingRequest(texts=("synthetic",), dimensions=768))

    assert len(response.vectors[0]) == 768
    await client.aclose()


def test_embedding_request_rejects_non_positive_dimensions() -> None:
    with pytest.raises(ValueError, match="dimensions must be positive"):
        EmbeddingRequest(texts=("synthetic",), dimensions=0)


async def test_endpoint_policy_is_evaluated_per_capability() -> None:
    gateway = create_model_gateway(
        GatewayConfig(
            provider=ModelProvider.OPENAI_COMPATIBLE,
            fast_chat_endpoint="https://models.example.test/v1",
            fast_chat_model="chat-model",
            embedding_endpoint="http://127.0.0.1:11434/v1",
            embedding_model="embedding-model",
        )
    )

    assert gateway.status.capabilities == (CapabilityAlias.EMBEDDING_ZH,)
    chat_status = gateway.status.for_capability(CapabilityAlias.FAST_CHAT)
    assert chat_status.available is False
    assert chat_status.code == "MODEL_POLICY_DENIED"
    with pytest.raises(ModelGatewayError) as captured:
        await gateway.chat(chat_request())
    assert captured.value.code is ModelErrorCode.POLICY_DENIED
    assert isinstance(gateway, OpenAICompatibleGateway)
    await gateway.aclose()


async def test_disabled_provider_returns_explicit_unavailable_error() -> None:
    gateway = create_model_gateway(GatewayConfig(provider=ModelProvider.DISABLED))

    assert gateway.status.code == "MODEL_DISABLED"
    with pytest.raises(ModelGatewayError) as captured:
        await gateway.embed(EmbeddingRequest(texts=("synthetic",)))
    assert captured.value.code is ModelErrorCode.UNAVAILABLE
    assert captured.value.capability is CapabilityAlias.EMBEDDING_ZH
