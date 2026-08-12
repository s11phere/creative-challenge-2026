from __future__ import annotations

import json
from typing import cast
from uuid import UUID

import pytest
from agent_runtime import (
    InMemoryRuntimeStateStore,
    InMemoryToolRegistry,
    LLMDecision,
    LLMDecisionAction,
)
from agent_runtime.skills import PinnedSkill, SkillCompatibility
from agent_runtime.tools import JSONValue, ToolDefinition, ToolExecutionContext, ToolRef
from application.assistant import (
    AssistantConversationLoopFinalizer,
    AssistantSkillContext,
    AssistantTurnSubmission,
    AutonomousAssistantLoopService,
    ConversationRunService,
)
from application.assistant.autonomous_loop import _next_workspace_artifact_decision
from application.qa import InMemoryGroundedQARepository
from domain.agent_loop import AgentLoopState, AgentLoopTask, AgentLoopToolObservation
from domain.agent_runtime import AgentRun, AgentRunContext, RunBudget, ToolPermission
from domain.agent_sse import AgentRunEventLog
from domain.assistant_sse import AssistantEventLog
from domain.conversation_run import ConversationRunStatus
from domain.qa_persistence import ConversationRecord, QARunRecord
from model_gateway import (
    CapabilityAlias,
    ChatRequest,
    ChatResponse,
    FakeModelGateway,
    GatewayStatus,
    ModelGateway,
    ModelUsage,
)


class SerialDecisionGateway:
    def __init__(self) -> None:
        self._delegate = FakeModelGateway()
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
        payload = json.loads(request.messages[-1].content)
        observations = payload["state"]["observations"]
        if not observations:
            decision = {
                "action": "call_tool",
                "tool_name": "research_skill",
                "arguments": {"topic": "first"},
            }
        elif observations[-1]["tool_name"] == "research_skill":
            assert observations[-1]["output"] == {"result": "research-ready"}
            decision = {
                "action": "call_tool",
                "tool_name": "review_skill",
                "arguments": {"topic": "second"},
            }
        else:
            assert observations[-1]["output"] == {"result": "review-ready"}
            decision = {
                "action": "complete",
                "reason": "Both Skill adapters completed.",
                "final_response": "Combined result.",
            }
        text = json.dumps(decision, separators=(",", ":"))
        return ChatResponse(
            text=text,
            finish_reason="stop",
            usage=ModelUsage(input_tokens=5, output_tokens=3),
            capability=capability,
            latency_ms=1.0,
        )


class UnavailableWorkspaceToolGateway:
    """Model fixture that incorrectly selects a Tool hidden by server policy."""

    def __init__(self) -> None:
        self._delegate = FakeModelGateway()
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
            text=json.dumps(
                {
                    "action": "call_tool",
                    "tool_name": "fs_write",
                    "arguments": {"path": "omnistudio-modules.md", "content": "..."},
                },
                separators=(",", ":"),
            ),
            finish_reason="stop",
            usage=ModelUsage(input_tokens=5, output_tokens=3),
            capability=capability,
            latency_ms=1.0,
        )


class EscalatedAnswerGateway:
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


async def _research(
    _arguments: dict[str, JSONValue], _context: ToolExecutionContext
) -> dict[str, JSONValue]:
    return {"result": "research-ready"}


async def _review(
    _arguments: dict[str, JSONValue], _context: ToolExecutionContext
) -> dict[str, JSONValue]:
    return {"result": "review-ready"}


def _tool(name: str, handler_name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        version="1.0.0",
        description=f"Pinned adapter for {name}.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["topic"],
            "properties": {"topic": {"type": "string", "minLength": 1}},
        },
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["result"],
            "properties": {"result": {"type": "string"}},
        },
        permissions=frozenset({ToolPermission.READ_KNOWLEDGE}),
        handler_name=handler_name,
        model_visible=True,
    )


def _pin() -> PinnedSkill:
    return PinnedSkill(
        name="assistant_agent",
        version="0.1.0",
        content_sha256="a" * 64,
        manifest_version="1",
        entrypoint_sha256="b" * 64,
        input_schema_sha256="c" * 64,
        output_schema_sha256="d" * 64,
        prompt_digests=(("prompts/system.md", "e" * 64),),
        compatibility=SkillCompatibility(runtime=">=0.1.0,<1.0.0", checkpoint_schema_versions=(1,)),
    )


