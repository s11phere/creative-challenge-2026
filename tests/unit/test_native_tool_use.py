"""Synthetic-only tests for the feature-gated native Tool-use v2 executor."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from typing import cast
from uuid import UUID

import pytest
from agent_runtime import (
    InMemoryRuntimeStateStore,
    InMemoryToolRegistry,
    NativeSkillPin,
    NativeSkillRoute,
    NativeSkillSelection,
    NativeToolUseAgentLoopExecutor,
    ToolRef,
    checkpoint_state_sha256,
)
from agent_runtime.skills import PinnedSkill, SkillCompatibility
from agent_runtime.tools import JSONValue, ToolDefinition, ToolExecutionContext
from domain.agent_runtime import (
    AgentRun,
    AgentRunContext,
    RecoveryRejectedError,
    RunBudget,
    RunStatus,
    ToolPermission,
)
from model_gateway import (
    CapabilityAlias,
    ChatRequest,
    ChatResponse,
    ChatToolCall,
    FakeModelGateway,
    GatewayStatus,
    ModelErrorCode,
    ModelGateway,
    ModelGatewayError,
    ModelProvider,
    ModelUsage,
)


class SequenceNativeGateway:
    def __init__(self, *responses: ChatResponse) -> None:
        self._status = FakeModelGateway().status
        self._responses = list(responses)
        self.requests: list[ChatRequest] = []

    @property
    def status(self) -> GatewayStatus:
        return self._status

    async def chat(
        self,
        request: ChatRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.FAST_CHAT,
    ) -> ChatResponse:
        self.requests.append(request)
        assert capability is CapabilityAlias.FAST_CHAT
        return self._responses.pop(0)


class NoNativeToolUseGateway(SequenceNativeGateway):
    @property
    def status(self) -> GatewayStatus:
        return GatewayStatus(
            available=True,
            code="MODEL_READY",
            provider=ModelProvider.FAKE,
            capabilities=(CapabilityAlias.FAST_CHAT,),
            model_identity="fake-fast-chat-v1",
        )


class TimeoutNativeGateway(NoNativeToolUseGateway):
    @property
    def status(self) -> GatewayStatus:
        return FakeModelGateway().status

    async def chat(
        self,
        request: ChatRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.FAST_CHAT,
    ) -> ChatResponse:
        del request
        raise ModelGatewayError(
            ModelErrorCode.TIMEOUT,
            "synthetic timeout",
            retryable=True,
            capability=capability,
        )


@dataclass
class RecordingFinalizer:
    calls: int = 0
    text: str | None = None

    async def finalize(self, **kwargs: object) -> dict[str, JSONValue]:
        self.calls += 1
        self.text = cast(str, kwargs["terminal_text"])
        return {"message": self.text}


class SyntheticSkillCatalog:
    def __init__(self, *entries: NativeSkillSelection | NativeSkillRoute) -> None:
        self._selections = {
            entry.pin.name: entry for entry in entries if isinstance(entry, NativeSkillSelection)
        }
        self._routes = {
            entry.pin.name: entry.route if isinstance(entry, NativeSkillSelection) else entry
            for entry in entries
        }

    def list_routes(self) -> tuple[NativeSkillRoute, ...]:
        return tuple(self._routes.values())

    def select(self, name: str) -> NativeSkillSelection:
        selection = self._selections.get(name)
        if selection is None or not selection.route.adapter_available:
            raise ValueError("Synthetic Skill has no native adapter")
        return selection

    def resolve(self, pin: NativeSkillPin) -> NativeSkillSelection:
        selection = self._selections.get(pin.name)
        if selection is None or selection.pin != pin:
            raise ValueError("Synthetic Skill pin is unavailable")
        return selection


def _response(
    *, text: str = "", calls: tuple[ChatToolCall, ...] = (), finish_reason: str = "stop"
) -> ChatResponse:
    return ChatResponse(
        text=text,
        tool_calls=calls,
        finish_reason=finish_reason,
        usage=ModelUsage(input_tokens=3, output_tokens=2),
        capability=CapabilityAlias.FAST_CHAT,
        latency_ms=1.0,
    )


def _run(*, permissions: frozenset[ToolPermission]) -> AgentRun:
    return AgentRun(
        context=AgentRunContext(
            run_id=UUID("00000000-0000-4000-8000-000000000021"),
            space_id=UUID("00000000-0000-4000-8000-000000000022"),
            skill_name="native_fixture",
            skill_version="1.0.0",
            skill_content_sha256="a" * 64,
            trace_id="native-trace",
            caller_id="native-caller",
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


def _pin() -> PinnedSkill:
    return PinnedSkill(
        name="native_fixture",
        version="1.0.0",
        content_sha256="a" * 64,
        manifest_version="1",
        entrypoint_sha256="b" * 64,
        input_schema_sha256="c" * 64,
        output_schema_sha256="d" * 64,
        prompt_digests=(("prompts/system.md", "e" * 64),),
        compatibility=SkillCompatibility(runtime=">=0.1.0,<1.0.0", checkpoint_schema_versions=(1,)),
    )


async def _handler(
    arguments: dict[str, JSONValue], _context: ToolExecutionContext
) -> dict[str, JSONValue]:
    query = arguments.get("query")
    return {"status": "ok", "query": query if isinstance(query, str) else ""}


def _tool(*, permission: ToolPermission = ToolPermission.READ_KNOWLEDGE) -> ToolDefinition:
    return ToolDefinition(
        name="synthetic_lookup",
        version="1.0.0",
        description="Read synthetic metadata.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["query"],
            "properties": {"query": {"type": "string", "minLength": 1}},
        },
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["status", "query"],
            "properties": {
                "status": {"type": "string"},
                "query": {"type": "string"},
            },
        },
        permissions=frozenset({permission}),
        handler_name="tool",
        model_visible=True,
    )


def _fake_tool() -> ToolDefinition:
    return ToolDefinition(
        name="synthetic_lookup",
        version="1.0.0",
        description="Read synthetic metadata.",
        input_schema={"type": "object", "additionalProperties": False},
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["status", "query"],
            "properties": {"status": {"type": "string"}, "query": {"type": "string"}},
        },
        permissions=frozenset({ToolPermission.READ_KNOWLEDGE}),
        handler_name="tool",
        model_visible=True,
    )


def _skill(
    name: str,
    *,
    instructions: str,
    adapter_available: bool = True,
    tools: tuple[ToolRef, ...] = (),
) -> NativeSkillSelection:
    return NativeSkillSelection(
        route=NativeSkillRoute(
            pin=NativeSkillPin(
                name=name,
                version="1.0.0",
                content_sha256=hashlib.sha256(name.encode("utf-8")).hexdigest(),
            ),
            description=f"Synthetic route for {name}.",
            command=name.replace("_", "-"),
            adapter_available=adapter_available,
        ),
        instructions=instructions,
        allowed_tools=tools,
    )


def _unavailable_skill(name: str) -> NativeSkillRoute:
    return NativeSkillRoute(
        pin=NativeSkillPin(
            name=name,
            version="1.0.0",
            content_sha256=hashlib.sha256(name.encode("utf-8")).hexdigest(),
        ),
        description=f"Synthetic route for {name}.",
        command=name.replace("_", "-"),
        adapter_available=False,
    )


async def test_native_tool_use_replays_one_result_then_finalizes_once() -> None:
    registry = InMemoryToolRegistry(handlers={"tool": _handler})
    definition = registry.register(_tool())
    call = ChatToolCall(
        call_id="call_1",
        tool_name=definition.name,
        arguments={"query": "synthetic"},
    )
    gateway = SequenceNativeGateway(
        _response(calls=(call,), finish_reason="tool_calls"),
        _response(text="Synthetic terminal response."),
    )
    finalizer = RecordingFinalizer()
    result = await NativeToolUseAgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(definition.ref,),
        system_prompt="Use native Tools only when needed.",
        model_gateway=cast(ModelGateway, gateway),
        state_store=InMemoryRuntimeStateStore(),
        finalizer=finalizer,
    ).execute(
        _run(permissions=definition.permissions),
        cast(PinnedSkill, object()),
        {"question": "synthetic"},
        goal="Answer the synthetic request.",
    )

    assert result.run.status is RunStatus.COMPLETED
    assert result.output == {"message": "Synthetic terminal response."}
    assert finalizer.calls == 1
    assert len(gateway.requests) == 2
    assert gateway.requests[0].tools[0].name == definition.name
    assert gateway.requests[0].tool_results == ()
    assert gateway.requests[1].tool_call_history == (call,)
    assert gateway.requests[1].tool_results[0].observation == {
        "status": "ok",
        "query": "synthetic",
    }
    assert result.run.usage.steps == 2
    assert result.run.usage.tool_calls == 1


async def test_fake_gateway_executes_a_tool_then_returns_one_terminal_response() -> None:
    registry = InMemoryToolRegistry(handlers={"tool": _handler})
    definition = registry.register(_fake_tool())
    finalizer = RecordingFinalizer()
    result = await NativeToolUseAgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(definition.ref,),
        system_prompt="Use native Tools only when needed.",
        model_gateway=FakeModelGateway(),
        finalizer=finalizer,
    ).execute(
        _run(permissions=definition.permissions),
        cast(PinnedSkill, object()),
        {"question": "synthetic"},
        goal="Answer the synthetic request.",
    )

    assert result.run.status is RunStatus.COMPLETED
    assert result.run.usage.steps == 2
    assert result.run.usage.tool_calls == 1
    assert finalizer.calls == 1
    assert finalizer.text is not None
    assert finalizer.text.startswith("fake-response-")


async def test_native_tool_use_resumes_an_approved_pending_call_once() -> None:
    handler_calls = 0

    async def count_handler(
        arguments: dict[str, JSONValue], context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        nonlocal handler_calls
        handler_calls += 1
        return await _handler(arguments, context)

    class ApprovedPort:
        async def is_approved(self, approval_id: str, _context: AgentRunContext) -> bool:
            return approval_id == "approval-1"

    registry = InMemoryToolRegistry(handlers={"tool": count_handler}, approval_port=ApprovedPort())
    definition = registry.register(_tool(permission=ToolPermission.WRITE_KNOWLEDGE))
    call = ChatToolCall("call_1", definition.name, {"query": "synthetic"})
    gateway = SequenceNativeGateway(
        _response(calls=(call,), finish_reason="tool_calls"),
        _response(text="Synthetic terminal response."),
    )
    state_store = InMemoryRuntimeStateStore()

    async def request_approval(_context: AgentRunContext, _record: object) -> str:
        return "approval-1"

    executor = NativeToolUseAgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(definition.ref,),
        system_prompt="Use native Tools only when needed.",
        model_gateway=cast(ModelGateway, gateway),
        state_store=state_store,
        approval_request=request_approval,
    )
    started = _run(permissions=definition.permissions)
    waiting = await executor.execute(
        started,
        _pin(),
        {"question": "synthetic"},
        goal="Write the approved synthetic note.",
    )

    assert waiting.waiting_approval
    assert waiting.run.status is RunStatus.WAITING_APPROVAL
    assert handler_calls == 0
    checkpoint = await state_store.get_latest(started.context.run_id)
    assert checkpoint is not None
    assert checkpoint.approval_id == "approval-1"

    resumed = await executor.resume(
        waiting.run,
        _pin(),
        checkpoint,
        {"question": "synthetic"},
        caller_id=started.context.caller_id,
        space_id=started.context.space_id,
        approval_id="approval-1",
    )

    assert resumed.run.status is RunStatus.COMPLETED
    assert resumed.output == {"message": "Synthetic terminal response."}
    assert handler_calls == 1
    assert len(gateway.requests) == 2
    assert resumed.run.usage.tool_calls == 1


async def test_native_tool_use_rejects_multiple_calls_without_invoking_a_tool() -> None:
    registry = InMemoryToolRegistry(handlers={"tool": _handler})
    definition = registry.register(_tool())
    gateway = SequenceNativeGateway(
        _response(
            calls=(
                ChatToolCall("call_1", definition.name, {"query": "one"}),
                ChatToolCall("call_2", definition.name, {"query": "two"}),
            ),
            finish_reason="tool_calls",
        )
    )
    result = await NativeToolUseAgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(definition.ref,),
        system_prompt="Use native Tools only when needed.",
        model_gateway=cast(ModelGateway, gateway),
    ).execute(
        _run(permissions=definition.permissions),
        cast(PinnedSkill, object()),
        {"question": "synthetic"},
        goal="Answer the synthetic request.",
    )

    assert result.run.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.code == "RUN_NATIVE_TOOL_USE_MULTIPLE_CALLS"
    assert result.run.usage.tool_calls == 0


async def test_native_tool_use_rejects_empty_terminal_text() -> None:
    registry = InMemoryToolRegistry(handlers={"tool": _handler})
    definition = registry.register(_tool())
    result = await NativeToolUseAgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(definition.ref,),
        system_prompt="Use native Tools only when needed.",
        model_gateway=cast(ModelGateway, SequenceNativeGateway(_response())),
    ).execute(
        _run(permissions=definition.permissions),
        cast(PinnedSkill, object()),
        {"question": "synthetic"},
        goal="Answer the synthetic request.",
    )

    assert result.run.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.code == "RUN_NATIVE_TOOL_USE_TERMINAL_EMPTY"


async def test_native_tool_use_fails_closed_without_gateway_capability() -> None:
    registry = InMemoryToolRegistry(handlers={"tool": _handler})
    definition = registry.register(_tool())
    result = await NativeToolUseAgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(definition.ref,),
        system_prompt="Use native Tools only when needed.",
        model_gateway=cast(ModelGateway, NoNativeToolUseGateway(_response(text="unused"))),
    ).execute(
        _run(permissions=definition.permissions),
        cast(PinnedSkill, object()),
        {"question": "synthetic"},
        goal="Answer the synthetic request.",
    )

    assert result.run.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.code == "RUN_NATIVE_TOOL_USE_UNSUPPORTED"


async def test_native_tool_use_maps_model_timeout_to_a_stable_run_error() -> None:
    registry = InMemoryToolRegistry(handlers={"tool": _handler})
    definition = registry.register(_tool())
    result = await NativeToolUseAgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(definition.ref,),
        system_prompt="Use native Tools only when needed.",
        model_gateway=cast(ModelGateway, TimeoutNativeGateway()),
    ).execute(
        _run(permissions=definition.permissions),
        cast(PinnedSkill, object()),
        {"question": "synthetic"},
        goal="Answer the synthetic request.",
    )

    assert result.run.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.code == "RUN_MODEL_TIMEOUT"
    assert result.error.retryable is True


async def test_native_tool_use_keeps_unselected_skill_instructions_and_tools_out_of_request() -> (
    None
):
    registry = InMemoryToolRegistry(handlers={"tool": _handler})
    definition = registry.register(_tool())
    selected = _skill(
        "knowledge_agent",
        instructions="SELECTED_SKILL_INSTRUCTIONS",
        tools=(definition.ref,),
    )
    unselected = _skill("skill_creator", instructions="UNSELECTED_SKILL_INSTRUCTIONS")
    gateway = SequenceNativeGateway(_response(text="Synthetic terminal response."))
    result = await NativeToolUseAgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(definition.ref,),
        system_prompt="Native base prompt.",
        model_gateway=cast(ModelGateway, gateway),
        skill_catalog=SyntheticSkillCatalog(selected, unselected),
    ).execute(
        _run(permissions=definition.permissions),
        _pin(),
        {"question": "synthetic"},
        goal="Answer the synthetic request.",
    )

    assert result.run.status is RunStatus.COMPLETED
    request = gateway.requests[0]
    assert [tool.name for tool in request.tools] == ["list_skills", "invoke_skill"]
    assert "UNSELECTED_SKILL_INSTRUCTIONS" not in request.messages[0].content
    assert "SELECTED_SKILL_INSTRUCTIONS" not in request.messages[0].content


async def test_native_tool_use_selects_one_skill_before_exposing_its_instructions_and_tools() -> (
    None
):
    registry = InMemoryToolRegistry(handlers={"tool": _handler})
    definition = registry.register(_tool())
    selected = _skill(
        "knowledge_agent",
        instructions="SELECTED_SKILL_INSTRUCTIONS",
        tools=(definition.ref,),
    )
    unselected = _skill("skill_creator", instructions="UNSELECTED_SKILL_INSTRUCTIONS")
    gateway = SequenceNativeGateway(
        _response(
            calls=(ChatToolCall("call_1", "invoke_skill", {"name": "knowledge_agent"}),),
            finish_reason="tool_calls",
        ),
        _response(
            calls=(ChatToolCall("call_2", definition.name, {"query": "synthetic"}),),
            finish_reason="tool_calls",
        ),
        _response(text="Synthetic terminal response."),
    )
    result = await NativeToolUseAgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(definition.ref,),
        system_prompt="Native base prompt.",
        model_gateway=cast(ModelGateway, gateway),
        skill_catalog=SyntheticSkillCatalog(selected, unselected),
    ).execute(
        _run(permissions=definition.permissions),
        _pin(),
        {"question": "synthetic"},
        goal="Answer the synthetic request.",
    )

    assert result.run.status is RunStatus.COMPLETED
    assert [tool.name for tool in gateway.requests[0].tools] == ["list_skills", "invoke_skill"]
    assert [tool.name for tool in gateway.requests[1].tools] == [
        "list_skills",
        "invoke_skill",
        definition.name,
    ]
    assert "SELECTED_SKILL_INSTRUCTIONS" in gateway.requests[1].messages[0].content
    assert "UNSELECTED_SKILL_INSTRUCTIONS" not in gateway.requests[1].messages[0].content
    assert gateway.requests[1].tool_results[0].observation == {
        "name": "knowledge_agent",
        "version": "1.0.0",
        "selected": True,
    }


async def test_native_tool_use_lists_routes_without_skill_instructions() -> None:
    registry = InMemoryToolRegistry(handlers={"tool": _handler})
    definition = registry.register(_tool())
    knowledge = _skill(
        "knowledge_agent",
        instructions="KNOWLEDGE_SKILL_INSTRUCTIONS",
        tools=(definition.ref,),
    )
    creator = _unavailable_skill("skill_creator")
    gateway = SequenceNativeGateway(
        _response(calls=(ChatToolCall("call_1", "list_skills", {}),), finish_reason="tool_calls"),
        _response(text="Synthetic terminal response."),
    )
    result = await NativeToolUseAgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(definition.ref,),
        system_prompt="Native base prompt.",
        model_gateway=cast(ModelGateway, gateway),
        skill_catalog=SyntheticSkillCatalog(knowledge, creator),
    ).execute(
        _run(permissions=definition.permissions),
        _pin(),
        {"question": "synthetic"},
        goal="Answer the synthetic request.",
    )

    assert result.run.status is RunStatus.COMPLETED
    observation = gateway.requests[1].tool_results[0].observation
    assert observation["skills"] == [
        {
            "name": "knowledge_agent",
            "version": "1.0.0",
            "description": "Synthetic route for knowledge_agent.",
            "command": "knowledge-agent",
            "adapter_available": True,
        },
        {
            "name": "skill_creator",
            "version": "1.0.0",
            "description": "Synthetic route for skill_creator.",
            "command": "skill-creator",
            "adapter_available": False,
        },
    ]
    assert "KNOWLEDGE_SKILL_INSTRUCTIONS" not in str(observation)
    assert "CREATOR_SKILL_INSTRUCTIONS" not in str(observation)


async def test_native_tool_use_rejects_skill_without_runtime_adapter() -> None:
    registry = InMemoryToolRegistry(handlers={"tool": _handler})
    definition = registry.register(_tool())
    unavailable = _unavailable_skill("skill_creator")
    result = await NativeToolUseAgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(definition.ref,),
        system_prompt="Native base prompt.",
        model_gateway=cast(
            ModelGateway,
            SequenceNativeGateway(
                _response(
                    calls=(ChatToolCall("call_1", "invoke_skill", {"name": "skill_creator"}),),
                    finish_reason="tool_calls",
                )
            ),
        ),
        skill_catalog=SyntheticSkillCatalog(unavailable),
    ).execute(
        _run(permissions=definition.permissions),
        _pin(),
        {"question": "synthetic"},
        goal="Answer the synthetic request.",
    )

    assert result.run.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.code == "RUN_NATIVE_TOOL_USE_SKILL_DENIED"


async def test_native_tool_use_rejects_a_third_selected_skill() -> None:
    registry = InMemoryToolRegistry(handlers={"tool": _handler})
    definition = registry.register(_tool())
    first = _skill("first_skill", instructions="FIRST")
    second = _skill("second_skill", instructions="SECOND")
    third = _skill("third_skill", instructions="THIRD")
    gateway = SequenceNativeGateway(
        _response(
            calls=(ChatToolCall("call_1", "invoke_skill", {"name": "first_skill"}),),
            finish_reason="tool_calls",
        ),
        _response(
            calls=(ChatToolCall("call_2", "invoke_skill", {"name": "second_skill"}),),
            finish_reason="tool_calls",
        ),
        _response(
            calls=(ChatToolCall("call_3", "invoke_skill", {"name": "third_skill"}),),
            finish_reason="tool_calls",
        ),
    )
    result = await NativeToolUseAgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(definition.ref,),
        system_prompt="Native base prompt.",
        model_gateway=cast(ModelGateway, gateway),
        skill_catalog=SyntheticSkillCatalog(first, second, third),
    ).execute(
        _run(permissions=definition.permissions),
        _pin(),
        {"question": "synthetic"},
        goal="Answer the synthetic request.",
    )

    assert result.run.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.code == "RUN_NATIVE_TOOL_USE_SKILL_DENIED"


async def test_native_tool_use_rejects_a_skill_tool_outside_the_server_allowlist() -> None:
    registry = InMemoryToolRegistry(handlers={"tool": _handler})
    definition = registry.register(_tool())
    selected = _skill(
        "knowledge_agent",
        instructions="SELECTED_SKILL_INSTRUCTIONS",
        tools=(ToolRef("unapproved_tool", "1.0.0"),),
    )
    result = await NativeToolUseAgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(definition.ref,),
        system_prompt="Native base prompt.",
        model_gateway=cast(
            ModelGateway,
            SequenceNativeGateway(
                _response(
                    calls=(ChatToolCall("call_1", "invoke_skill", {"name": "knowledge_agent"}),),
                    finish_reason="tool_calls",
                )
            ),
        ),
        skill_catalog=SyntheticSkillCatalog(selected),
    ).execute(
        _run(permissions=definition.permissions),
        _pin(),
        {"question": "synthetic"},
        goal="Answer the synthetic request.",
    )

    assert result.run.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.code == "RUN_NATIVE_TOOL_USE_SKILL_TOOL_DENIED"


async def test_native_tool_use_limits_selected_skill_instruction_context() -> None:
    registry = InMemoryToolRegistry(handlers={"tool": _handler})
    definition = registry.register(_tool())
    selected = _skill("knowledge_agent", instructions="TOO_LONG")
    result = await NativeToolUseAgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(definition.ref,),
        system_prompt="Native base prompt.",
        model_gateway=cast(
            ModelGateway,
            SequenceNativeGateway(
                _response(
                    calls=(ChatToolCall("call_1", "invoke_skill", {"name": "knowledge_agent"}),),
                    finish_reason="tool_calls",
                )
            ),
        ),
        skill_catalog=SyntheticSkillCatalog(selected),
        max_selected_skill_instruction_bytes=1,
    ).execute(
        _run(permissions=definition.permissions),
        _pin(),
        {"question": "synthetic"},
        goal="Answer the synthetic request.",
    )

    assert result.run.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.code == "RUN_NATIVE_TOOL_USE_SKILL_CONTEXT_LIMIT"


async def test_native_tool_use_recovers_selected_skill_pin_and_rejects_tampering() -> None:
    class ApprovedPort:
        async def is_approved(self, approval_id: str, _context: AgentRunContext) -> bool:
            return approval_id == "approval-1"

    registry = InMemoryToolRegistry(handlers={"tool": _handler}, approval_port=ApprovedPort())
    definition = registry.register(_tool(permission=ToolPermission.WRITE_KNOWLEDGE))
    selected = _skill(
        "knowledge_agent",
        instructions="SELECTED_SKILL_INSTRUCTIONS",
        tools=(definition.ref,),
    )
    gateway = SequenceNativeGateway(
        _response(
            calls=(ChatToolCall("call_1", "invoke_skill", {"name": "knowledge_agent"}),),
            finish_reason="tool_calls",
        ),
        _response(
            calls=(ChatToolCall("call_2", definition.name, {"query": "synthetic"}),),
            finish_reason="tool_calls",
        ),
        _response(text="Synthetic terminal response."),
    )
    state_store = InMemoryRuntimeStateStore()

    async def request_approval(_context: AgentRunContext, _record: object) -> str:
        return "approval-1"

    executor = NativeToolUseAgentLoopExecutor(
        tool_registry=registry,
        allowed_tools=(definition.ref,),
        system_prompt="Native base prompt.",
        model_gateway=cast(ModelGateway, gateway),
        state_store=state_store,
        approval_request=request_approval,
        skill_catalog=SyntheticSkillCatalog(selected),
    )
    started = _run(permissions=definition.permissions)
    waiting = await executor.execute(
        started,
        _pin(),
        {"question": "synthetic"},
        goal="Answer the synthetic request.",
    )
    checkpoint = await state_store.get_latest(started.context.run_id)

    assert waiting.waiting_approval
    assert checkpoint is not None
    assert checkpoint.state["selected_skills"] == [
        {
            "name": "knowledge_agent",
            "version": "1.0.0",
            "content_sha256": hashlib.sha256(b"knowledge_agent").hexdigest(),
        }
    ]

    raw_state = dict(checkpoint.state)
    raw_state["selected_skills"] = [
        {
            "name": "knowledge_agent",
            "version": "1.0.0",
            "content_sha256": "0" * 64,
        }
    ]
    tampered = replace(checkpoint, state=raw_state, state_sha256=checkpoint_state_sha256(raw_state))
    with pytest.raises(RecoveryRejectedError):
        await executor.resume(
            waiting.run,
            _pin(),
            tampered,
            {"question": "synthetic"},
            caller_id=started.context.caller_id,
            space_id=started.context.space_id,
            approval_id="approval-1",
        )

    resumed = await executor.resume(
        waiting.run,
        _pin(),
        checkpoint,
        {"question": "synthetic"},
        caller_id=started.context.caller_id,
        space_id=started.context.space_id,
        approval_id="approval-1",
    )
    assert resumed.run.status is RunStatus.COMPLETED
    assert resumed.state.selected_skills == (selected.pin,)
