from __future__ import annotations

from typing import cast
from uuid import UUID, uuid4

import pytest
from agent_runtime import (
    BoundedLLMAgentNode,
    InMemoryToolRegistry,
    JSONValue,
    LLMDecisionAction,
    LLMDecisionError,
    LLMDecisionNode,
    LLMDecisionVerifyNode,
    NodeExecutionContext,
    NodeExecutionError,
    PinnedSkill,
    ToolDefinition,
    ToolExecutionContext,
    ToolRef,
    parse_llm_decision,
)
from domain.agent_runtime import AgentRun, AgentRunContext, RunBudget, ToolPermission
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
    def __init__(self, *responses: str) -> None:
        self._delegate = FakeModelGateway()
        self.responses = list(responses)
        self.requests: list[ChatRequest] = []

    @property
    def status(self) -> GatewayStatus:
        return self._delegate.status

    async def chat(
        self,
        _request: ChatRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.FAST_CHAT,
    ) -> ChatResponse:
        self.requests.append(_request)
        return ChatResponse(
            text=self.responses.pop(0),
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


def agent_run(*, permissions: frozenset[ToolPermission]) -> AgentRun:
    return AgentRun(
        context=AgentRunContext(
            run_id=UUID(int=10),
            space_id=UUID(int=11),
            skill_name="bounded_agent",
            skill_version="0.1.0",
            skill_content_sha256="b" * 64,
            trace_id="agent-trace",
            caller_id="agent-caller",
            granted_permissions=permissions,
        ),
        budget=RunBudget(
            max_steps=4,
            max_tool_calls=4,
            max_input_tokens=100,
            max_output_tokens=100,
            timeout_seconds=10,
        ),
    )


async def search_handler(
    arguments: dict[str, JSONValue], context: ToolExecutionContext
) -> dict[str, JSONValue]:
    assert context.idempotency_key
    return {"matches": [f"found:{arguments['query']}"]}


def search_tool(*, model_visible: bool = True) -> ToolDefinition:
    return ToolDefinition(
        name="search_knowledge",
        version="1.0.0",
        description="Search authorized knowledge in the run Space.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["query"],
            "properties": {"query": {"type": "string", "minLength": 1}},
        },
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["matches"],
            "properties": {"matches": {"type": "array", "items": {"type": "string"}}},
        },
        permissions=frozenset({ToolPermission.READ_KNOWLEDGE}),
        handler_name="search",
        model_visible=model_visible,
    )


def agent_context(run: AgentRun, gateway: DecisionGateway) -> NodeExecutionContext:
    return NodeExecutionContext(
        run=run,
        pin=cast(PinnedSkill, object()),
        input={"question": "find authorized evidence"},
        state={},
        model_gateway=cast(ModelGateway, gateway),
    )


@pytest.mark.asyncio
async def test_bounded_agent_calls_registered_tool_then_completes() -> None:
    registry = InMemoryToolRegistry(handlers={"search": search_handler})
    tool = registry.register(search_tool())
    gateway = DecisionGateway(
        '{"action":"call_tool","tool_name":"search_knowledge","arguments":{"query":"safe"}}',
        '{"action":"complete","reason":"grounded result"}',
    )
    result = await BoundedLLMAgentNode(
        tool_registry=registry,
        allowed_tools=(tool.ref,),
        system_prompt="Use only authorized evidence.",
    )(agent_context(agent_run(permissions=tool.permissions), gateway))

    assert result.output == {"action": "complete", "reason": "grounded result"}
    assert result.usage.tool_calls == 1
    assert result.usage.input_tokens == 6
    assert len(gateway.requests) == 2
    assert "found:safe" in gateway.requests[1].messages[1].content
    calls = result.state_updates["agent_tool_calls"]
    assert isinstance(calls, list)
    first_call = calls[0]
    assert isinstance(first_call, dict)
    assert first_call["tool_name"] == "search_knowledge"
    input_summary = first_call["input_summary"]
    assert isinstance(input_summary, str)
    assert "safe" not in input_summary


@pytest.mark.asyncio
async def test_bounded_agent_rejects_non_visible_or_write_tool() -> None:
    registry = InMemoryToolRegistry(handlers={"search": search_handler})
    hidden = registry.register(search_tool(model_visible=False))
    with pytest.raises(NodeExecutionError) as hidden_error:
        await BoundedLLMAgentNode(
            tool_registry=registry,
            allowed_tools=(hidden.ref,),
            system_prompt="Use safe tools.",
        )(agent_context(agent_run(permissions=hidden.permissions), DecisionGateway()))
    assert hidden_error.value.code == "TOOL_MODEL_OUTPUT_DENIED"

    write_registry = InMemoryToolRegistry(handlers={"search": search_handler})
    write = write_registry.register(
        ToolDefinition(
            **{
                **search_tool().__dict__,
                "name": "save_knowledge",
                "permissions": frozenset({ToolPermission.WRITE_KNOWLEDGE}),
            }
        )
    )
    with pytest.raises(NodeExecutionError) as write_error:
        await BoundedLLMAgentNode(
            tool_registry=write_registry,
            allowed_tools=(write.ref,),
            system_prompt="Use safe tools.",
        )(agent_context(agent_run(permissions=write.permissions), DecisionGateway()))
    assert write_error.value.code == "TOOL_APPROVAL_REQUIRED"


@pytest.mark.asyncio
async def test_bounded_agent_stops_at_iteration_limit() -> None:
    registry = InMemoryToolRegistry(handlers={"search": search_handler})
    tool = registry.register(search_tool())
    gateway = DecisionGateway(
        '{"action":"call_tool","tool_name":"search_knowledge","arguments":{"query":"safe"}}'
    )
    with pytest.raises(NodeExecutionError) as exhausted:
        await BoundedLLMAgentNode(
            tool_registry=registry,
            allowed_tools=(ToolRef(tool.name, tool.version),),
            system_prompt="Use safe tools.",
            max_iterations=1,
        )(agent_context(agent_run(permissions=tool.permissions), gateway))
    assert exhausted.value.code == "RUN_LLM_MAX_ITERATIONS"


@pytest.mark.asyncio
async def test_verify_node_projects_only_terminal_decisions() -> None:
    gateway = DecisionGateway()
    context = agent_context(agent_run(permissions=frozenset()), gateway)
    complete = await LLMDecisionVerifyNode()(
        replace_context(context, state={"agent_decision": {"action": "complete", "reason": "done"}})
    )
    assert complete.outcome.value == "complete"
    with pytest.raises(NodeExecutionError) as missing:
        await LLMDecisionVerifyNode()(replace_context(context, state={}))
    assert missing.value.code == "RUN_LLM_DECISION_MISSING"


def replace_context(
    context: NodeExecutionContext, *, state: dict[str, JSONValue]
) -> NodeExecutionContext:
    return NodeExecutionContext(
        run=context.run,
        pin=context.pin,
        input=context.input,
        state=state,
        model_gateway=context.model_gateway,
    )
