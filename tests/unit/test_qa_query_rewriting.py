"""Tests for the LLM-backed query rewriter used by the QA planning stage."""

from __future__ import annotations

from uuid import UUID

import pytest
from application.qa.profile import QAPlanningProfileV1
from application.qa.query_planning import QueryPlanner
from application.qa.query_rewriting import LlmQueryRewriter
from domain.grounded_qa import QAContractError, QuestionInput
from model_gateway import (
    CapabilityAlias,
    ChatResponse,
    ChatRole,
    ModelUsage,
)

SPACE_ID = UUID(int=1)


class FakeGateway:
    def __init__(self, text: str = "", *, error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.requests: list[tuple[object, CapabilityAlias]] = []

    async def chat(self, request: object, *, capability: CapabilityAlias) -> ChatResponse:
        self.requests.append((request, capability))
        if self.error:
            raise self.error
        return ChatResponse(
            text=self.text,
            finish_reason="stop",
            usage=ModelUsage(input_tokens=1, output_tokens=1),
            capability=capability,
            latency_ms=1.0,
        )


def _question(text: str = "What is the least upper bound property?") -> QuestionInput:
    return QuestionInput(
        question=text,
        space_id=SPACE_ID,
        caller_id="test-caller",
    )


async def test_rewrites_parse_json_queries_and_use_fast_chat() -> None:
    gateway = FakeGateway(text='{"queries": ["supremum definition", "real completeness Rudin"]}')
    rewriter = LlmQueryRewriter(gateway)
    queries = await rewriter.rewrite(_question(), max_queries=3)
    assert queries == ("supremum definition", "real completeness Rudin")
    request, capability = gateway.requests[0]
    assert capability is CapabilityAlias.FAST_CHAT
    assert request.temperature == 0.0  # type: ignore[attr-defined]
    messages = request.messages  # type: ignore[attr-defined]
    assert messages[0].role is ChatRole.SYSTEM
    assert messages[1].role is ChatRole.USER
    assert messages[1].content == "What is the least upper bound property?"


async def test_rewrites_cap_at_max_queries() -> None:
    gateway = FakeGateway(text='{"queries": ["a", "b", "c", "d"]}')
    rewriter = LlmQueryRewriter(gateway)
    assert await rewriter.rewrite(_question(), max_queries=2) == ("a", "b")


async def test_rewrites_filter_blank_and_non_string_items() -> None:
    gateway = FakeGateway(text='{"queries": ["a", "", 7, null, "b"]}')
    rewriter = LlmQueryRewriter(gateway)
    assert await rewriter.rewrite(_question(), max_queries=5) == ("a", "b")


async def test_rewrites_reject_non_json_output() -> None:
    gateway = FakeGateway(text="supremum definition\nreal completeness")
    rewriter = LlmQueryRewriter(gateway)
    with pytest.raises(QAContractError):
        await rewriter.rewrite(_question(), max_queries=2)


async def test_rewrites_reject_missing_queries_key() -> None:
    gateway = FakeGateway(text='{"other": ["a"]}')
    rewriter = LlmQueryRewriter(gateway)
    with pytest.raises(QAContractError):
        await rewriter.rewrite(_question(), max_queries=2)


async def test_rewrites_reject_non_object_json() -> None:
    gateway = FakeGateway(text='["a", "b"]')
    rewriter = LlmQueryRewriter(gateway)
    with pytest.raises(QAContractError):
        await rewriter.rewrite(_question(), max_queries=2)


async def test_rewrites_reject_max_queries_below_one() -> None:
    gateway = FakeGateway(text='{"queries": ["a"]}')
    rewriter = LlmQueryRewriter(gateway)
    with pytest.raises(QAContractError):
        await rewriter.rewrite(_question(), max_queries=0)


async def test_query_planner_applies_rewrites_when_enabled() -> None:
    gateway = FakeGateway(text='{"queries": ["supremum definition"]}')
    planner = QueryPlanner(rewriter=LlmQueryRewriter(gateway))
    result = await planner.plan(
        _question(),
        QAPlanningProfileV1(rewrite_enabled=True, max_subqueries=3),
    )
    assert result.plan.rewrite_applied is True
    assert result.plan.queries == (
        "What is the least upper bound property?",
        "supremum definition",
    )


async def test_query_planner_falls_back_when_rewriter_output_invalid() -> None:
    gateway = FakeGateway(text="not json")
    planner = QueryPlanner(rewriter=LlmQueryRewriter(gateway))
    result = await planner.plan(
        _question(),
        QAPlanningProfileV1(rewrite_enabled=True, max_subqueries=3),
    )
    assert result.plan.rewrite_applied is False
    assert result.plan.queries == ("What is the least upper bound property?",)