@pytest.mark.asyncio
async def test_top_level_loop_observes_one_skill_adapter_before_selecting_another() -> None:
    repository = InMemoryGroundedQARepository()
    conversation = ConversationRecord(
        conversation_id=UUID(int=1001), space_id=UUID(int=1002), owner_id="loop-user"
    )
    await repository.create_conversation(conversation)
    submitted = await ConversationRunService(conversations=repository, runs=repository).submit(
        AssistantTurnSubmission(
            conversation_id=conversation.conversation_id,
            content="Complete the synthetic multi-skill task.",
            idempotency_key="autonomous-loop-1",
        )
    )
    await repository.claim_conversation_run(
        submitted.run_id, lease_owner="test-worker", lease_seconds=60
    )
    registry = InMemoryToolRegistry(handlers={"research": _research, "review": _review})
    research = registry.register(_tool("research_skill", "research"))
    review = registry.register(_tool("review_skill", "review"))
    gateway = SerialDecisionGateway()
    agent_events = AgentRunEventLog()
    service = AutonomousAssistantLoopService(
        runs=repository,
        messages=repository,
        gateway=cast(ModelGateway, gateway),
        events=AssistantEventLog(),
        agent_events=agent_events,
        runtime_state=InMemoryRuntimeStateStore(),
        pin=_pin(),
        budget=RunBudget(
            max_steps=8,
            max_tool_calls=4,
            max_input_tokens=100,
            max_output_tokens=100,
            timeout_seconds=30,
        ),
        tool_registry=registry,
        allowed_tools=(
            ToolRef(research.name, research.version),
            ToolRef(review.name, review.version),
        ),
        tool_skill_refs={
            research.ref: ToolRef("research_skill", "1.0.0"),
            review.ref: ToolRef("summary_skill", "2.0.0"),
        },
        qa_results=_no_qa_result,
        skill_contexts=(
            AssistantSkillContext(
                name="research_skill",
                version="1.0.0",
                description="Synthetic trusted Skill context.",
                instructions="Use this adapter only when its result advances the task.",
            ),
            AssistantSkillContext(
                name="summary_skill",
                version="2.0.0",
                description="Synthetic summary Skill context.",
                instructions="Summarize only when the user explicitly asks.",
            ),
        ),
        workspace_context={
            "selected": True,
            "tools_enabled": False,
            "status": "model_visibility_consent_required",
        },
    )

    completed = await service.execute(submitted.run_id, trace_id="a" * 32)

    assert completed is not None
    assert completed.status is ConversationRunStatus.COMPLETED
    assert completed.result is not None
    message = await repository.get_message(completed.result.message_id)
    assert message is not None
    assert message.content == "Combined result."
    assert len(gateway.requests) == 3
    system_prompt = gateway.requests[0].messages[0].content
    assert "Synthetic trusted Skill context." in system_prompt
    assert "Synthetic summary Skill context." in system_prompt
    assert "<active_skill_catalog" in system_prompt
    assert "- research_skill v1.0.0" in system_prompt
    assert "- summary_skill v2.0.0" in system_prompt
    assert '"name":"research_skill"' in system_prompt
    assert '"name":"review_skill"' in system_prompt
    assert "saving it is part of the requested outcome" in system_prompt
    assert "A successful Tool result is one completed observation" in system_prompt
    assert '<workspace-tool-availability trust="trusted_configuration">' in system_prompt
    assert "workspace Tools are unavailable for this Run" in system_prompt
    history = await agent_events.page(submitted.run_id, limit=200)
    assert history.events[0].event_type.value == "accepted"
    assert any(event.event_type.value == "tool_output" for event in history.events)
    activations = [event for event in history.events if event.event_type.value == "skill_activated"]
    assert [
        (event.payload["skill_name"], event.payload["skill_version"]) for event in activations
    ] == [
        ("assistant_agent", "0.1.0"),
        ("research_skill", "1.0.0"),
        ("summary_skill", "2.0.0"),
    ]


