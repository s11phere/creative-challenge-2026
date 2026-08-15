"""Synthetic tests for the v2 server-owned knowledge Tool path."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import cast
from uuid import UUID

import pytest
from agent_runtime import (
    InMemoryRuntimeStateStore,
    InMemoryToolRegistry,
    JSONValue,
    NativeSkillPin,
    NativeSkillRoute,
    NativeSkillSelection,
    NativeToolUseAgentLoopExecutor,
    NativeToolUseCall,
    NativeToolUseLoopState,
    NativeToolUseObservation,
    NodeExecutionError,
    ToolDefinition,
    ToolExecutionContext,
    ToolRef,
)
from agent_runtime.skills import PinnedSkill, SkillCompatibility
from application.qa.profile import QAPlanningProfileV1
from application.qa.service import (
    AgentRetrievalPlan,
    GroundedQAApplicationPort,
    GroundedQAExecutionProfile,
)
from application.skills import (
    NATIVE_KNOWLEDGE_AGENT_V2_INSTRUCTIONS,
    NativeKnowledgeTools,
    NativeKnowledgeToolsConfig,
)
from domain.agent_runtime import (
    AgentRun,
    AgentRunContext,
    RunBudget,
    RunStatus,
    ToolCallRecord,
    ToolPermission,
)
from domain.agent_sse import AGENT_RUN_SSE_V4, AgentRunEventLog, AgentRunEventType
from domain.grounded_qa import (
    Citation,
    Claim,
    ConflictNotice,
    GroundedAnswer,
    QAAttempt,
    QAOutcome,
    QAResult,
    QAStatus,
    Refusal,
    RefusalReason,
)
from domain.qa_persistence import QARetrievalScope, QARunRecord, QARunVersions
from domain.retrieval import (
    CandidateCounts,
    LocatorKind,
    RetrievalMode,
    RetrievalProfileV1,
    SearchDiagnostics,
    SearchHit,
    SearchHitSummary,
    SearchLocator,
    SearchRequest,
    SearchResult,
)
from model_gateway import (
    CapabilityAlias,
    ChatRequest,
    ChatResponse,
    ChatToolCall,
    FakeModelGateway,
    GatewayStatus,
    ModelGateway,
    ModelUsage,
)

RUN_ID = UUID("00000000-0000-4000-8000-000000000041")
SPACE_ID = UUID("00000000-0000-4000-8000-000000000042")
SOURCE_ID = UUID("00000000-0000-4000-8000-000000000043")
DOCUMENT_ID = UUID("00000000-0000-4000-8000-000000000044")
VERSION_ID = UUID("00000000-0000-4000-8000-000000000045")
CHUNK_ID = UUID("00000000-0000-4000-8000-000000000046")
EVIDENCE_ID = UUID("00000000-0000-4000-8000-000000000047")
OTHER_EVIDENCE_ID = UUID("00000000-0000-4000-8000-000000000048")
MESSAGE_ID = UUID("00000000-0000-4000-8000-000000000049")
PRIVATE_SOURCE_TEXT = "SYNTHETIC_PRIVATE_SOURCE_TEXT"


class SequenceNativeGateway:
    def __init__(self, *responses: ChatResponse) -> None:
        self._responses = list(responses)
        self.requests: list[ChatRequest] = []

    @property
    def status(self) -> GatewayStatus:
        return FakeModelGateway().status

    @property
    def remaining(self) -> int:
        return len(self._responses)

    async def chat(
        self,
        request: ChatRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.FAST_CHAT,
    ) -> ChatResponse:
        self.requests.append(request)
        assert capability is CapabilityAlias.FAST_CHAT
        if not self._responses:
            raise AssertionError("Synthetic gateway received an unexpected model turn")
        return self._responses.pop(0)


class SyntheticSkillCatalog:
    def __init__(self, *entries: NativeSkillSelection) -> None:
        self._entries = {entry.pin.name: entry for entry in entries}

    def list_routes(self) -> tuple[NativeSkillRoute, ...]:
        return tuple(entry.route for entry in self._entries.values())

    def select(self, name: str) -> NativeSkillSelection:
        selection = self._entries.get(name)
        if selection is None:
            raise ValueError("Synthetic Skill is unavailable")
        return selection

    def resolve(self, pin: NativeSkillPin) -> NativeSkillSelection:
        selection = self._entries.get(pin.name)
        if selection is None or selection.pin != pin:
            raise ValueError("Synthetic Skill pin is unavailable")
        return selection


class FakeSearchService:
    def __init__(self, *, matched: bool = True, no_hits: bool = False) -> None:
        self.matched = matched
        self.no_hits = no_hits
        self.calls: list[tuple[SearchRequest, RetrievalProfileV1]] = []

    async def search(self, request: SearchRequest, profile: RetrievalProfileV1) -> SearchResult:
        self.calls.append((request, profile))
        hit = SearchHit(
            chunk_id=CHUNK_ID,
            version_id=VERSION_ID,
            document_id=DOCUMENT_ID,
            source_id=SOURCE_ID,
            source_key="internal/fixture",
            text=PRIVATE_SOURCE_TEXT,
            chunk_hash="a" * 64,
            safe_summary=SearchHitSummary("a" * 64, 48, 1),
            locators=(SearchLocator(LocatorKind.LINES, 4, 6),),
            final_rank=1,
            context_only=not self.matched,
        )
        return SearchResult(
            hits=() if self.no_hits else (hit,),
            diagnostics=SearchDiagnostics(
                requested_mode=RetrievalMode.DENSE_RERANK,
                executed_mode=RetrievalMode.DENSE_RERANK,
                profile_version=profile.profile_version,
                embedding_version="fixture-embedding",
                reranker_version="fixture-reranker",
                keyword_index_version=None,
                dense_index_version="fixture-index",
                candidate_counts=CandidateCounts(final=1),
                stage_timings=(),
            ),
        )


class FakeGroundedQA:
    def __init__(
        self,
        outcome: QAOutcome = QAOutcome.ANSWER,
        *,
        invalid_citation: bool = False,
        fail: bool = False,
    ) -> None:
        self.outcome = outcome
        self.invalid_citation = invalid_citation
        self.fail = fail
        self.execute_calls: list[UUID] = []
        self.agent_plans: list[object] = []

    async def execute(
        self,
        run_id: UUID,
        *,
        profile: GroundedQAExecutionProfile,
        agent_plan: AgentRetrievalPlan | None = None,
    ) -> QARunRecord:
        assert profile == execution_profile()
        self.execute_calls.append(run_id)
        self.agent_plans.append(agent_plan)
        if self.fail:
            return QARunRecord(
                run_id=run_id,
                attempt=QAAttempt(run_id=run_id),
                conversation_id=UUID(int=21),
                question_message_id=UUID(int=22),
                space_id=SPACE_ID,
                caller_id="synthetic-user",
                idempotency_key=str(run_id),
                versions=versions(),
                status=QAStatus.FAILED,
                result=None,
                error_code="QA_MODEL_FAILED",
            )
        return qa_run(
            self.outcome,
            run_id=run_id,
            invalid_citation=self.invalid_citation,
        )


def _response(
    *,
    text: str = "",
    calls: tuple[ChatToolCall, ...] = (),
    finish_reason: str = "stop",
) -> ChatResponse:
    return ChatResponse(
        text=text,
        tool_calls=calls,
        finish_reason=finish_reason,
        usage=ModelUsage(input_tokens=3, output_tokens=2),
        capability=CapabilityAlias.FAST_CHAT,
        latency_ms=1.0,
    )


def _call(name: str, arguments: dict[str, object], call_id: str | None = None) -> ChatToolCall:
    return ChatToolCall(call_id or f"call-{name}", name, arguments)


def _run(
    *,
    permissions: frozenset[ToolPermission] = frozenset(
        {ToolPermission.READ_KNOWLEDGE, ToolPermission.MODEL}
    ),
) -> AgentRun:
    return AgentRun(
        context=AgentRunContext(
            run_id=RUN_ID,
            space_id=SPACE_ID,
            skill_name="knowledge_agent",
            skill_version="2.0.0",
            skill_content_sha256="b" * 64,
            trace_id="native-knowledge-test",
            caller_id="synthetic-user",
            granted_permissions=permissions,
        ),
        budget=RunBudget(
            max_steps=8,
            max_tool_calls=8,
            max_input_tokens=1_000,
            max_output_tokens=1_000,
            timeout_seconds=30,
        ),
    )


def _pin() -> PinnedSkill:
    return PinnedSkill(
        name="knowledge_agent",
        version="2.0.0",
        content_sha256="b" * 64,
        manifest_version="2",
        entrypoint_sha256="c" * 64,
        input_schema_sha256="d" * 64,
        output_schema_sha256="e" * 64,
        prompt_digests=(("prompts/system.md", "f" * 64),),
        compatibility=agent_runtime_compatibility(),
    )


def agent_runtime_compatibility() -> SkillCompatibility:
    return SkillCompatibility(runtime=">=0.1.0,<1.0.0", checkpoint_schema_versions=(1,))


def versions() -> QARunVersions:
    return QARunVersions(
        skill_name="knowledge_agent",
        skill_version="2.0.0",
        profile_version="grounded-qa-provisional-v1",
        retrieval_profile_version="retrieval-profile-v1",
        model_identity="fake-fast-chat-v1",
        prompt_version="native-knowledge-v2-provisional",
        output_schema_version="native-knowledge-tool-output-v1",
        corpus_version="v0-provisional",
        dataset_version="knowledge-qa-v0-provisional",
    )


def execution_profile() -> GroundedQAExecutionProfile:
    return GroundedQAExecutionProfile(
        planning=QAPlanningProfileV1(),
        retrieval=RetrievalProfileV1(
            profile_version="retrieval-profile-v1", embedding_version="fixture-embedding"
        ),
    )


def _adapter(
    search: FakeSearchService,
    qa: FakeGroundedQA,
    *,
    result_reader: Callable[[UUID], Awaitable[QARunRecord | None]] | None = None,
) -> NativeKnowledgeTools:
    return NativeKnowledgeTools(
        qa=cast(GroundedQAApplicationPort, qa),
        search=search,
        config=NativeKnowledgeToolsConfig(
            profile=execution_profile(),
            versions=versions(),
            retrieval_scope=QARetrievalScope(
                source_ids=frozenset({SOURCE_ID}),
                document_ids=frozenset({DOCUMENT_ID}),
                version_ids=frozenset({VERSION_ID}),
            ),
            result_reader=result_reader,
        ),
    )


def qa_run(
    outcome: QAOutcome,
    *,
    run_id: UUID,
    invalid_citation: bool = False,
) -> QARunRecord:
    citation = Citation(
        evidence_id=EVIDENCE_ID,
        space_id=SPACE_ID,
        source_id=SOURCE_ID,
        document_id=DOCUMENT_ID,
        version_id=VERSION_ID,
        chunk_id=CHUNK_ID,
        locator=SearchLocator(LocatorKind.LINES, 4, 6),
        excerpt_sha256="b" * 64,
    )
    if outcome is QAOutcome.ANSWER:
        evidence_id = OTHER_EVIDENCE_ID if invalid_citation else EVIDENCE_ID
        answer = GroundedAnswer(
            text="Synthetic authoritative QA answer.",
            claims=(Claim("claim-1", "Synthetic grounded claim.", (EVIDENCE_ID,)),),
            citations=(citation,),
        )
        if invalid_citation:
            object.__setattr__(
                answer,
                "claims",
                (Claim("claim-1", "Synthetic grounded claim.", (evidence_id,)),),
            )
        result = QAResult(
            outcome=QAOutcome.ANSWER,
            answer=answer,
        )
    elif outcome is QAOutcome.REFUSE:
        result = QAResult(
            outcome=QAOutcome.REFUSE,
            refusal=Refusal(RefusalReason.INSUFFICIENT_EVIDENCE, "Synthetic refusal."),
        )
    else:
        result = QAResult(
            outcome=QAOutcome.CONFLICT,
            conflict=ConflictNotice((EVIDENCE_ID, OTHER_EVIDENCE_ID), "Synthetic conflict."),
        )
    return QARunRecord(
        run_id=run_id,
        attempt=QAAttempt(run_id=run_id),
        conversation_id=UUID(int=21),
        question_message_id=UUID(int=22),
        space_id=SPACE_ID,
        caller_id="synthetic-user",
        idempotency_key=str(run_id),
        versions=versions(),
        status=QAStatus.REFUSED if outcome is QAOutcome.REFUSE else QAStatus.COMPLETED,
        result=result,
        answer_message_id=MESSAGE_ID,
    )


def _skill(tools: tuple[ToolRef, ...]) -> NativeSkillSelection:
    pin = NativeSkillPin(
        name="knowledge_agent",
        version="2.0.0",
        content_sha256="b" * 64,
    )
    return NativeSkillSelection(
        route=NativeSkillRoute(
            pin=pin,
            description="Synthetic native knowledge route.",
            command="ask",
            adapter_available=True,
        ),
        instructions=NATIVE_KNOWLEDGE_AGENT_V2_INSTRUCTIONS,
        allowed_tools=tools,
    )


def _retrieve_call(
    query: str = "architecture", call_id: str = "call-retrieve"
) -> NativeToolUseCall:
    return NativeToolUseCall(
        call_id=call_id, tool_name="knowledge_retrieve", arguments={"query": query}
    )


def _answer_call(call_id: str = "call-answer") -> NativeToolUseCall:
    return NativeToolUseCall(call_id=call_id, tool_name="knowledge_answer", arguments={})


def _state(goal: str = "Answer the synthetic knowledge question.") -> NativeToolUseLoopState:
    return NativeToolUseLoopState.accepted(goal)


@pytest.mark.asyncio
async def test_retrieve_merges_search_and_inspection_and_never_exposes_source_text() -> None:
    search = FakeSearchService()
    qa = FakeGroundedQA()
    adapter = _adapter(search, qa)
    call = _retrieve_call()

    retrieved = await adapter.execute(_run(), _state(), call, {})
    answered = await adapter.execute(retrieved.run, _state(), _answer_call(), {})
    assert answered.terminal_output is not None
    assert answered.publication_id is not None
    terminal_output = answered.terminal_output
    finalized = await adapter.finalize_server_terminal(
        run=retrieved.run,
        goal="Answer the synthetic knowledge question.",
        terminal_output=terminal_output,
        publication_id=answered.publication_id,
        input_data={},
    )

    assert len(search.calls) == 1
    assert retrieved.observation["recommended_next"] == "knowledge_answer"
    assert retrieved.observation["evidence_ids"] == [str(CHUNK_ID)]
    assert PRIVATE_SOURCE_TEXT not in json.dumps(retrieved.observation)
    assert terminal_output == {
        "status": "completed",
        "outcome": "answer",
        "qa_run_id": str(RUN_ID),
        "publication": "grounded_qa",
    }
    assert finalized == terminal_output
    assert PRIVATE_SOURCE_TEXT not in json.dumps(answered.observation)
    assert PRIVATE_SOURCE_TEXT not in json.dumps(terminal_output)
    assert qa.execute_calls == [RUN_ID]


@pytest.mark.asyncio
async def test_answer_before_retrieval_returns_needs_retrieval_without_qa() -> None:
    search = FakeSearchService()
    qa = FakeGroundedQA()
    adapter = _adapter(search, qa)

    result = await adapter.execute(_run(), _state(), _answer_call(), {})

    assert result.observation["status"] == "needs_retrieval"
    assert result.observation["terminal_reason"] == "retrieval_required"
    assert result.observation["recommended_next"] == "knowledge_retrieve"
    assert result.terminal_output is None
    assert qa.execute_calls == []


@pytest.mark.asyncio
async def test_unmatched_retrieval_requires_more_search_before_qa() -> None:
    search = FakeSearchService(matched=False)
    qa = FakeGroundedQA()
    adapter = _adapter(search, qa)
    run = _run()

    first = await adapter.execute(run, _state(), _retrieve_call(), {})
    blocked = await adapter.execute(run, _state(), _answer_call(), {})

    assert first.observation["recommended_next"] == "knowledge_retrieve"
    assert first.observation["gap_signals"] == ["no_matched_evidence"]
    assert blocked.observation["status"] == "needs_retrieval"
    assert blocked.terminal_output is None
    assert qa.execute_calls == []

    search.matched = True
    second = await adapter.execute(
        run, _state(), _retrieve_call("architecture follow-up", "call-retrieve-2"), {}
    )
    answered = await adapter.execute(run, _state(), _answer_call("call-answer-2"), {})

    assert second.observation["recommended_next"] == "knowledge_answer"
    assert answered.terminal_output is not None
    assert len(search.calls) == 2
    assert qa.execute_calls == [RUN_ID]


@pytest.mark.asyncio
async def test_zero_hit_retrieval_runs_grounded_qa_for_a_normal_refusal() -> None:
    search = FakeSearchService(no_hits=True)
    qa = FakeGroundedQA(QAOutcome.REFUSE)
    adapter = _adapter(search, qa)
    run = _run()

    retrieved = await adapter.execute(run, _state(), _retrieve_call(), {})
    answered = await adapter.execute(run, _state(), _answer_call(), {})

    assert retrieved.observation["hit_count"] == 0
    assert retrieved.observation["recommended_next"] == "knowledge_answer"
    assert answered.terminal_output is not None
    assert answered.observation["terminal_reason"] == "evidence_insufficient"
    assert qa.execute_calls == [RUN_ID]
    notice = adapter.user_notice(RUN_ID)
    assert notice is not None
    assert "上传相关文件" in notice


@pytest.mark.asyncio
async def test_workspace_delivery_notice_distinguishes_unavailable_workspace_from_denied_path() -> (
    None
):
    adapter = _adapter(FakeSearchService(), FakeGroundedQA())
    run = _run()
    await adapter.execute(run, _state(), _retrieve_call(), {})
    unavailable = await adapter.execute(
        run,
        _state(),
        _answer_call(),
        {"question": "Answer and save this as answer.md.", "workspace": {}},
    )

    assert unavailable.terminal_output is not None
    first_notice = adapter.user_notice(RUN_ID)
    assert first_notice is not None
    assert "尚未选择可写工作区" in first_notice

    adapter.note_workspace_delivery_blocked(RUN_ID, "TOOL_PATH_DENIED")
    denied_notice = adapter.user_notice(RUN_ID)
    assert denied_notice is not None
    assert "目标位置不可用或未获授权" in denied_notice
    assert "尚未选择可写工作区" not in denied_notice


def test_workspace_delivery_facts_are_restored_from_checkpoint_observations() -> None:
    adapter = _adapter(FakeSearchService(), FakeGroundedQA())
    state = NativeToolUseLoopState(
        goal="Answer and save the synthetic result.",
        iteration=2,
        observations=(
            NativeToolUseObservation(
                iteration=1,
                call=_answer_call(),
                observation={
                    "workspace_required": True,
                    "workspace_delivery": "unavailable",
                },
                input_summary="sha256:answer",
                output_summary="sha256:answer-output",
            ),
            NativeToolUseObservation(
                iteration=2,
                call=NativeToolUseCall(
                    call_id="call-write",
                    tool_name="fs_write",
                    arguments={"path": "answer.md", "content": "{{current_grounded_qa_answer}}"},
                ),
                observation={"status": "tool_error", "error_code": "TOOL_PATH_DENIED"},
                input_summary="sha256:write",
                output_summary="error:TOOL_PATH_DENIED",
            ),
        ),
    )

    adapter.restore_delivery_facts(RUN_ID, state)

    notice = adapter.user_notice(RUN_ID)
    assert notice is not None
    assert "目标位置不可用或未获授权" in notice


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "status", "terminal_reason"),
    [
        (QAOutcome.REFUSE, "refused", "evidence_insufficient"),
        (QAOutcome.CONFLICT, "completed", "conflict"),
    ],
)
async def test_refusal_and_conflict_are_verified_terminal_outcomes(
    outcome: QAOutcome, status: str, terminal_reason: str
) -> None:
    search = FakeSearchService()
    qa = FakeGroundedQA(outcome)
    adapter = _adapter(search, qa)
    run = _run()
    await adapter.execute(run, _state(), _retrieve_call(), {})

    answered = await adapter.execute(run, _state(), _answer_call(), {})

    assert answered.observation["status"] == status
    assert answered.observation["outcome"] == outcome.value
    assert answered.observation["terminal_reason"] == terminal_reason
    assert answered.terminal_output is not None
    terminal_output = answered.terminal_output
    assert terminal_output["outcome"] == outcome.value
    assert qa.execute_calls == [RUN_ID]


@pytest.mark.asyncio
async def test_invalid_citation_blocks_terminal_without_retrying_qa() -> None:
    search = FakeSearchService()
    qa = FakeGroundedQA(invalid_citation=True)
    adapter = _adapter(search, qa)
    run = _run()
    await adapter.execute(run, _state(), _retrieve_call(), {})

    answered = await adapter.execute(run, _state(), _answer_call(), {})

    assert answered.observation["status"] == "verification_failed"
    assert answered.observation["terminal_reason"] == "citation_incomplete"
    assert answered.observation["recommended_next"] == "knowledge_retrieve"
    assert answered.terminal_output is None
    assert qa.execute_calls == [RUN_ID]


@pytest.mark.asyncio
async def test_qa_dependency_failure_has_a_stable_run_error() -> None:
    search = FakeSearchService()
    qa = FakeGroundedQA(fail=True)
    adapter = _adapter(search, qa)
    run = _run()
    await adapter.execute(run, _state(), _retrieve_call(), {})

    with pytest.raises(NodeExecutionError) as raised:
        await adapter.execute(run, _state(), _answer_call(), {})

    assert raised.value.code == "DEPENDENCY_MODEL_FAILED"


@pytest.mark.asyncio
async def test_native_executor_finalizes_knowledge_answer_without_an_extra_model_turn() -> None:
    search = FakeSearchService()
    qa = FakeGroundedQA()
    adapter = _adapter(search, qa)
    selection = _skill(adapter.allowed_tools())
    gateway = SequenceNativeGateway(
        _response(
            calls=(_call("invoke_skill", {"name": "knowledge_agent"}, "call-1"),),
            finish_reason="tool_calls",
        ),
        _response(
            calls=(_call("knowledge_retrieve", {"query": "architecture"}, "call-2"),),
            finish_reason="tool_calls",
        ),
        _response(
            calls=(_call("knowledge_answer", {}, "call-3"),),
            finish_reason="tool_calls",
        ),
    )
    events = AgentRunEventLog()
    executor = NativeToolUseAgentLoopExecutor(
        tool_registry=adapter.tool_registry,
        allowed_tools=adapter.allowed_tools(),
        system_prompt="Native base prompt.",
        model_gateway=cast(ModelGateway, gateway),
        state_store=InMemoryRuntimeStateStore(),
        skill_catalog=SyntheticSkillCatalog(selection),
        server_tools=adapter,
        event_store=events,
    )

    result = await executor.execute(
        _run(),
        _pin(),
        {"question": "synthetic"},
        goal="Answer the synthetic knowledge question.",
    )

    assert result.run.status is RunStatus.COMPLETED, result.error
    assert result.output == {
        "status": "completed",
        "outcome": "answer",
        "qa_run_id": str(RUN_ID),
        "publication": "grounded_qa",
    }
    assert [item.call.tool_name for item in result.state.observations] == [
        "invoke_skill",
        "knowledge_retrieve",
        "knowledge_answer",
    ]
    assert len(gateway.requests) == 3
    assert gateway.remaining == 0
    assert gateway.requests[0].tools[0].name == "invoke_skill"
    assert [tool.name for tool in gateway.requests[1].tools] == [
        "invoke_skill",
        "knowledge_retrieve",
        "knowledge_answer",
    ]
    assert "knowledge_search" not in str(gateway.requests[1])
    assert "grounded_answer" not in str(gateway.requests[1])
    assert "knowledge_retrieve" in gateway.requests[1].messages[0].content
    assert "knowledge_answer" in gateway.requests[1].messages[0].content
    assert qa.execute_calls == [RUN_ID]
    assert result.run.usage.tool_calls == 3
    page = await events.page(RUN_ID, limit=100)
    assert [event.schema_version for event in page.events] == [AGENT_RUN_SSE_V4] * len(page.events)
    assert {event.event_type for event in page.events} == {
        AgentRunEventType.ACCEPTED,
        AgentRunEventType.SKILL_ACTIVATED,
        AgentRunEventType.TOOL_STARTED,
        AgentRunEventType.TOOL_OUTPUT,
        AgentRunEventType.CACHE_USED,
        AgentRunEventType.COMPLETED,
    }
    assert all(
        key not in event.payload
        for event in page.events
        for key in ("prompt", "answer", "content", "raw", "secret")
    )


@pytest.mark.asyncio
async def test_native_executor_completes_a_workspace_request_without_workspace_tools() -> None:
    search = FakeSearchService()
    qa = FakeGroundedQA()
    adapter = _adapter(search, qa)
    gateway = SequenceNativeGateway(
        _response(
            calls=(_call("invoke_skill", {"name": "knowledge_agent"}, "call-1"),),
            finish_reason="tool_calls",
        ),
        _response(
            calls=(_call("knowledge_retrieve", {"query": "Omnistudio"}, "call-2"),),
            finish_reason="tool_calls",
        ),
        _response(
            calls=(_call("knowledge_answer", {}, "call-3"),),
            finish_reason="tool_calls",
        ),
    )
    executor = NativeToolUseAgentLoopExecutor(
        tool_registry=adapter.tool_registry,
        allowed_tools=adapter.allowed_tools(),
        system_prompt="Native base prompt.",
        model_gateway=cast(ModelGateway, gateway),
        skill_catalog=SyntheticSkillCatalog(_skill(adapter.allowed_tools())),
        server_tools=adapter,
    )

    result = await executor.execute(
        _run(),
        _pin(),
        {
            "question": "Introduce Omnistudio's main modules and save the answer as Markdown.",
            "workspace": {"selected": False, "tools_enabled": False},
        },
        goal="Introduce Omnistudio's main modules and save the answer as Markdown.",
    )

    assert result.run.status is RunStatus.COMPLETED, result.error
    assert result.error is None
    assert result.state.observations[-1].observation["workspace_delivery"] == "unavailable"
    assert adapter.user_notice(RUN_ID) is not None
    assert gateway.remaining == 0


@pytest.mark.asyncio
async def test_native_executor_publishes_zero_hit_knowledge_refusal() -> None:
    search = FakeSearchService(no_hits=True)
    qa = FakeGroundedQA(QAOutcome.REFUSE)
    adapter = _adapter(search, qa)
    gateway = SequenceNativeGateway(
        _response(
            calls=(_call("invoke_skill", {"name": "knowledge_agent"}, "call-1"),),
            finish_reason="tool_calls",
        ),
        _response(
            calls=(_call("knowledge_retrieve", {"query": "unavailable topic"}, "call-2"),),
            finish_reason="tool_calls",
        ),
        _response(
            calls=(_call("knowledge_answer", {}, "call-3"),),
            finish_reason="tool_calls",
        ),
    )
    events = AgentRunEventLog()

    result = await NativeToolUseAgentLoopExecutor(
        tool_registry=adapter.tool_registry,
        allowed_tools=adapter.allowed_tools(),
        system_prompt="Native base prompt.",
        model_gateway=cast(ModelGateway, gateway),
        skill_catalog=SyntheticSkillCatalog(_skill(adapter.allowed_tools())),
        server_tools=adapter,
        event_store=events,
    ).execute(
        _run(),
        _pin(),
        {"question": "Ask about a topic without uploaded documents."},
        goal="Answer the synthetic knowledge question.",
    )

    assert result.run.status is RunStatus.COMPLETED, result.error
    assert result.error is None
    assert result.output == {
        "status": "refused",
        "outcome": "refuse",
        "qa_run_id": str(RUN_ID),
        "publication": "grounded_qa",
    }
    assert result.state.observations[-1].observation["outcome"] == "refuse"
    assert result.state.observations[-1].observation["terminal_reason"] == "evidence_insufficient"
    assert qa.execute_calls == [RUN_ID]
    assert gateway.remaining == 0
    page = await events.page(RUN_ID, limit=100)
    assert page.events[-1].event_type is AgentRunEventType.REFUSED
    assert page.events[-1].payload["stop_reason"] == "evidence_insufficient"


@pytest.mark.asyncio
async def test_direct_terminal_after_knowledge_skill_selection_is_denied() -> None:
    search = FakeSearchService()
    qa = FakeGroundedQA()
    adapter = _adapter(search, qa)
    gateway = SequenceNativeGateway(
        _response(
            calls=(_call("invoke_skill", {"name": "knowledge_agent"}, "call-1"),),
            finish_reason="tool_calls",
        ),
        _response(text="Synthetic direct answer."),
    )

    result = await NativeToolUseAgentLoopExecutor(
        tool_registry=adapter.tool_registry,
        allowed_tools=adapter.allowed_tools(),
        system_prompt="Native base prompt.",
        model_gateway=cast(ModelGateway, gateway),
        skill_catalog=SyntheticSkillCatalog(_skill(adapter.allowed_tools())),
        server_tools=adapter,
    ).execute(
        _run(),
        _pin(),
        {"question": "synthetic"},
        goal="Answer the synthetic knowledge question.",
    )

    assert result.run.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.code == "RUN_NATIVE_TOOL_USE_KNOWLEDGE_TERMINAL_DENIED"
    assert qa.execute_calls == []


@pytest.mark.asyncio
async def test_knowledge_tool_without_granted_permission_is_denied() -> None:
    search = FakeSearchService()
    qa = FakeGroundedQA()
    adapter = _adapter(search, qa)
    gateway = SequenceNativeGateway(
        _response(
            calls=(_call("invoke_skill", {"name": "knowledge_agent"}, "call-1"),),
            finish_reason="tool_calls",
        ),
        _response(
            calls=(_call("knowledge_retrieve", {"query": "architecture"}, "call-2"),),
            finish_reason="tool_calls",
        ),
    )

    result = await NativeToolUseAgentLoopExecutor(
        tool_registry=adapter.tool_registry,
        allowed_tools=adapter.allowed_tools(),
        system_prompt="Native base prompt.",
        model_gateway=cast(ModelGateway, gateway),
        skill_catalog=SyntheticSkillCatalog(_skill(adapter.allowed_tools())),
        server_tools=adapter,
    ).execute(
        _run(permissions=frozenset({ToolPermission.MODEL})),
        _pin(),
        {"question": "synthetic"},
        goal="Answer the synthetic knowledge question.",
    )

    assert result.run.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.code == "RUN_NATIVE_TOOL_USE_TOOL_DENIED"
    assert search.calls == []


@pytest.mark.asyncio
async def test_server_tool_input_failure_is_observed_then_the_knowledge_run_recovers() -> None:
    search = FakeSearchService()
    qa = FakeGroundedQA()
    adapter = _adapter(search, qa)
    events = AgentRunEventLog()
    gateway = SequenceNativeGateway(
        _response(
            calls=(_call("invoke_skill", {"name": "knowledge_agent"}, "call-1"),),
            finish_reason="tool_calls",
        ),
        _response(
            calls=(_call("knowledge_retrieve", {}, "call-2"),),
            finish_reason="tool_calls",
        ),
        _response(
            calls=(_call("knowledge_retrieve", {"query": "architecture"}, "call-3"),),
            finish_reason="tool_calls",
        ),
        _response(
            calls=(_call("knowledge_answer", {}, "call-4"),),
            finish_reason="tool_calls",
        ),
    )

    result = await NativeToolUseAgentLoopExecutor(
        tool_registry=adapter.tool_registry,
        allowed_tools=adapter.allowed_tools(),
        system_prompt="Native base prompt.",
        model_gateway=cast(ModelGateway, gateway),
        skill_catalog=SyntheticSkillCatalog(_skill(adapter.allowed_tools())),
        server_tools=adapter,
        event_store=events,
    ).execute(
        _run(),
        _pin(),
        {"question": "synthetic"},
        goal="Answer the synthetic knowledge question.",
    )

    assert result.run.status is RunStatus.COMPLETED
    assert result.error is None
    assert result.state.observations[1].observation == {
        "status": "tool_error",
        "error_code": "SKILL_INPUT_INVALID",
    }
    assert gateway.requests[2].tool_results[-1].observation["status"] == "failed"
    context = json.loads(gateway.requests[2].messages[-1].content)["model_context"]
    assert context["observations"][-1]["error_code"] == "SKILL_INPUT_INVALID"
    history = await events.page(RUN_ID, limit=20)
    failed_output = next(
        event
        for event in history.events
        if event.event_type is AgentRunEventType.TOOL_OUTPUT and event.payload["status"] == "failed"
    )
    assert failed_output.payload["tool_name"] == "knowledge_retrieve"
    assert qa.execute_calls == [RUN_ID]


@pytest.mark.asyncio
async def test_fresh_coordinator_restores_retrieval_facts_from_checkpoint_observations() -> None:
    first_search = FakeSearchService()
    first_qa = FakeGroundedQA()
    first_adapter = _adapter(first_search, first_qa)
    retrieved = await first_adapter.execute(_run(), _state(), _retrieve_call(), {})
    observation = NativeToolUseObservation(
        iteration=1,
        call=_retrieve_call(),
        observation=retrieved.observation,
        input_summary="sha256:input",
        output_summary="sha256:output",
    )
    state = NativeToolUseLoopState(
        goal="Answer the synthetic knowledge question.",
        iteration=1,
        observations=(observation,),
    )
    fresh_search = FakeSearchService()
    fresh_qa = FakeGroundedQA()
    fresh_adapter = _adapter(fresh_search, fresh_qa)

    answered = await fresh_adapter.execute(_run(), state, _answer_call(), {})

    assert answered.terminal_output is not None
    assert fresh_search.calls == []
    assert fresh_qa.execute_calls == [RUN_ID]


@pytest.mark.asyncio
async def test_workspace_tool_does_not_unlock_a_direct_knowledge_terminal() -> None:
    write_calls = 0

    async def write_handler(
        _arguments: dict[str, JSONValue], _context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        nonlocal write_calls
        write_calls += 1
        return {"status": "ok"}

    class AlwaysApprovedPort:
        async def request(self, context: AgentRunContext, tool: ToolCallRecord) -> str:
            del context, tool
            return "approval-1"

        async def is_approved(self, approval_id: str, context: AgentRunContext) -> bool:
            del approval_id, context
            return True

        async def is_always_allowed(
            self, context: AgentRunContext, tool_name: str, tool_version: str
        ) -> bool:
            del context, tool_name, tool_version
            return True

    search = FakeSearchService()
    qa = FakeGroundedQA()
    adapter = _adapter(search, qa)
    combined = InMemoryToolRegistry(
        handlers={
            "knowledge_retrieve": adapter._retrieve_handler,
            "knowledge_answer": adapter._answer_handler,
            "fs_write": write_handler,
        },
        approval_port=AlwaysApprovedPort(),
    )
    combined.register(adapter.retrieve_tool)
    combined.register(adapter.answer_tool)
    write_tool = combined.register(
        ToolDefinition(
            name="fs_write",
            version="1.0.0",
            description="Write a synthetic workspace artifact.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "content"],
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                },
            },
            output_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["status"],
                "properties": {"status": {"type": "string"}},
            },
            permissions=frozenset({ToolPermission.WRITE_KNOWLEDGE}),
            handler_name="fs_write",
            model_visible=True,
        )
    )
    allowed_tools = (*adapter.allowed_tools(), write_tool.ref)
    gateway = SequenceNativeGateway(
        _response(
            calls=(_call("invoke_skill", {"name": "knowledge_agent"}, "call-1"),),
            finish_reason="tool_calls",
        ),
        _response(
            calls=(_call("knowledge_retrieve", {"query": "architecture"}, "call-2"),),
            finish_reason="tool_calls",
        ),
        _response(
            calls=(
                _call(
                    "fs_write",
                    {"path": "answer.md", "content": "{{current_grounded_qa_answer}}"},
                    "call-3",
                ),
            ),
            finish_reason="tool_calls",
        ),
        _response(text="Synthetic direct answer."),
    )

    result = await NativeToolUseAgentLoopExecutor(
        tool_registry=combined,
        allowed_tools=allowed_tools,
        system_prompt="Native base prompt.",
        model_gateway=cast(ModelGateway, gateway),
        skill_catalog=SyntheticSkillCatalog(_skill(allowed_tools)),
        server_tools=adapter,
        approval_port=AlwaysApprovedPort(),
    ).execute(
        _run(
            permissions=frozenset(
                {
                    ToolPermission.READ_KNOWLEDGE,
                    ToolPermission.MODEL,
                    ToolPermission.WRITE_KNOWLEDGE,
                }
            )
        ),
        _pin(),
        {"question": "synthetic"},
        goal="Answer and save a synthetic workspace artifact.",
    )

    assert write_calls == 0
    assert result.run.status is RunStatus.FAILED
    assert result.error is not None
    assert result.error.code == "RUN_KNOWLEDGE_WORKSPACE_QA_REQUIRED"
    assert qa.execute_calls == []


@pytest.mark.asyncio
async def test_workspace_write_after_knowledge_answer_is_resolved_and_finalized() -> None:
    write_calls: list[dict[str, JSONValue]] = []
    list_calls = 0

    async def write_handler(
        arguments: dict[str, JSONValue], _context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        write_calls.append(arguments)
        return {"status": "ok"}

    async def list_handler(
        _arguments: dict[str, JSONValue], _context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        nonlocal list_calls
        list_calls += 1
        return {"status": "ok"}

    class AlwaysApprovedPort:
        async def request(self, context: AgentRunContext, tool: ToolCallRecord) -> str:
            del context, tool
            return "approval-1"

        async def is_approved(self, approval_id: str, context: AgentRunContext) -> bool:
            del approval_id, context
            return True

        async def is_always_allowed(
            self, context: AgentRunContext, tool_name: str, tool_version: str
        ) -> bool:
            del context, tool_name, tool_version
            return True

    search = FakeSearchService()
    qa = FakeGroundedQA()
    adapter = _adapter(search, qa)
    combined = InMemoryToolRegistry(
        handlers={
            "knowledge_retrieve": adapter._retrieve_handler,
            "knowledge_answer": adapter._answer_handler,
            "fs_list": list_handler,
            "fs_write": write_handler,
        },
        approval_port=AlwaysApprovedPort(),
    )
    combined.register(adapter.retrieve_tool)
    combined.register(adapter.answer_tool)
    list_tool = combined.register(
        ToolDefinition(
            name="fs_list",
            version="1.0.0",
            description="List workspace files.",
            input_schema={"type": "object", "properties": {}},
            output_schema={
                "type": "object",
                "required": ["status"],
                "properties": {"status": {"type": "string"}},
            },
            permissions=frozenset({ToolPermission.READ_KNOWLEDGE}),
            handler_name="fs_list",
            model_visible=True,
        )
    )
    write_tool = combined.register(
        ToolDefinition(
            name="fs_write",
            version="1.0.0",
            description="Write a workspace artifact.",
            input_schema={
                "type": "object",
                "required": ["path", "content"],
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
            },
            output_schema={
                "type": "object",
                "required": ["status"],
                "properties": {"status": {"type": "string"}},
            },
            permissions=frozenset({ToolPermission.WRITE_KNOWLEDGE}),
            handler_name="fs_write",
            model_visible=True,
        )
    )
    allowed_tools = (*adapter.allowed_tools(), list_tool.ref, write_tool.ref)
    gateway = SequenceNativeGateway(
        _response(
            calls=(_call("invoke_skill", {"name": "knowledge_agent"}, "call-1"),),
            finish_reason="tool_calls",
        ),
        _response(
            calls=(_call("knowledge_retrieve", {"query": "architecture"}, "call-2"),),
            finish_reason="tool_calls",
        ),
        _response(
            calls=(_call("knowledge_answer", {}, "call-3"),),
            finish_reason="tool_calls",
        ),
        _response(
            calls=(_call("fs_list", {"path": "."}, "call-4"),),
            finish_reason="tool_calls",
        ),
        _response(
            calls=(
                _call(
                    "fs_write",
                    {
                        "path": "answer.md",
                        "content": "{{current_grounded_qa_answer}}",
                    },
                    "call-5",
                ),
            ),
            finish_reason="tool_calls",
        ),
    )

    result = await NativeToolUseAgentLoopExecutor(
        tool_registry=combined,
        allowed_tools=allowed_tools,
        system_prompt="Native base prompt.",
        model_gateway=cast(ModelGateway, gateway),
        skill_catalog=SyntheticSkillCatalog(_skill(allowed_tools)),
        server_tools=adapter,
        approval_port=AlwaysApprovedPort(),
    ).execute(
        _run(
            permissions=frozenset(
                {
                    ToolPermission.READ_KNOWLEDGE,
                    ToolPermission.MODEL,
                    ToolPermission.WRITE_KNOWLEDGE,
                }
            )
        ),
        _pin(),
        {
            "question": "Answer and save as md file.",
            "conversation": "Answer and save as md file.",
            "workspace": {"selected": True, "tools_enabled": True},
        },
        goal="Answer and save a synthetic workspace artifact.",
    )

    assert result.run.status is RunStatus.COMPLETED
    assert result.error is None
    assert list_calls == 1
    assert write_calls[0]["content"] == "Synthetic authoritative QA answer."
    assert qa.execute_calls == [RUN_ID]
    assert gateway.remaining == 0


@pytest.mark.asyncio
async def test_workspace_marker_write_restores_qa_facts_before_durable_approval() -> None:
    write_calls: list[dict[str, JSONValue]] = []
    completed = qa_run(QAOutcome.ANSWER, run_id=RUN_ID)

    async def list_handler(
        _arguments: dict[str, JSONValue], _context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        return {"status": "ok"}

    async def write_handler(
        arguments: dict[str, JSONValue], _context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        write_calls.append(arguments)
        return {"status": "ok"}

    class ApprovalPort:
        def __init__(self) -> None:
            self.approved = False
            self.requests: list[ToolCallRecord] = []

        async def request(self, context: AgentRunContext, tool: ToolCallRecord) -> str:
            del context
            self.requests.append(tool)
            return "approval-1"

        async def is_approved(self, approval_id: str, _context: AgentRunContext) -> bool:
            return self.approved and approval_id == "approval-1"

        async def is_always_allowed(
            self, context: AgentRunContext, tool_name: str, tool_version: str
        ) -> bool:
            del context, tool_name, tool_version
            return False

    approvals = ApprovalPort()
    first_search = FakeSearchService()
    first_qa = FakeGroundedQA()
    first_adapter = _adapter(first_search, first_qa)
    combined = InMemoryToolRegistry(
        handlers={
            "knowledge_retrieve": first_adapter._retrieve_handler,
            "knowledge_answer": first_adapter._answer_handler,
            "fs_list": list_handler,
            "fs_write": write_handler,
        },
        approval_port=approvals,
    )
    combined.register(first_adapter.retrieve_tool)
    combined.register(first_adapter.answer_tool)
    list_tool = combined.register(
        ToolDefinition(
            name="fs_list",
            version="1.0.0",
            description="List workspace files.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["path"],
                "properties": {"path": {"type": "string", "minLength": 1}},
            },
            output_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["status"],
                "properties": {"status": {"type": "string"}},
            },
            permissions=frozenset({ToolPermission.READ_KNOWLEDGE}),
            handler_name="fs_list",
            model_visible=True,
        )
    )
    write_tool = combined.register(
        ToolDefinition(
            name="fs_write",
            version="1.0.0",
            description="Write a workspace artifact.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "content"],
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "content": {"type": "string"},
                },
            },
            output_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["status"],
                "properties": {"status": {"type": "string"}},
            },
            permissions=frozenset({ToolPermission.WRITE_KNOWLEDGE}),
            handler_name="fs_write",
            model_visible=True,
        )
    )
    allowed_tools = (*first_adapter.allowed_tools(), list_tool.ref, write_tool.ref)
    state_store = InMemoryRuntimeStateStore()
    initial = await NativeToolUseAgentLoopExecutor(
        tool_registry=combined,
        allowed_tools=allowed_tools,
        system_prompt="Native base prompt.",
        model_gateway=cast(
            ModelGateway,
            SequenceNativeGateway(
                _response(
                    calls=(_call("invoke_skill", {"name": "knowledge_agent"}, "call-1"),),
                    finish_reason="tool_calls",
                ),
                _response(
                    calls=(_call("knowledge_retrieve", {"query": "architecture"}, "call-2"),),
                    finish_reason="tool_calls",
                ),
                _response(
                    calls=(_call("knowledge_answer", {}, "call-3"),),
                    finish_reason="tool_calls",
                ),
                _response(
                    calls=(_call("fs_list", {"path": "."}, "call-4"),),
                    finish_reason="tool_calls",
                ),
                _response(
                    calls=(
                        _call(
                            "fs_write",
                            {"path": "answer.md", "content": "{{current_grounded_qa_answer}}"},
                            "call-5",
                        ),
                    ),
                    finish_reason="tool_calls",
                ),
            ),
        ),
        state_store=state_store,
        skill_catalog=SyntheticSkillCatalog(_skill(allowed_tools)),
        server_tools=first_adapter,
        approval_port=approvals,
    ).execute(
        _run(
            permissions=frozenset(
                {
                    ToolPermission.READ_KNOWLEDGE,
                    ToolPermission.MODEL,
                    ToolPermission.WRITE_KNOWLEDGE,
                }
            )
        ),
        _pin(),
        {
            "question": "Answer and save as md file.",
            "conversation": "Answer and save as md file.",
            "workspace": {"selected": True, "tools_enabled": True},
        },
        goal="Answer and save a synthetic workspace artifact.",
    )

    assert initial.waiting_approval
    assert write_calls == []
    assert first_qa.execute_calls == [RUN_ID]
    checkpoint = await state_store.get_latest(RUN_ID)
    assert checkpoint is not None
    assert checkpoint.approval_id is None

    async def result_reader(run_id: UUID) -> QARunRecord | None:
        return completed if run_id == RUN_ID else None

    fresh_qa = FakeGroundedQA(fail=True)
    fresh_adapter = _adapter(FakeSearchService(), fresh_qa, result_reader=result_reader)
    recovered_executor = NativeToolUseAgentLoopExecutor(
        tool_registry=combined,
        allowed_tools=allowed_tools,
        system_prompt="Native base prompt.",
        model_gateway=cast(ModelGateway, SequenceNativeGateway()),
        state_store=state_store,
        skill_catalog=SyntheticSkillCatalog(_skill(allowed_tools)),
        server_tools=fresh_adapter,
        approval_request=approvals.request,
        approval_port=approvals,
    )
    recovered = await recovered_executor.resume(
        initial.run,
        _pin(),
        checkpoint,
        {
            "question": "Answer and save as md file.",
            "conversation": "Answer and save as md file.",
            "workspace": {"selected": True, "tools_enabled": True},
        },
        caller_id=initial.run.context.caller_id,
        space_id=initial.run.context.space_id,
    )

    assert recovered.waiting_approval
    assert write_calls == []
    assert [record.tool_name for record in approvals.requests] == ["fs_write"]
    recovered_checkpoint = await state_store.get_latest(RUN_ID)
    assert recovered_checkpoint is not None
    assert recovered_checkpoint.approval_id == "approval-1"

    approvals.approved = True
    resumed = await recovered_executor.resume(
        recovered.run,
        _pin(),
        recovered_checkpoint,
        {
            "question": "Answer and save as md file.",
            "conversation": "Answer and save as md file.",
            "workspace": {"selected": True, "tools_enabled": True},
        },
        caller_id=recovered.run.context.caller_id,
        space_id=recovered.run.context.space_id,
        approval_id="approval-1",
    )

    assert resumed.run.status is RunStatus.COMPLETED
    assert resumed.error is None
    assert write_calls == [{"path": "answer.md", "content": "Synthetic authoritative QA answer."}]
    assert fresh_qa.execute_calls == []
