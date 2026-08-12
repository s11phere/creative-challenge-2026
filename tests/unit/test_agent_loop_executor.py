from __future__ import annotations

from typing import Any, cast
from uuid import UUID

import pytest
from agent_runtime import (
    AgentLoopExecutor,
    InMemoryRuntimeStateStore,
    InMemoryToolRegistry,
    LLMDecision,
    LLMDecisionAction,
)
from agent_runtime.skills import PinnedSkill
from agent_runtime.tools import JSONValue, ToolDefinition, ToolExecutionContext, ToolRef
from domain.agent_loop import AgentLoopFinalizationState, AgentLoopPhase
from domain.agent_runtime import AgentRun, AgentRunContext, RunBudget, RunStatus, ToolPermission
from domain.agent_sse import AgentRunEventLog, AgentRunEventType
from model_gateway import (
    CapabilityAlias,
    ChatRequest,
    ChatResponse,
    FakeModelGateway,
    GatewayStatus,
    ModelGateway,
    ModelUsage,
)


class DecisionGateway:
    def __init__(self, *responses: str) -> None:
        self._delegate = FakeModelGateway()
        self._responses = list(responses)
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
        return ChatResponse(
            text=self._responses.pop(0),
            finish_reason="stop",
            usage=ModelUsage(input_tokens=3, output_tokens=2),
            capability=capability,
            latency_ms=1.0,
        )


class EscalatingGateway:
    """Model fixture that truncates a long complete decision, then writes the answer."""

    def __init__(self, full_answer: str) -> None:
        self._delegate = FakeModelGateway()
        self.requests: list[ChatRequest] = []
        self._full_answer = full_answer

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
        if len(self.requests) == 1:
            return ChatResponse(
                text=(
                    '{"action":"complete","reason":"full lineage answer","final_response":"'
                    + ("x" * 600)
                    + "…"
                ),
                finish_reason="length",
                usage=ModelUsage(input_tokens=10, output_tokens=20),
                capability=capability,
                latency_ms=1.0,
            )
        return ChatResponse(
            text=self._full_answer,
            finish_reason="stop",
            usage=ModelUsage(input_tokens=5, output_tokens=30),
            capability=capability,
            latency_ms=1.0,
        )


class RecordingFinalizer:
    def __init__(self) -> None:
        self.calls = 0

    async def finalize(self, **kwargs: object) -> dict[str, JSONValue]:
        self.calls += 1
        decision = kwargs["decision"]
        return {"message": f"final:{getattr(decision, 'reason', '')}"}


class RecordingDebugTrace:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def record(self, event_type: str, **payload: object) -> None:
        self.events.append((event_type, cast(dict[str, Any], payload)))


class ApprovedPort:
    async def request(self, _context: AgentRunContext, _tool: object) -> str:
        return "approval-1"

    async def is_approved(self, approval_id: str, _context: AgentRunContext) -> bool:
        return approval_id == "approval-1"


def loop_run(*, permissions: frozenset[ToolPermission]) -> AgentRun:
    return AgentRun(
        context=AgentRunContext(
            run_id=UUID("00000000-0000-4000-8000-000000000011"),
            space_id=UUID("00000000-0000-4000-8000-000000000012"),
            skill_name="loop_fixture",
            skill_version="1.0.0",
            skill_content_sha256="a" * 64,
            trace_id="loop-trace",
            caller_id="loop-caller",
            granted_permissions=permissions,
        ),
        budget=RunBudget(
            max_steps=8,
            max_tool_calls=4,
            max_input_tokens=100,
            max_output_tokens=100,
            timeout_seconds=30,
        ),
    )


async def search_handler(
    arguments: dict[str, JSONValue], _context: ToolExecutionContext
) -> dict[str, JSONValue]:
    return {"matches": [f"found:{arguments['query']}"]}


def tool(
    *,
    write: bool = False,
    permission: ToolPermission | None = None,
    name: str | None = None,
) -> ToolDefinition:
    tool_name = name or ("search_knowledge" if not write else "write_note")
    return ToolDefinition(
        name=tool_name,
        version="1.0.0",
        description="Operate on synthetic loop data.",
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
        permissions=frozenset(
            {permission}
            if permission is not None
            else ({ToolPermission.WRITE_KNOWLEDGE} if write else {ToolPermission.READ_KNOWLEDGE})
        ),
        handler_name="tool",
        model_visible=True,
    )


