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
from agent_runtime.llm_decision import _GENERATION_SYSTEM_PROMPT
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


def test_parse_decision_ignores_empty_server_authored_response_on_clarification() -> None:
    decision = parse_llm_decision(
        '{"action":"clarify","reason":"Need one detail.","final_response":""}',
        allowed_tools=frozenset(),
    )
    assert decision.action is LLMDecisionAction.CLARIFY
    assert decision.final_response is None

    with pytest.raises(LLMDecisionError):
        parse_llm_decision(
            '{"action":"complete","reason":"done","final_response":""}',
            allowed_tools=frozenset(),
        )


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


def search_tool(*, model_visible: bool = True, max_retries: int = 0) -> ToolDefinition:
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
        max_retries=max_retries,
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
async def test_bounded_agent_completes_without_another_model_call_after_terminal_tool() -> None:
    registry = InMemoryToolRegistry(handlers={"search": search_handler})
    tool = registry.register(search_tool())
    gateway = DecisionGateway(
        '{"action":"call_tool","tool_name":"search_knowledge","arguments":{"query":"safe"}}'
    )

    result = await BoundedLLMAgentNode(
        tool_registry=registry,
        allowed_tools=(tool.ref,),
        terminal_tools=frozenset({tool.name}),
        system_prompt="Use only authorized evidence.",
    )(agent_context(agent_run(permissions=tool.permissions), gateway))

    assert result.output == {
        "action": "complete",
        "reason": "Terminal Tool search_knowledge completed.",
    }
    assert result.usage.tool_calls == 1
    assert len(gateway.requests) == 1


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
async def test_bounded_agent_retries_transient_tool_failure_within_declared_budget() -> None:
    attempts = {"count": 0}

    async def flaky_handler(
        arguments: dict[str, JSONValue], _context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("transient model failure")
        return {"matches": [f"found:{arguments['query']}"]}

    registry = InMemoryToolRegistry(handlers={"search": flaky_handler})
    tool = registry.register(search_tool(max_retries=1))
    gateway = DecisionGateway(
        '{"action":"call_tool","tool_name":"search_knowledge","arguments":{"query":"safe"}}',
        '{"action":"complete","reason":"grounded result"}',
    )
    result = await BoundedLLMAgentNode(
        tool_registry=registry,
        allowed_tools=(tool.ref,),
        system_prompt="Use only authorized evidence.",
    )(agent_context(agent_run(permissions=tool.permissions), gateway))

    assert attempts["count"] == 2
    assert result.output == {"action": "complete", "reason": "grounded result"}
    assert result.usage.tool_calls == 1
    calls = result.state_updates["agent_tool_calls"]
    assert isinstance(calls, list)
    assert len(calls) == 1
    first_call = calls[0]
    assert isinstance(first_call, dict)
    assert first_call["tool_name"] == "search_knowledge"
    assert "found:safe" in gateway.requests[1].messages[1].content


@pytest.mark.asyncio
async def test_bounded_agent_does_not_retry_without_declared_budget() -> None:
    attempts = {"count": 0}

    async def always_failing_handler(
        _arguments: dict[str, JSONValue], _context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        attempts["count"] += 1
        raise RuntimeError("persistent failure")

    registry = InMemoryToolRegistry(handlers={"search": always_failing_handler})
    tool = registry.register(search_tool())  # max_retries defaults to 0
    gateway = DecisionGateway(
        '{"action":"call_tool","tool_name":"search_knowledge","arguments":{"query":"safe"}}'
    )
    with pytest.raises(NodeExecutionError) as failure:
        await BoundedLLMAgentNode(
            tool_registry=registry,
            allowed_tools=(tool.ref,),
            system_prompt="Use only authorized evidence.",
        )(agent_context(agent_run(permissions=tool.permissions), gateway))
    assert failure.value.code == "TOOL_EXECUTION_FAILED"
    assert failure.value.retryable is False
    assert attempts["count"] == 1


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


class ScriptedChatGateway:
    """Model gateway driven by an explicit (text, finish_reason) script.

    Usage increments per call so escalation accounting is deterministic:
    call i returns input_tokens=i and output_tokens=i*10.
    """

    def __init__(self, *responses: tuple[str, str]) -> None:
        self._delegate = FakeModelGateway()
        self.responses = list(responses)
        self.requests: list[ChatRequest] = []

    @property
    def status(self) -> GatewayStatus:
        return self._delegate.status

    async def chat(
        self,
        request: ChatRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.FAST_CHAT,
    ) -> ChatResponse:
        self.requests.append(request)
        text, finish_reason = self.responses.pop(0)
        index = len(self.requests)
        return ChatResponse(
            text=text,
            finish_reason=finish_reason,
            usage=ModelUsage(input_tokens=index, output_tokens=index * 10),
            capability=capability,
            latency_ms=1.0,
        )


def decision_context(gateway: ScriptedChatGateway) -> NodeExecutionContext:
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
    return NodeExecutionContext(
        run=run,
        pin=cast(PinnedSkill, object()),
        input={"question": "explain the full generative model lineage"},
        state={},
        model_gateway=cast(ModelGateway, gateway),
    )


def _truncated_complete_text() -> str:
    return (
        '{"action":"complete","reason":"full lineage answer","final_response":"' + ("x" * 600) + "…"
    )


@pytest.mark.asyncio
async def test_decision_node_inlines_short_answer_without_escalation() -> None:
    gateway = ScriptedChatGateway(
        ('{"action":"complete","reason":"done","final_response":"short answer"}', "stop")
    )
    context = decision_context(gateway)
    decision, usage = await LLMDecisionNode(
        allowed_tools=frozenset(),
        system_prompt="Return only the decision schema.",
        escalate_long_answer=True,
    ).decide(context)
    assert decision.action is LLMDecisionAction.COMPLETE
    assert decision.final_response == "short answer"
    assert len(gateway.requests) == 1
    assert usage.input_tokens == 1
    assert usage.output_tokens == 10


@pytest.mark.asyncio
async def test_decision_node_escalates_truncated_long_answer_to_generation() -> None:
    full_answer = "# 生成模型谱系\nVAE → GAN → DDPM → SDE"
    gateway = ScriptedChatGateway((_truncated_complete_text(), "length"), (full_answer, "stop"))
    context = decision_context(gateway)
    decision, usage = await LLMDecisionNode(
        allowed_tools=frozenset(),
        system_prompt="Return only the decision schema.",
        escalate_long_answer=True,
    ).decide(context)
    assert decision.action is LLMDecisionAction.COMPLETE
    assert decision.reason == "escalated long answer"
    assert decision.final_response == full_answer
    assert len(gateway.requests) == 2
    generation_request = gateway.requests[1]
    assert generation_request.messages[0].content == _GENERATION_SYSTEM_PROMPT
    assert generation_request.messages[1].content == "explain the full generative model lineage"
    assert generation_request.temperature == 0.2
    assert generation_request.max_tokens == 6_144
    assert usage.input_tokens == 3
    assert usage.output_tokens == 30


@pytest.mark.asyncio
async def test_decision_node_does_not_escalate_non_complete_truncation() -> None:
    gateway = ScriptedChatGateway(
        (
            '{"action":"call_tool","tool_name":"search_knowledge","arguments":{"query":"'
            + ("q" * 500)
            + "…",
            "length",
        )
    )
    context = decision_context(gateway)
    with pytest.raises(LLMDecisionError):
        await LLMDecisionNode(
            allowed_tools=frozenset({"search_knowledge"}),
            system_prompt="Return only the decision schema.",
            escalate_long_answer=True,
        ).decide(context)
    assert len(gateway.requests) == 1


@pytest.mark.asyncio
async def test_decision_node_does_not_escalate_when_disabled() -> None:
    gateway = ScriptedChatGateway((_truncated_complete_text(), "length"))
    context = decision_context(gateway)
    with pytest.raises(LLMDecisionError):
        await LLMDecisionNode(
            allowed_tools=frozenset(),
            system_prompt="Return only the decision schema.",
        ).decide(context)
    assert len(gateway.requests) == 1


@pytest.mark.asyncio
async def test_decision_node_does_not_publish_when_generation_truncates() -> None:
    gateway = ScriptedChatGateway(
        (_truncated_complete_text(), "length"), ("# partial answer", "length")
    )
    context = decision_context(gateway)
    with pytest.raises(NodeExecutionError) as failure:
        await LLMDecisionNode(
            allowed_tools=frozenset(),
            system_prompt="Return only the decision schema.",
            escalate_long_answer=True,
        ).decide(context)
    assert failure.value.code == "RUN_LLM_GENERATION_TRUNCATED"
    assert len(gateway.requests) == 2


@pytest.mark.asyncio
async def test_generation_uses_loop_goal_when_input_has_no_question() -> None:
    gateway = ScriptedChatGateway((_truncated_complete_text(), "length"), ("full answer", "stop"))
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
        input={},
        state={"goal": "the loop goal text"},
        model_gateway=cast(ModelGateway, gateway),
    )
    decision, _usage = await LLMDecisionNode(
        allowed_tools=frozenset(),
        system_prompt="Return only the decision schema.",
        escalate_long_answer=True,
    ).decide(context)
    assert decision.final_response == "full answer"
    assert gateway.requests[1].messages[1].content == "the loop goal text"


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
