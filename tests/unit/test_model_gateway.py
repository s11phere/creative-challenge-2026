"""ModelGateway policy, failure, retry, and privacy tests."""

from __future__ import annotations

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
    create_model_gateway,
)
from model_gateway import openai_compatible as provider_module
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


async def test_provider_retries_timeout_with_a_finite_limit() -> None:
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


async def test_missing_provider_configuration_is_explicitly_unavailable() -> None:
    gateway = create_model_gateway(GatewayConfig(provider=ModelProvider.OPENAI_COMPATIBLE))

    assert gateway.status.code == "MODEL_CONFIGURATION_MISSING"
    with pytest.raises(ModelGatewayError) as captured:
        await gateway.chat(chat_request())
    assert captured.value.code is ModelErrorCode.UNAVAILABLE


async def test_disabled_provider_returns_explicit_unavailable_error() -> None:
    gateway = create_model_gateway(GatewayConfig(provider=ModelProvider.DISABLED))

    assert gateway.status.code == "MODEL_DISABLED"
    with pytest.raises(ModelGatewayError) as captured:
        await gateway.embed(EmbeddingRequest(texts=("synthetic",)))
    assert captured.value.code is ModelErrorCode.UNAVAILABLE
    assert captured.value.capability is CapabilityAlias.EMBEDDING_ZH
