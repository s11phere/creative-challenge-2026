from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from agent_runtime import (
    InMemoryToolRegistry,
    JSONValue,
    ToolDefinition,
    ToolExecutionContext,
    ToolInvocation,
)
from domain.agent_runtime import AgentRun, AgentRunContext, RunBudget, ToolPermission
from infrastructure.qa_debug_trace import QADebugTrace, TracingModelGateway, TracingToolRegistry
from model_gateway import (
    CapabilityAlias,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatRole,
    ChatToolCall,
    ChatToolDefinition,
    ChatToolResult,
    FakeModelGateway,
    FakeScenario,
    ModelGatewayError,
    ModelUsage,
)


def _events(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8")
    return [json.loads(block) for block in text.split("\n\n") if block.strip()]


class NativeToolGateway(FakeModelGateway):
    async def chat(
        self,
        request: ChatRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.FAST_CHAT,
    ) -> ChatResponse:
        del request
        return ChatResponse(
            text="",
            tool_calls=(ChatToolCall("call_1", "invoke_skill", {"name": "knowledge_agent"}),),
            finish_reason="tool_calls",
            usage=ModelUsage(input_tokens=1, output_tokens=1),
            capability=capability,
            latency_ms=0.0,
        )


@pytest.mark.asyncio
async def test_debug_trace_is_disabled_by_default(tmp_path: Path) -> None:
    trace = QADebugTrace(
        run_id=uuid4(),
        trace_id="a" * 32,
        enabled=False,
        environment="development",
        path=str(tmp_path),
    )

    await trace.record("llm_request", messages=[{"content": "private question"}])

    assert list(tmp_path.iterdir()) == []
    assert trace.file_path == tmp_path / f"{trace.run_id}.jsonl"


@pytest.mark.asyncio
async def test_debug_trace_cannot_be_enabled_in_production(tmp_path: Path) -> None:
    trace = QADebugTrace(
        run_id=uuid4(),
        trace_id="b" * 32,
        enabled=True,
        environment="production",
        path=str(tmp_path),
    )

    await trace.record("llm_request", messages=[{"content": "private question"}])

    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_tracing_gateway_records_full_chat_and_model_error(tmp_path: Path) -> None:
    run_id = uuid4()
    trace = QADebugTrace(
        run_id=run_id,
        trace_id="c" * 32,
        enabled=True,
        environment="development",
        path=str(tmp_path),
    )
    request = ChatRequest(
        messages=(
            ChatMessage(ChatRole.SYSTEM, "private system prompt"),
            ChatMessage(ChatRole.USER, "private question"),
        )
    )

    await TracingModelGateway(FakeModelGateway(), trace, phase="agent_decision").chat(request)
    with pytest.raises(ModelGatewayError):
        await TracingModelGateway(
            FakeModelGateway(scenario=FakeScenario.UNAVAILABLE),
            trace,
            phase="grounded_generation",
        ).chat(request)

    events = _events(tmp_path / f"{run_id}.jsonl")
    assert [event["event"] for event in events] == [
        "llm_request",
        "llm_response",
        "llm_request",
        "llm_error",
    ]
    assert events[0]["messages"][1]["content"] == "private question"
    assert events[3]["error"]["error_code"] == "MODEL_UNAVAILABLE"
    assert "api_key" not in json.dumps(events)


@pytest.mark.asyncio
async def test_tracing_gateway_records_native_tool_surface_and_output(tmp_path: Path) -> None:
    run_id = uuid4()
    trace = QADebugTrace(
        run_id=run_id,
        trace_id="e" * 32,
        enabled=True,
        environment="development",
        path=str(tmp_path),
    )
    call = ChatToolCall(
        call_id="call_1",
        tool_name="invoke_skill",
        arguments={"name": "knowledge_agent"},
    )
    request = ChatRequest(
        messages=(ChatMessage(ChatRole.USER, "private question"),),
        tools=(ChatToolDefinition("invoke_skill", "Select a Skill.", {"type": "object"}),),
        tool_call_history=(call,),
        tool_results=(
            ChatToolResult(
                call_id="call_1",
                tool_name="invoke_skill",
                observation={"status": "succeeded", "summary": "Selected Skill."},
            ),
        ),
    )
    response = await TracingModelGateway(
        NativeToolGateway(),
        trace,
        phase="assistant_agent_decision",
    ).chat(request)

    assert response.tool_calls[0].tool_name == "invoke_skill"
    events = _events(tmp_path / f"{run_id}.jsonl")
    assert events[0]["tools"][0]["name"] == "invoke_skill"
    assert events[0]["tool_call_history"][0]["arguments"] == {"name": "knowledge_agent"}
    assert events[0]["tool_results"][0]["observation"]["summary"] == "Selected Skill."
    assert events[1]["tool_calls"][0]["arguments"] == {"name": "knowledge_agent"}


@pytest.mark.asyncio
async def test_tracing_tool_registry_records_arguments_and_result(tmp_path: Path) -> None:
    async def handler(arguments: dict[str, JSONValue], _context: ToolExecutionContext) -> JSONValue:
        return {"answer": arguments["query"]}

    registry = InMemoryToolRegistry(handlers={"search": handler})
    definition = registry.register(
        ToolDefinition(
            name="search",
            version="1.0.0",
            description="Search synthetic content.",
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}},
            },
            output_schema={
                "type": "object",
                "required": ["answer"],
                "properties": {"answer": {"type": "string"}},
            },
            permissions=frozenset({ToolPermission.READ_KNOWLEDGE}),
            handler_name="search",
            model_visible=True,
        )
    )
    run_id = uuid4()
    run = AgentRun(
        context=AgentRunContext(
            run_id=run_id,
            space_id=UUID(int=1),
            skill_name="knowledge_agent",
            skill_version="0.2.0",
            skill_content_sha256="a" * 64,
            trace_id="d" * 32,
            caller_id="developer",
            granted_permissions=frozenset({ToolPermission.READ_KNOWLEDGE}),
        ),
        budget=RunBudget(),
    )
    trace = QADebugTrace(
        run_id=run_id,
        trace_id="d" * 32,
        enabled=True,
        environment="development",
        path=str(tmp_path),
    )
    invocation = ToolInvocation(
        ref=definition.ref,
        arguments={"query": "private tool query"},
        allowed_tools=frozenset({definition.ref}),
        granted_permissions=run.context.granted_permissions,
        resource_space_id=run.context.space_id,
        idempotency_key="debug-tool-call",
    )

    result = await TracingToolRegistry(registry, trace).invoke(run, invocation)

    assert result.output == {"answer": "private tool query"}
    events = _events(tmp_path / f"{run_id}.jsonl")
    assert [event["event"] for event in events] == ["tool_call", "tool_result"]
    assert events[0]["arguments"] == {"query": "private tool query"}
    assert events[1]["output"] == {"answer": "private tool query"}
