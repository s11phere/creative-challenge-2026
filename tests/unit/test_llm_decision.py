from __future__ import annotations

from typing import cast
from uuid import UUID, uuid4

import pytest
from agent_runtime import (
    LLMDecisionAction,
    LLMDecisionError,
    LLMDecisionNode,
    NodeExecutionContext,
    PinnedSkill,
    parse_llm_decision,
)
from domain.agent_runtime import AgentRun, AgentRunContext, RunBudget
from model_gateway import (
    CapabilityAlias,
    ChatRequest,
    ChatResponse,
    FakeModelGateway,
    GatewayStatus,
    ModelGateway,
    ModelUsage,
)


def test_parse_decision_enforces_action_and_tool_allowlist() -> None:
    decision = parse_llm_decision(
        '{"action":"call_tool","tool_name":"search_knowledge","arguments":{"q":"x"}}',
        allowed_tools=frozenset({"search_knowledge"}),
    )
    assert decision.action is LLMDecisionAction.CALL_TOOL
    assert decision.arguments == {"q": "x"}

    with pytest.raises(LLMDecisionError):
        parse_llm_decision(
            '{"action":"call_tool","tool_name":"delete_everything","arguments":{}}',
            allowed_tools=frozenset({"search_knowledge"}),
        )
    with pytest.raises(LLMDecisionError):
        parse_llm_decision(
            '{"action":"complete","reason":"ok","extra":true}',
            allowed_tools=frozenset(),
        )
    with pytest.raises(LLMDecisionError):
        parse_llm_decision(
            '{"action":"complete","reason":"ok","arguments":{}}',
            allowed_tools=frozenset(),
        )


def test_parse_decision_requires_reason_for_terminal_actions() -> None:
    with pytest.raises(LLMDecisionError):
        parse_llm_decision('{"action":"refuse"}', allowed_tools=frozenset())
    with pytest.raises(LLMDecisionError):
        parse_llm_decision("not json", allowed_tools=frozenset())


class DecisionGateway:
    def __init__(self, response: str) -> None:
        self._delegate = FakeModelGateway()
        self.response = response

    @property
    def status(self) -> GatewayStatus:
        return self._delegate.status

    async def chat(
        self,
        _request: ChatRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.FAST_CHAT,
    ) -> ChatResponse:
        return ChatResponse(
            text=self.response,
            finish_reason="stop",
            usage=ModelUsage(input_tokens=3, output_tokens=2),
            capability=capability,
            latency_ms=1.0,
        )


@pytest.mark.asyncio
async def test_decision_node_returns_validated_state_without_executing_tool() -> None:
    run = AgentRun(
        context=AgentRunContext(
            run_id=uuid4(),
            space_id=UUID(int=1),
            skill_name="agent",
            skill_version="0.1.0",
            skill_content_sha256="a" * 64,
            trace_id="trace",
            caller_id="caller",
        ),
        budget=RunBudget(),
    )
    context = NodeExecutionContext(
        run=run,
        pin=cast(PinnedSkill, object()),
        input={"question": "find it"},
        state={},
        model_gateway=cast(
            ModelGateway,
            DecisionGateway(
                '{"action":"call_tool","tool_name":"search_knowledge","arguments":{"q":"find it"}}'
            ),
        ),
    )
    result = await LLMDecisionNode(
        allowed_tools=frozenset({"search_knowledge"}),
        system_prompt="Return only the decision schema.",
    )(context)
    assert result.output == result.state_updates["llm_decision"]
    assert isinstance(result.output, dict)
    assert result.output["action"] == "call_tool"
    assert result.usage.input_tokens == 3
