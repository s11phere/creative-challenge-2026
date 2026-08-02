from __future__ import annotations

import asyncio
import json
from uuid import UUID

import httpx
import pytest
from application.retrieval import GatewayReranker, GatewayRerankerConfig
from domain.retrieval import (
    RerankDocument,
    RerankRequest,
    RetrievalError,
    RetrievalErrorCode,
)
from model_gateway import (
    CapabilityAlias,
    FakeModelGateway,
    FakeScenario,
    GatewayConfig,
    ModelErrorCode,
    ModelGatewayError,
    ModelProvider,
    OpenAICompatibleGateway,
    create_model_gateway,
)
from model_gateway import (
    RerankRequest as GatewayRerankRequest,
)


def _request() -> RerankRequest:
    return RerankRequest(
        query="synthetic query",
        documents=(
            RerankDocument(index=0, chunk_id=UUID(int=1), text="first private text"),
            RerankDocument(index=1, chunk_id=UUID(int=2), text="second private text"),
        ),
    )


async def test_gateway_reranker_maps_deterministic_fake_response() -> None:
    response = await GatewayReranker(FakeModelGateway()).rerank(_request())

    assert [(score.index, score.score) for score in response.scores] == [(0, -0.0), (1, -1.0)]
    assert response.model_version == "fake-reranker-v1"


async def test_gateway_reranker_maps_provider_failure_without_content() -> None:
    reranker = GatewayReranker(FakeModelGateway(scenario=FakeScenario.UNAVAILABLE))

    with pytest.raises(RetrievalError) as captured:
        await reranker.rerank(_request())

    assert captured.value.code is RetrievalErrorCode.RERANKER_UNAVAILABLE
    assert captured.value.retryable is True
    assert "private text" not in str(captured.value)


async def test_gateway_reranker_timeout_is_bounded() -> None:
    class SlowGateway(FakeModelGateway):
        async def rerank(self, request, *, capability):  # type: ignore[no-untyped-def]
            await asyncio.sleep(0.05)
            return await super().rerank(request, capability=capability)

    reranker = GatewayReranker(SlowGateway(), config=GatewayRerankerConfig(timeout_seconds=0.01))

    with pytest.raises(RetrievalError) as captured:
        await reranker.rerank(_request())

    assert captured.value.code is RetrievalErrorCode.RERANKER_UNAVAILABLE
    assert captured.value.retryable is True


async def test_http_reranker_rejects_duplicate_or_missing_indices() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[{"index": 0, "score": 0.9}, {"index": 0, "score": 0.8}],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = OpenAICompatibleGateway(
        reranker_endpoint="http://127.0.0.1:8081",
        reranker_model="BAAI/bge-reranker-base@fixed-revision",
        client=client,
    )
    try:
        with pytest.raises(ModelGatewayError) as captured:
            await gateway.rerank(GatewayRerankRequest(query="query", documents=("first", "second")))
        assert captured.value.code is ModelErrorCode.INVALID_RESPONSE
    finally:
        await client.aclose()


async def test_tei_reranker_uses_local_compose_endpoint_and_protocol() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "http://reranker/rerank"
        assert request.content == (
            b'{"query":"query","texts":["first","second"],"raw_scores":false}'
        )
        return httpx.Response(
            200,
            json=[{"index": 1, "score": 0.2}, {"index": 0, "score": 0.9}],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = create_model_gateway(
        GatewayConfig(
            provider=ModelProvider.TEXT_EMBEDDINGS_INFERENCE,
            reranker_endpoint="http://reranker:80",
            reranker_model="BAAI/bge-reranker-base@fixed-revision",
        ),
        client=client,
    )
    try:
        assert gateway.status.capabilities == (CapabilityAlias.RERANKER_MULTILINGUAL,)
        response = await gateway.rerank(
            GatewayRerankRequest(query="query", documents=("first", "second"))
        )
        assert [(score.index, score.score) for score in response.scores] == [(1, 0.2), (0, 0.9)]
    finally:
        await client.aclose()


async def test_http_reranker_splits_batches_and_restores_global_indices() -> None:
    requests: list[list[str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        texts = payload["texts"]
        requests.append(texts)
        return httpx.Response(
            200,
            json=[
                {"index": index, "score": float(len(texts) - index)} for index in range(len(texts))
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gateway = OpenAICompatibleGateway(
        reranker_endpoint="http://127.0.0.1:8081",
        reranker_model="BAAI/bge-reranker-base@fixed-revision",
        reranker_batch_size=2,
        client=client,
    )
    try:
        response = await gateway.rerank(
            GatewayRerankRequest(
                query="query",
                documents=("first", "second", "third", "fourth", "fifth"),
            )
        )
        assert requests == [["first", "second"], ["third", "fourth"], ["fifth"]]
        assert [(score.index, score.score) for score in response.scores] == [
            (0, 2.0),
            (1, 1.0),
            (2, 2.0),
            (3, 1.0),
            (4, 1.0),
        ]
    finally:
        await client.aclose()