@pytest.mark.asyncio
async def test_loop_observes_multiple_tools_then_finalizes_once() -> None:
    registry = InMemoryToolRegistry(handlers={"tool": search_handler})
    definition = registry.register(tool())
    finalizer = RecordingFinalizer()
    state_store = InMemoryRuntimeStateStore()
    events = AgentRunEventLog()
    result = await AgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(ToolRef(definition.name, definition.version),),
        system_prompt="Use only the registered synthetic Tool.",
        model_gateway=cast(
            ModelGateway,
            DecisionGateway(
                '{"action":"call_tool","tool_name":"search_knowledge","arguments":{"query":"one"}}',
                '{"action":"call_tool","tool_name":"search_knowledge","arguments":{"query":"two"}}',
                '{"action":"complete","reason":"verified synthetic result"}',
            ),
        ),
        state_store=state_store,
        event_store=events,
        finalizer=finalizer,
        tool_skill_refs={definition.ref: ToolRef("knowledge_agent", "1.0.0")},
    ).execute(
        loop_run(permissions=definition.permissions),
        cast(PinnedSkill, object()),
        {"question": "synthetic"},
        goal="Answer the synthetic request with evidence.",
    )

    assert result.run.status is RunStatus.COMPLETED
    assert result.state.phase is AgentLoopPhase.COMPLETED
    assert result.state.finalization is AgentLoopFinalizationState.PUBLISHED
    assert len(result.state.observations) == 2
    assert result.run.checkpoint_sequence == 5
    assert result.output == {"message": "final:verified synthetic result"}
    assert finalizer.calls == 1
    stored = await state_store.get_run(result.run.context.run_id)
    assert stored == result.run
    history = await events.page(result.run.context.run_id, limit=200)
    activations = [
        event for event in history.events if event.event_type is AgentRunEventType.SKILL_ACTIVATED
    ]
    assert [(event.payload["skill_name"], event.payload["iteration"]) for event in activations] == [
        ("loop_fixture", 0),
        ("knowledge_agent", 1),
    ]


@pytest.mark.asyncio
async def test_loop_escalates_a_truncated_long_answer_to_generation() -> None:
    registry = InMemoryToolRegistry(handlers={"tool": search_handler})
    definition = registry.register(tool())
    finalizer = RecordingFinalizer()
    full_answer = "# 生成模型谱系\n完整长文回答。"
    gateway = EscalatingGateway(full_answer)
    result = await AgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(definition.ref,),
        system_prompt="Use only the registered synthetic Tool.",
        model_gateway=cast(ModelGateway, gateway),
        finalizer=finalizer,
        escalate_long_answer=True,
    ).execute(
        loop_run(permissions=definition.permissions),
        cast(PinnedSkill, object()),
        {"question": "synthetic"},
        goal="Answer the synthetic request with evidence.",
    )

    assert result.run.status is RunStatus.COMPLETED
    assert result.state.finalization_response == full_answer
    assert result.output == {"message": "final:escalated long answer"}
    assert finalizer.calls == 1
    assert len(gateway.requests) == 2
    assert gateway.requests[1].max_tokens == 6_144
    assert gateway.requests[1].messages[1].content == "synthetic"


@pytest.mark.asyncio
async def test_loop_records_complete_model_io_for_each_agent_round_only_in_debug_trace() -> None:
    registry = InMemoryToolRegistry(handlers={"tool": search_handler})
    definition = registry.register(tool())
    trace = RecordingDebugTrace()
    result = await AgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(definition.ref,),
        system_prompt="Use only the registered synthetic Tool.",
        model_gateway=cast(
            ModelGateway,
            DecisionGateway(
                (
                    '{"action":"call_tool","tool_name":"search_knowledge",'
                    '"arguments":{"query":"private query"}}'
                ),
                '{"action":"complete","reason":"done","final_response":"private answer"}',
            ),
        ),
        debug_trace=trace,
    ).execute(
        loop_run(permissions=definition.permissions),
        cast(PinnedSkill, object()),
        {"question": "private question"},
        goal="Answer the private request.",
    )

    assert result.run.status is RunStatus.COMPLETED
    assert [event[0] for event in trace.events] == ["agent_round", "agent_round"]
    first = trace.events[0][1]
    second = trace.events[1][1]
    assert first["round_number"] == 1
    assert second["round_number"] == 2
    assert first["input"]["messages"][1]["content"]
    assert "private query" in first["output"]["text"]
    assert "private answer" in second["output"]["text"]


