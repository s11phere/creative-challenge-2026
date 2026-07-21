"""Shared Chat and Embedding contracts for every ModelGateway implementation."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
from model_gateway import (
    CapabilityAlias,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatRole,
    EmbeddingRequest,
    EmbeddingResponse,
    FakeModelGateway,
    ModelGateway,
    OpenAICompatibleGateway,
    RerankRequest,
    RerankResponse,
)

GatewayFactory = Callable[[], tuple[ModelGateway, httpx.AsyncClient | None]]


async def provider_stub(request: httpx.Request) -> httpx.Response:
    payload = json.loads(request.content)
    if request.url.path.endswith("/chat/completions"):
        assert payload["model"] == "configured-chat-model"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": "stub answer"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2},
            },
        )
    if request.url.path.endswith("/rerank"):
        assert payload["model"] == "configured-reranker-model"
        assert payload["texts"] == ["alpha", "beta"]
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.2},
                    {"index": 0, "relevance_score": 0.8},
                ],
                "usage": {"prompt_tokens": 4},
            },
        )
    assert request.url.path.endswith("/embeddings")
    assert payload["model"] == "configured-embedding-model"
    return httpx.Response(
        200,
        json={
            "data": [
                {"index": 1, "embedding": [0.3, 0.4]},
                {"index": 0, "embedding": [0.1, 0.2]},
            ],
            "usage": {"prompt_tokens": 3, "total_tokens": 3},
        },
    )


def fake_factory() -> tuple[ModelGateway, None]:
    return FakeModelGateway(embedding_dimensions=2), None


def provider_factory() -> tuple[ModelGateway, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(provider_stub))
    return (
        OpenAICompatibleGateway(
            endpoint="http://127.0.0.1:11434/v1",
            fast_chat_model="configured-chat-model",
            embedding_model="configured-embedding-model",
            reranker_model="configured-reranker-model",
            client=client,
        ),
        client,
    )


@pytest.mark.parametrize("gateway_factory", [fake_factory, provider_factory])
async def test_chat_contract(gateway_factory: GatewayFactory) -> None:
    gateway, client = gateway_factory()
    try:
        response = await gateway.chat(
            ChatRequest(messages=(ChatMessage(role=ChatRole.USER, content="synthetic question"),))
        )

        assert isinstance(response, ChatResponse)
        assert response.text
        assert response.capability is CapabilityAlias.FAST_CHAT
        assert response.usage.input_tokens >= 0
        assert response.usage.output_tokens >= 0
        assert response.usage.total_tokens >= response.usage.input_tokens
        assert response.latency_ms >= 0
    finally:
        if client is not None:
            await client.aclose()


@pytest.mark.parametrize("gateway_factory", [fake_factory, provider_factory])
async def test_embedding_contract(gateway_factory: GatewayFactory) -> None:
    gateway, client = gateway_factory()
    try:
        response = await gateway.embed(
            EmbeddingRequest(texts=("synthetic alpha", "synthetic beta"))
        )

        assert isinstance(response, EmbeddingResponse)
        assert response.capability is CapabilityAlias.EMBEDDING_ZH
        assert len(response.vectors) == 2
        assert {len(vector) for vector in response.vectors} == {2}
        assert all(isinstance(value, float) for vector in response.vectors for value in vector)
        assert response.usage.input_tokens >= 0
        assert response.latency_ms >= 0
    finally:
        if client is not None:
            await client.aclose()


@pytest.mark.parametrize("gateway_factory", [fake_factory, provider_factory])
async def test_reranker_contract(gateway_factory: GatewayFactory) -> None:
    gateway, client = gateway_factory()
    try:
        response = await gateway.rerank(
            RerankRequest(query="synthetic question", documents=("alpha", "beta"))
        )

        assert isinstance(response, RerankResponse)
        assert response.capability is CapabilityAlias.RERANKER_MULTILINGUAL
        assert {score.index for score in response.scores} == {0, 1}
        assert response.model_version
        assert response.latency_ms >= 0
    finally:
        if client is not None:
            await client.aclose()
