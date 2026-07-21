from __future__ import annotations

import pytest
from application.retrieval import GatewayQueryTextEmbedder
from domain.retrieval import RetrievalError, RetrievalErrorCode
from model_gateway import FakeModelGateway, FakeScenario


async def test_gateway_query_embedder_maps_fake_vectors() -> None:
    vectors = await GatewayQueryTextEmbedder(FakeModelGateway()).embed(("private query",))
    assert len(vectors) == 1
    assert len(vectors[0]) == 768


async def test_gateway_query_embedder_maps_provider_failure_without_content() -> None:
    embedder = GatewayQueryTextEmbedder(FakeModelGateway(scenario=FakeScenario.UNAVAILABLE))
    with pytest.raises(RetrievalError) as captured:
        await embedder.embed(("private query",))
    assert captured.value.code is RetrievalErrorCode.EMBEDDING_UNAVAILABLE
    assert captured.value.retryable is True
    assert "private query" not in str(captured.value)