@pytest.mark.asyncio
async def test_loop_recovers_one_allowlist_error_with_a_caller_scoped_decision() -> None:
    registry = InMemoryToolRegistry(handlers={"tool": search_handler})
    definition = registry.register(tool())
    recoveries: list[str] = []

    def recover(_run: AgentRun, _state: object, error: object) -> LLMDecision | None:
        recoveries.append(str(getattr(error, "message", "")))
        return LLMDecision(
            action=LLMDecisionAction.CALL_TOOL,
            tool_name="search_knowledge",
            arguments={"query": "recovered"},
        )

    result = await AgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(definition.ref,),
        system_prompt="Use only the registered synthetic Tool.",
        model_gateway=cast(
            ModelGateway,
            DecisionGateway(
                '{"action":"call_tool","tool_name":"fs_read","arguments":{"path":"note.md"}}',
                '{"action":"complete","reason":"recovered result","final_response":"done"}',
            ),
        ),
        invalid_decision_recovery=recover,
    ).execute(
        loop_run(permissions=definition.permissions),
        cast(PinnedSkill, object()),
        {"question": "synthetic"},
        goal="Answer the synthetic request.",
    )

    assert result.error is None
    assert result.run.status is RunStatus.COMPLETED
    assert recoveries == ["Model selected a Tool outside the server allowlist."]
    assert [observation.tool_name for observation in result.state.observations] == [
        "search_knowledge"
    ]


@pytest.mark.asyncio
async def test_loop_persists_redacted_v3_history_with_one_terminal_event() -> None:
    registry = InMemoryToolRegistry(handlers={"tool": search_handler})
    definition = registry.register(tool())
    events = AgentRunEventLog()
    result = await AgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(definition.ref,),
        system_prompt="Use only the registered synthetic Tool.",
        model_gateway=cast(
            ModelGateway,
            DecisionGateway(
                (
                    '{"action":"call_tool","tool_name":"search_knowledge",'
                    '"arguments":{"query":"secret synthetic query"}}'
                ),
                '{"action":"complete","reason":"verified synthetic result"}',
            ),
        ),
        state_store=InMemoryRuntimeStateStore(),
        event_store=events,
    ).execute(
        loop_run(permissions=definition.permissions),
        cast(PinnedSkill, object()),
        {"question": "synthetic"},
        goal="Answer the synthetic request with evidence.",
    )

    history = await events.page(result.run.context.run_id, limit=200)

    assert [event.event_type for event in history.events] == [
        AgentRunEventType.ACCEPTED,
        AgentRunEventType.SKILL_ACTIVATED,
        AgentRunEventType.ITERATION_STARTED,
        AgentRunEventType.TOOL_REQUESTED,
        AgentRunEventType.CHECKPOINT_SAVED,
        AgentRunEventType.TOOL_STARTED,
        AgentRunEventType.TOOL_OUTPUT,
        AgentRunEventType.CHECKPOINT_SAVED,
        AgentRunEventType.ITERATION_STARTED,
        AgentRunEventType.FINALIZING,
        AgentRunEventType.CHECKPOINT_SAVED,
        AgentRunEventType.COMPLETED,
    ]
    assert history.events[-1].terminal
    serialized = str([event.as_dict() for event in history.events])
    assert "secret synthetic query" not in serialized
    assert '"question"' not in serialized
    assert history.events[-1].payload["publication_id"].startswith("assistant-publication:")