@pytest.mark.asyncio
async def test_unavailable_workspace_tool_decision_publishes_configuration_explanation() -> None:
    repository = InMemoryGroundedQARepository()
    conversation = ConversationRecord(
        conversation_id=UUID(int=1051), space_id=UUID(int=1052), owner_id="loop-user"
    )
    await repository.create_conversation(conversation)
    submitted = await ConversationRunService(conversations=repository, runs=repository).submit(
        AssistantTurnSubmission(
            conversation_id=conversation.conversation_id,
            content="介绍一下 omnistudio 的主要模块，并保存为 markdown 文件",
            idempotency_key="autonomous-loop-workspace-unavailable-1",
        )
    )
    await repository.claim_conversation_run(
        submitted.run_id, lease_owner="test-worker", lease_seconds=60
    )
    registry = InMemoryToolRegistry(handlers={"research": _research})
    research = registry.register(_tool("research_skill", "research"))
    gateway = UnavailableWorkspaceToolGateway()
    service = AutonomousAssistantLoopService(
        runs=repository,
        messages=repository,
        gateway=cast(ModelGateway, gateway),
        events=AssistantEventLog(),
        agent_events=AgentRunEventLog(),
        runtime_state=InMemoryRuntimeStateStore(),
        pin=_pin(),
        budget=RunBudget(
            max_steps=4,
            max_tool_calls=2,
            max_input_tokens=100,
            max_output_tokens=100,
            timeout_seconds=30,
        ),
        tool_registry=registry,
        allowed_tools=(ToolRef(research.name, research.version),),
        qa_results=_no_qa_result,
        skill_contexts=(),
        workspace_context={
            "selected": True,
            "tools_enabled": False,
            "status": "model_visibility_consent_required",
        },
    )

    completed = await service.execute(submitted.run_id, trace_id="d" * 32)

    assert completed is not None
    assert completed.status is ConversationRunStatus.COMPLETED
    assert completed.result is not None
    message = await repository.get_message(completed.result.message_id)
    assert message is not None
    assert "尚未启用外部模型的工作区写入能力" in message.content
    assert "确认" in message.content
    assert len(gateway.requests) == 1


@pytest.mark.asyncio
async def test_loop_escalates_a_long_answer_truncation_and_publishes_the_generation() -> None:
    repository = InMemoryGroundedQARepository()
    conversation = ConversationRecord(
        conversation_id=UUID(int=1301), space_id=UUID(int=1302), owner_id="loop-user"
    )
    await repository.create_conversation(conversation)
    question = "请你从 VAE -> GAN -> DDPM -> SDE 这一条线梳理生成模型"
    submitted = await ConversationRunService(conversations=repository, runs=repository).submit(
        AssistantTurnSubmission(
            conversation_id=conversation.conversation_id,
            content=question,
            idempotency_key="autonomous-loop-long-answer-1",
        )
    )
    await repository.claim_conversation_run(
        submitted.run_id, lease_owner="test-worker", lease_seconds=60
    )
    registry = InMemoryToolRegistry(handlers={"research": _research})
    research = registry.register(_tool("research_skill", "research"))
    full_answer = "# 生成模型谱系\n从 VAE 到 SDE 的完整梳理。"
    gateway = EscalatedAnswerGateway(full_answer)
    service = AutonomousAssistantLoopService(
        runs=repository,
        messages=repository,
        gateway=cast(ModelGateway, gateway),
        events=AssistantEventLog(),
        agent_events=AgentRunEventLog(),
        runtime_state=InMemoryRuntimeStateStore(),
        pin=_pin(),
        budget=RunBudget(
            max_steps=4,
            max_tool_calls=2,
            max_input_tokens=100,
            max_output_tokens=100,
            timeout_seconds=30,
        ),
        tool_registry=registry,
        allowed_tools=(ToolRef(research.name, research.version),),
        qa_results=_no_qa_result,
        skill_contexts=(),
        workspace_context={},
    )

    completed = await service.execute(submitted.run_id, trace_id="e" * 32)

    assert completed is not None
    assert completed.status is ConversationRunStatus.COMPLETED
    assert completed.result is not None
    message = await repository.get_message(completed.result.message_id)
    assert message is not None
    assert message.content == full_answer
    assert len(gateway.requests) == 2
    generation_request = gateway.requests[1]
    assert generation_request.max_tokens == 6_144
    assert generation_request.temperature == 0.2
    assert generation_request.messages[1].content == question


def test_completion_recovery_requires_a_workspace_write_after_qa_finalization() -> None:
    state = AgentLoopState.accepted(
        AgentLoopTask("OmniStudio modules: save as a Markdown file.")
    ).start()

    list_decision = _next_workspace_artifact_decision(state, state.task.goal)

    assert list_decision.action is LLMDecisionAction.CALL_TOOL
    assert list_decision.tool_name == "fs_list"
    assert list_decision.arguments == {"path": "."}

    state = (
        state.begin_iteration({"action": "call_tool"})
        .request_tool(
            name="fs_list",
            version="1.0.0",
            arguments={"path": "."},
            idempotency_key="list-1",
            request_fingerprint="list-1",
        )
        .start_tool()
        .observe(
            AgentLoopToolObservation(
                iteration=1,
                tool_name="fs_list",
                tool_version="1.0.0",
                idempotency_key="list-1",
                input_summary="sha256:input",
                output_summary="sha256:output",
                model_output={
                    "entries": [{"path": "omnistudio-modules.md", "kind": "file", "size_bytes": 1}]
                },
            )
        )
    )

    write_decision = _next_workspace_artifact_decision(state, state.task.goal)

    assert write_decision.action is LLMDecisionAction.CALL_TOOL
    assert write_decision.tool_name == "fs_write"
    assert write_decision.arguments == {
        "path": "omnistudio-modules-2.md",
        "content": "{{current_grounded_qa_answer}}",
    }