@pytest.mark.asyncio
async def test_knowledge_search_events_expose_only_a_bounded_query_preview() -> None:
    registry = InMemoryToolRegistry(handlers={"tool": search_handler})
    definition = registry.register(tool(name="knowledge_search"))
    events = AgentRunEventLog()
    result = await AgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(definition.ref,),
        system_prompt="Use the registered retrieval Tool.",
        model_gateway=cast(
            ModelGateway,
            DecisionGateway(
                '{"action":"call_tool","tool_name":"knowledge_search",'
                '"arguments":{"query":"  What\\n is the current retrieval question?  "}}',
                '{"action":"complete","reason":"done"}',
            ),
        ),
        event_store=events,
    ).execute(
        loop_run(permissions=definition.permissions),
        cast(PinnedSkill, object()),
        {"question": "synthetic"},
        goal="Answer the retrieval request.",
    )

    history = await events.page(result.run.context.run_id, limit=200)
    tool_events = [
        event
        for event in history.events
        if event.event_type
        in {
            AgentRunEventType.TOOL_REQUESTED,
            AgentRunEventType.TOOL_STARTED,
            AgentRunEventType.TOOL_OUTPUT,
        }
    ]
    assert tool_events
    assert all(
        event.payload["query_preview"] == "What is the current retrieval question?"
        for event in tool_events
    )
    assert all("arguments" not in event.payload for event in tool_events)


@pytest.mark.asyncio
async def test_loop_returns_one_repeated_tool_request_as_model_feedback() -> None:
    calls = 0

    async def count_handler(
        arguments: dict[str, JSONValue], context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        nonlocal calls
        calls += 1
        return await search_handler(arguments, context)

    registry = InMemoryToolRegistry(handlers={"tool": count_handler})
    definition = registry.register(tool())
    result = await AgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(definition.ref,),
        system_prompt="Use only the registered synthetic Tool.",
        model_gateway=cast(
            ModelGateway,
            DecisionGateway(
                '{"action":"call_tool","tool_name":"search_knowledge","arguments":{"query":"same"}}',
                '{"action":"call_tool","tool_name":"search_knowledge","arguments":{"query":"same"}}',
                '{"action":"complete","reason":"the existing observation is sufficient"}',
            ),
        ),
    ).execute(
        loop_run(permissions=definition.permissions),
        cast(PinnedSkill, object()),
        {"question": "synthetic"},
        goal="Answer the synthetic request.",
    )

    assert result.run.status is RunStatus.COMPLETED
    assert result.error is None
    assert calls == 1
    assert result.state.observations[-1].error_code == "RUN_LLM_DUPLICATE_TOOL_REQUEST"
    assert result.state.observations[-1].model_output == {
        "trust": "trusted_runtime",
        "status": "already_observed",
        "tool_name": "search_knowledge",
        "recommended_next": "choose_a_different_action",
    }


@pytest.mark.asyncio
async def test_loop_rejects_a_second_repeated_tool_request_as_no_progress() -> None:
    calls = 0

    async def count_handler(
        arguments: dict[str, JSONValue], context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        nonlocal calls
        calls += 1
        return await search_handler(arguments, context)

    registry = InMemoryToolRegistry(handlers={"tool": count_handler})
    definition = registry.register(tool())
    result = await AgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(definition.ref,),
        system_prompt="Use only the registered synthetic Tool.",
        model_gateway=cast(
            ModelGateway,
            DecisionGateway(
                '{"action":"call_tool","tool_name":"search_knowledge","arguments":{"query":"same"}}',
                '{"action":"call_tool","tool_name":"search_knowledge","arguments":{"query":"same"}}',
                '{"action":"call_tool","tool_name":"search_knowledge","arguments":{"query":"same"}}',
            ),
        ),
    ).execute(
        loop_run(permissions=definition.permissions),
        cast(PinnedSkill, object()),
        {"question": "synthetic"},
        goal="Answer the synthetic request.",
    )

    assert result.run.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.code == "RUN_LLM_NO_PROGRESS"
    assert calls == 1