@pytest.mark.asyncio
async def test_top_level_loop_publishes_a_safe_direct_refusal() -> None:
    repository = InMemoryGroundedQARepository()
    conversation = ConversationRecord(
        conversation_id=UUID(int=1101), space_id=UUID(int=1102), owner_id="loop-user"
    )
    await repository.create_conversation(conversation)
    submitted = await ConversationRunService(conversations=repository, runs=repository).submit(
        AssistantTurnSubmission(
            conversation_id=conversation.conversation_id,
            content="Synthetic refusal task.",
            idempotency_key="autonomous-loop-refusal-1",
        )
    )
    runtime = AgentRun(
        context=AgentRunContext(
            run_id=submitted.run_id,
            space_id=conversation.space_id,
            skill_name="assistant_agent",
            skill_version="0.1.0",
            skill_content_sha256="a" * 64,
            trace_id="b" * 32,
            caller_id="loop-user",
            granted_permissions=frozenset({ToolPermission.MODEL}),
        ),
        budget=RunBudget(),
    )
    finalizer = AssistantConversationLoopFinalizer(
        runs=repository,
        qa_results=_no_qa_result,
        model_identity="fake",
    )

    result = await finalizer.finalize(
        run=runtime,
        task=AgentLoopTask("Synthetic refusal task."),
        decision=LLMDecision(
            action=LLMDecisionAction.REFUSE,
            reason="Synthetic policy refusal.",
        ),
        state=AgentLoopState.accepted(AgentLoopTask("Synthetic refusal task.")),
        input_data={},
    )

    assert result == {"status": "refused", "publication": "refusal"}
    refused = await repository.get_conversation_run(submitted.run_id)
    assert refused is not None
    assert refused.status is ConversationRunStatus.REFUSED
    assert refused.result is not None
    message = await repository.get_message(refused.result.message_id)
    assert message is not None
    assert message.content == "I cannot help with that request."


@pytest.mark.asyncio
async def test_clarification_keeps_document_resolution_reason_actionable() -> None:
    repository = InMemoryGroundedQARepository()
    conversation = ConversationRecord(
        conversation_id=UUID(int=1201), space_id=UUID(int=1202), owner_id="loop-user"
    )
    await repository.create_conversation(conversation)
    submitted = await ConversationRunService(conversations=repository, runs=repository).submit(
        AssistantTurnSubmission(
            conversation_id=conversation.conversation_id,
            content="Summarize the document.",
            idempotency_key="autonomous-loop-clarification-1",
        )
    )
    runtime = AgentRun(
        context=AgentRunContext(
            run_id=submitted.run_id,
            space_id=conversation.space_id,
            skill_name="assistant_agent",
            skill_version="0.1.0",
            skill_content_sha256="a" * 64,
            trace_id="c" * 32,
            caller_id="loop-user",
            granted_permissions=frozenset({ToolPermission.MODEL}),
        ),
        budget=RunBudget(),
    )
    finalizer = AssistantConversationLoopFinalizer(
        runs=repository,
        qa_results=_no_qa_result,
        model_identity="fake",
    )
    state = AgentLoopState.accepted(AgentLoopTask("Summarize the document.")).start()
    state = (
        state.begin_iteration({"action": "call_tool"})
        .request_tool(
            name="summarize_document",
            version="1.1.0",
            arguments={"document_reference": "CLAUDE.md"},
            idempotency_key="summary-1",
            request_fingerprint="summary-1",
        )
        .start_tool()
        .observe(
            AgentLoopToolObservation(
                iteration=1,
                tool_name="summarize_document",
                tool_version="1.1.0",
                idempotency_key="summary-1",
                input_summary="sha256:input",
                output_summary="sha256:output",
                model_output={
                    "trust": "untrusted",
                    "status": "not_found",
                    "candidate_count": 0,
                    "hit_count": 0,
                    "matched_count": 0,
                    "context_only_count": 0,
                    "evidence_ids": [],
                    "source_versions": [],
                },
            )
        )
    )

    await finalizer.finalize(
        run=runtime,
        task=state.task,
        decision=LLMDecision(LLMDecisionAction.CLARIFY, reason="Need more information."),
        state=state,
        input_data={},
    )
    clarified = await repository.get_conversation_run(submitted.run_id)
    assert clarified is not None and clarified.result is not None
    assert clarified.result.clarification is not None
    assert "exact name" in clarified.result.clarification.message


async def _no_qa_result(_run_id: UUID) -> QARunRecord | None:
    return None