@pytest.mark.asyncio
async def test_waiting_approval_checkpoints_and_resumes_the_pending_tool() -> None:
    registry = InMemoryToolRegistry(handlers={"tool": search_handler}, approval_port=ApprovedPort())
    definition = registry.register(tool(write=True))
    state_store = InMemoryRuntimeStateStore()
    events = AgentRunEventLog()
    gateway = cast(
        ModelGateway,
        DecisionGateway(
            '{"action":"call_tool","tool_name":"write_note","arguments":{"query":"approved"}}',
            '{"action":"complete","reason":"write completed"}',
        ),
    )
    executor = AgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(definition.ref,),
        system_prompt="Use only the registered synthetic Tool.",
        model_gateway=gateway,
        state_store=state_store,
        event_store=events,
    )
    started = loop_run(permissions=definition.permissions)
    waiting = await executor.execute(
        started,
        cast(PinnedSkill, object()),
        {"question": "synthetic"},
        goal="Write the approved synthetic note.",
    )
    assert waiting.waiting_approval
    assert waiting.run.status is RunStatus.WAITING_APPROVAL
    checkpoint = await state_store.get_latest(waiting.run.context.run_id)
    assert checkpoint is not None
    assert checkpoint.caller_id == waiting.run.context.caller_id
    assert checkpoint.space_id == waiting.run.context.space_id
    assert checkpoint.idempotency_key is not None

    resumed = await AgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(definition.ref,),
        system_prompt="Use only the registered synthetic Tool.",
        model_gateway=gateway,
        state_store=state_store,
        event_store=events,
    ).resume(
        waiting.run,
        cast(PinnedSkill, object()),
        checkpoint,
        {"question": "synthetic"},
        caller_id=waiting.run.context.caller_id,
        space_id=waiting.run.context.space_id,
        approval_id="approval-1",
    )
    assert resumed.run.status is RunStatus.COMPLETED
    assert resumed.state.phase is AgentLoopPhase.COMPLETED
    assert len(resumed.state.observations) == 1
    history = await events.page(started.context.run_id, limit=200)
    assert AgentRunEventType.APPROVAL_REQUIRED in {event.event_type for event in history.events}
    assert [event.event_type for event in history.events].count(AgentRunEventType.COMPLETED) == 1


@pytest.mark.asyncio
async def test_waiting_approval_records_the_durable_request_identity() -> None:
    registry = InMemoryToolRegistry(handlers={"tool": search_handler}, approval_port=ApprovedPort())
    definition = registry.register(tool(write=True))
    requested: list[object] = []

    async def request_approval(context: AgentRunContext, record: object) -> str:
        assert context.run_id == loop_run(permissions=definition.permissions).context.run_id
        requested.append(record)
        return "durable-approval-1"

    result = await AgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(definition.ref,),
        system_prompt="Use only the registered synthetic Tool.",
        model_gateway=cast(
            ModelGateway,
            DecisionGateway(
                '{"action":"call_tool","tool_name":"write_note","arguments":{"query":"approved"}}'
            ),
        ),
        approval_request=request_approval,
    ).execute(
        loop_run(permissions=definition.permissions),
        cast(PinnedSkill, object()),
        {"question": "synthetic"},
        goal="Write the approved synthetic note.",
    )

    assert result.waiting_approval
    assert result.state.approval_id == "durable-approval-1"
    assert len(requested) == 1


@pytest.mark.asyncio
async def test_process_permission_also_enters_waiting_approval() -> None:
    registry = InMemoryToolRegistry(handlers={"tool": search_handler}, approval_port=ApprovedPort())
    definition = registry.register(tool(permission=ToolPermission.EXECUTE_PROCESS))
    result = await AgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(definition.ref,),
        system_prompt="Use only the registered synthetic Tool.",
        model_gateway=cast(
            ModelGateway,
            DecisionGateway(
                '{"action":"call_tool","tool_name":"search_knowledge","arguments":{"query":"process"}}'
            ),
        ),
    ).execute(
        loop_run(permissions=definition.permissions),
        cast(PinnedSkill, object()),
        {"question": "synthetic"},
        goal="Run the approved process operation.",
    )

    assert result.waiting_approval
    assert result.run.status is RunStatus.WAITING_APPROVAL
    assert result.state.phase is AgentLoopPhase.WAITING_APPROVAL
