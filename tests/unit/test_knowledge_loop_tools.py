from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast
from uuid import UUID

import pytest
from agent_runtime import AgentLoopExecutor, LLMDecision, LLMDecisionAction, ToolExecutionContext
from agent_runtime.skills import PinnedSkill
from application.qa.profile import QAPlanningProfileV1
from application.qa.service import GroundedQAApplicationPort, GroundedQAExecutionProfile
from application.skills import KnowledgeLoopTools, KnowledgeLoopToolsConfig
from domain.agent_loop import AgentLoopState, AgentLoopTask, AgentLoopToolObservation
from domain.agent_runtime import AgentRun, AgentRunContext, RunBudget, ToolPermission
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
    FakeModelGateway,
    GatewayStatus,
    ModelGateway,
    ModelUsage,
)

RUN_ID = UUID("00000000-0000-4000-8000-000000000011")
SPACE_ID = UUID("00000000-0000-4000-8000-000000000012")
SOURCE_ID = UUID("00000000-0000-4000-8000-000000000013")
DOCUMENT_ID = UUID("00000000-0000-4000-8000-000000000014")
VERSION_ID = UUID("00000000-0000-4000-8000-000000000015")
CHUNK_ID = UUID("00000000-0000-4000-8000-000000000016")
EVIDENCE_ID = UUID("00000000-0000-4000-8000-000000000017")
MESSAGE_ID = UUID("00000000-0000-4000-8000-000000000018")


class DecisionGateway:
    def __init__(self, *responses: str) -> None:
        self._delegate = FakeModelGateway()
        self._responses = list(responses)

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
            text=self._responses.pop(0),
            finish_reason="stop",
            usage=ModelUsage(input_tokens=3, output_tokens=2),
            capability=capability,
            latency_ms=0.0,
        )


class FakeSearchService:
    def __init__(self) -> None:
        self.calls: list[tuple[SearchRequest, RetrievalProfileV1]] = []

    async def search(self, request: SearchRequest, profile: RetrievalProfileV1) -> SearchResult:
        self.calls.append((request, profile))
        hit = SearchHit(
            chunk_id=CHUNK_ID,
            version_id=VERSION_ID,
            document_id=DOCUMENT_ID,
            source_id=SOURCE_ID,
            source_key="internal/fixture",
            text="private source text must not leave the QA path",
            chunk_hash="a" * 64,
            safe_summary=SearchHitSummary("a" * 64, 48, 1),
            locators=(SearchLocator(LocatorKind.LINES, 4, 6),),
            final_rank=1,
        )
        return SearchResult(
            hits=(hit,),
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
    def __init__(self, outcome: QAOutcome = QAOutcome.ANSWER) -> None:
        self.outcome = outcome
        self.execute_calls: list[UUID] = []
        self.agent_plans: list[object] = []

    async def execute(
        self,
        run_id: UUID,
        *,
        profile: GroundedQAExecutionProfile,
        agent_plan: object | None = None,
    ) -> QARunRecord:
        assert profile == execution_profile()
        self.execute_calls.append(run_id)
        self.agent_plans.append(agent_plan)
        return qa_run(self.outcome, run_id=run_id)


@dataclass
class FakeResolvedResource:
    scope: QARetrievalScope


class FakeResourceResolver:
    def __init__(self, *, conflict: bool = False) -> None:
        self.conflict = conflict
        self.calls: list[tuple[UUID, str, str]] = []

    async def resolve(self, *, space_id: UUID, resource_type: str, reference: str) -> object:
        self.calls.append((space_id, resource_type, reference))
        if self.conflict:
            from application.assistant.resources import (
                ResourceResolutionError,
                ResourceResolutionErrorCode,
            )
            from domain.conversation_run import ResourceCandidate

            raise ResourceResolutionError(
                ResourceResolutionErrorCode.CONFLICT,
                "ambiguous",
                (ResourceCandidate("candidate:one", "document", "CLAUDE.md"),),
            )
        return FakeResolvedResource(
            QARetrievalScope(
                source_ids=frozenset({SOURCE_ID}),
                document_ids=frozenset({DOCUMENT_ID}),
                version_ids=frozenset({VERSION_ID}),
            )
        )


def versions(*, skill_version: str = "0.5.0") -> QARunVersions:
    return QARunVersions(
        skill_name="knowledge_agent",
        skill_version=skill_version,
        profile_version="grounded-qa-provisional-v1",
        retrieval_profile_version="retrieval-profile-v1",
        model_identity="fake-fast-chat-v1",
        prompt_version="grounded-qa-v1-provisional",
        output_schema_version="knowledge-loop-skill-output-v1",
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


def runtime_run(*, max_tool_calls: int = 5) -> AgentRun:
    return AgentRun(
        context=AgentRunContext(
            run_id=RUN_ID,
            space_id=SPACE_ID,
            skill_name="knowledge_agent",
            skill_version="0.5.0",
            skill_content_sha256="a" * 64,
            trace_id="knowledge-loop-test",
            caller_id="synthetic-user",
            granted_permissions=frozenset({ToolPermission.READ_KNOWLEDGE, ToolPermission.MODEL}),
        ),
        budget=RunBudget(
            max_steps=8,
            max_tool_calls=max_tool_calls,
            max_input_tokens=100,
            max_output_tokens=100,
            timeout_seconds=30,
        ),
    )


def tool_context() -> ToolExecutionContext:
    return ToolExecutionContext(run=runtime_run().context, idempotency_key="knowledge-loop-test")


def tools(
    outcome: QAOutcome = QAOutcome.ANSWER,
    *,
    skill_version: str = "0.5.0",
) -> tuple[KnowledgeLoopTools, FakeSearchService, FakeGroundedQA]:
    search = FakeSearchService()
    qa = FakeGroundedQA(outcome)
    return (
        KnowledgeLoopTools(
            qa=cast(GroundedQAApplicationPort, qa),
            search=search,
            config=KnowledgeLoopToolsConfig(
                profile=execution_profile(),
                versions=versions(skill_version=skill_version),
                retrieval_scope=QARetrievalScope(
                    source_ids=frozenset({SOURCE_ID}),
                    document_ids=frozenset({DOCUMENT_ID}),
                    version_ids=frozenset({VERSION_ID}),
                ),
            ),
        ),
        search,
        qa,
    )


def qa_run(outcome: QAOutcome, *, run_id: UUID) -> QARunRecord:
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
    result = (
        QAResult(
            outcome=QAOutcome.ANSWER,
            answer=GroundedAnswer(
                text="Authoritative QA answer remains private to this Tool output.",
                claims=(Claim("claim-1", "Grounded claim", (EVIDENCE_ID,)),),
                citations=(citation,),
            ),
        )
        if outcome is QAOutcome.ANSWER
        else QAResult(
            outcome=QAOutcome.REFUSE,
            refusal=Refusal(RefusalReason.INSUFFICIENT_EVIDENCE, "Synthetic refusal."),
        )
        if outcome is QAOutcome.REFUSE
        else QAResult(
            outcome=QAOutcome.CONFLICT,
            conflict=ConflictNotice((EVIDENCE_ID, UUID(int=19)), "Synthetic conflict."),
        )
    )
    return QARunRecord(
        run_id=run_id,
        attempt=QAAttempt(run_id=run_id, attempt_id=UUID(int=20)),
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


@pytest.mark.asyncio
async def test_knowledge_search_uses_only_search_service_and_hides_source_text() -> None:
    adapter, search, _qa = tools()

    output = await adapter.knowledge_search({"query": "current architecture"}, tool_context())

    assert len(search.calls) == 1
    request, profile = search.calls[0]
    assert request.space_id == SPACE_ID
    assert request.filters.source_ids == frozenset({SOURCE_ID})
    assert request.filters.document_ids == frozenset({DOCUMENT_ID})
    assert request.filters.version_ids == frozenset({VERSION_ID})
    assert profile == execution_profile().retrieval
    assert output["evidence_ids"] == [str(CHUNK_ID)]
    assert "private source text" not in json.dumps(output)
    inspected = await adapter.knowledge_inspect({}, tool_context())
    assert inspected["evidence_ids"] == [str(CHUNK_ID)]
    assert inspected["source_versions"] == [
        {
            "source_id": str(SOURCE_ID),
            "document_id": str(DOCUMENT_ID),
            "version_id": str(VERSION_ID),
        }
    ]
    assert "private source text" not in json.dumps(inspected)


@pytest.mark.asyncio
async def test_document_summary_resolves_and_searches_a_fixed_published_scope() -> None:
    resolver = FakeResourceResolver()
    search = FakeSearchService()
    qa = FakeGroundedQA()
    adapter = KnowledgeLoopTools(
        qa=cast(GroundedQAApplicationPort, qa),
        search=search,
        config=KnowledgeLoopToolsConfig(
            profile=execution_profile(),
            versions=versions(skill_version="0.7.0"),
            tool_version="1.1.0",
            resource_resolver=resolver,
        ),
    )

    output = await adapter.summarize_document(
        {"document_reference": "CLAUDE.md", "focus": "build and run"}, tool_context()
    )

    assert resolver.calls == [(SPACE_ID, "document", "CLAUDE.md")]
    assert search.calls[0][0].filters.document_ids == frozenset({DOCUMENT_ID})
    assert search.calls[0][0].filters.version_ids == frozenset({VERSION_ID})
    assert output["status"] == "resolved"
    assert output["recommended_next"] == "knowledge_inspect"
    assert "private source text" not in json.dumps(output)


@pytest.mark.asyncio
async def test_document_summary_reports_ambiguous_resource_for_actionable_clarification() -> None:
    resolver = FakeResourceResolver(conflict=True)
    adapter, _search, _qa = tools(skill_version="0.7.0")
    adapter = KnowledgeLoopTools(
        qa=cast(GroundedQAApplicationPort, _qa),
        search=_search,
        config=KnowledgeLoopToolsConfig(
            profile=execution_profile(),
            versions=versions(skill_version="0.7.0"),
            tool_version="1.1.0",
            resource_resolver=resolver,
        ),
    )

    output = await adapter.summarize_document({"document_reference": "CLAUDE.md"}, tool_context())

    assert output["status"] == "ambiguous"
    assert output["candidate_labels"] == ["CLAUDE.md"]
    assert output["recommended_next"] == "clarify"


@pytest.mark.asyncio
async def test_document_summary_can_be_serially_composed_with_knowledge_search() -> None:
    resolver = FakeResourceResolver()
    search = FakeSearchService()
    qa = FakeGroundedQA()
    adapter = KnowledgeLoopTools(
        qa=cast(GroundedQAApplicationPort, qa),
        search=search,
        config=KnowledgeLoopToolsConfig(
            profile=execution_profile(),
            versions=versions(skill_version="0.7.0"),
            tool_version="1.1.0",
            resource_resolver=resolver,
        ),
    )
    result = await AgentLoopExecutor(
        tool_registry=adapter.tool_registry,
        allowed_tools=adapter.allowed_tools,
        system_prompt="Use the registered knowledge and document Tools.",
        model_gateway=cast(
            ModelGateway,
            DecisionGateway(
                (
                    '{"action":"call_tool","tool_name":"knowledge_search",'
                    '"arguments":{"query":"OmniStudio modules"}}'
                ),
                '{"action":"call_tool","tool_name":"grounded_answer","arguments":{}}',
                '{"action":"call_tool","tool_name":"knowledge_inspect","arguments":{}}',
                '{"action":"call_tool","tool_name":"grounded_answer","arguments":{}}',
                '{"action":"call_tool","tool_name":"verify_answer","arguments":{}}',
                '{"action":"call_tool","tool_name":"finalize_answer","arguments":{}}',
                '{"action":"complete","reason":"Both grounded tasks are complete."}',
            ),
        ),
        finalizer=adapter.finalizer(),
        decision_policy=adapter.decision_policy,
    ).execute(
        runtime_run(max_tool_calls=8),
        cast(PinnedSkill, object()),
        {"question": "Introduce OmniStudio modules and summarize CLAUDE.md"},
        goal="Introduce OmniStudio modules and summarize CLAUDE.md.",
    )

    assert result.error is None
    assert [item.tool_name for item in result.state.observations] == [
        "knowledge_search",
        "summarize_document",
        "knowledge_inspect",
        "grounded_answer",
        "verify_answer",
        "finalize_answer",
    ]
    assert qa.execute_calls == [RUN_ID]


@pytest.mark.asyncio
async def test_knowledge_loop_requires_verified_qa_before_one_finalization() -> None:
    adapter, _search, qa = tools()
    result = await AgentLoopExecutor(
        tool_registry=adapter.tool_registry,
        allowed_tools=adapter.allowed_tools,
        system_prompt="Use only the fixed knowledge Tools.",
        model_gateway=cast(
            ModelGateway,
            DecisionGateway(
                '{"action":"call_tool","tool_name":"knowledge_search","arguments":{"query":"architecture"}}',
                '{"action":"call_tool","tool_name":"knowledge_inspect","arguments":{}}',
                '{"action":"call_tool","tool_name":"grounded_answer","arguments":{}}',
                '{"action":"call_tool","tool_name":"verify_answer","arguments":{}}',
                '{"action":"call_tool","tool_name":"finalize_answer","arguments":{}}',
                '{"action":"complete","reason":"Grounded QA verified the current Run."}',
            ),
        ),
        finalizer=adapter.finalizer(),
    ).execute(
        runtime_run(),
        cast(PinnedSkill, object()),
        {"question": "architecture", "conversation_id": str(UUID(int=21))},
        goal="Answer the current fixed-Space knowledge question.",
    )

    assert qa.execute_calls == [RUN_ID]
    assert result.output == {
        "status": "completed",
        "outcome": "answer",
        "qa_run_id": str(RUN_ID),
        "publication": "grounded_qa",
    }
    assert result.state.observations[-1].tool_name == "finalize_answer"
    assert result.state.observations[-1].output_summary.startswith("sha256:")


@pytest.mark.asyncio
async def test_knowledge_loop_server_policy_forces_verify_and_finalize_sequence() -> None:
    adapter, _search, _qa = tools()
    state = AgentLoopState.accepted(AgentLoopTask("Answer the question."))
    premature = LLMDecision(LLMDecisionAction.COMPLETE, reason="model stopped early")

    await adapter.grounded_answer({}, tool_context())
    forced_verify = adapter.decision_policy(runtime_run(), state, premature)
    assert forced_verify.action is LLMDecisionAction.CALL_TOOL
    assert forced_verify.tool_name == "verify_answer"

    await adapter.verify_answer({}, tool_context())
    forced_finalize = adapter.decision_policy(runtime_run(), state, premature)
    assert forced_finalize.action is LLMDecisionAction.CALL_TOOL
    assert forced_finalize.tool_name == "finalize_answer"

    await adapter.finalize_answer({}, tool_context())
    forced_complete = adapter.decision_policy(runtime_run(), state, premature)
    assert forced_complete.action is LLMDecisionAction.COMPLETE


@pytest.mark.asyncio
async def test_v8_policy_leaves_an_unstarted_direct_turn_to_the_outer_assistant() -> None:
    adapter, _search, _qa = tools(skill_version="0.8.0")
    direct = LLMDecision(
        LLMDecisionAction.COMPLETE,
        reason="The request is ordinary conversation.",
        final_response="A direct answer.",
    )

    assert (
        adapter.decision_policy(
            runtime_run(), AgentLoopState.accepted(AgentLoopTask("Ordinary conversation.")), direct
        )
        is direct
    )


def test_v9_policy_requires_a_named_document_summary_before_grounded_answer() -> None:
    search = FakeSearchService()
    qa = FakeGroundedQA()
    adapter = KnowledgeLoopTools(
        qa=cast(GroundedQAApplicationPort, qa),
        search=search,
        config=KnowledgeLoopToolsConfig(
            profile=execution_profile(),
            versions=versions(skill_version="0.9.0"),
            tool_version="1.1.0",
            resource_resolver=FakeResourceResolver(),
        ),
    )
    requested = LLMDecision(
        LLMDecisionAction.CALL_TOOL,
        tool_name="grounded_answer",
        arguments={},
    )

    recovered = adapter.decision_policy(
        runtime_run(),
        AgentLoopState.accepted(
            AgentLoopTask("Introduce OmniStudio modules and summarize CLAUDE.md.")
        ),
        requested,
    )

    assert recovered.action is LLMDecisionAction.CALL_TOOL
    assert recovered.tool_name == "summarize_document"
    assert recovered.arguments == {"document_reference": "CLAUDE.md"}

    recovered_chinese = adapter.decision_policy(
        runtime_run(),
        AgentLoopState.accepted(
            AgentLoopTask("介绍一下 OmniStudio 的主要模块，并为 CLAUDE.md 写一下摘要。")
        ),
        requested,
    )

    assert recovered_chinese.action is LLMDecisionAction.CALL_TOOL
    assert recovered_chinese.tool_name == "summarize_document"
    assert recovered_chinese.arguments == {"document_reference": "CLAUDE.md"}


@pytest.mark.asyncio
async def test_v8_policy_replaces_an_identical_followup_search() -> None:
    adapter, _search, _qa = tools(skill_version="0.8.0")
    await adapter.knowledge_search({"query": "architecture"}, tool_context())
    await adapter.knowledge_inspect({}, tool_context())
    repeated = LLMDecision(
        LLMDecisionAction.CALL_TOOL,
        tool_name="knowledge_search",
        arguments={"query": "architecture"},
    )

    recovered = adapter.decision_policy(
        runtime_run(), AgentLoopState.accepted(AgentLoopTask("Architecture overview.")), repeated
    )

    assert recovered.action is LLMDecisionAction.CALL_TOOL
    assert recovered.tool_name == "grounded_answer"
    assert recovered.arguments == {}


@pytest.mark.asyncio
async def test_v8_policy_does_not_turn_a_meta_instruction_into_a_search_query() -> None:
    adapter, _search, _qa = tools(skill_version="0.8.0")
    await adapter.knowledge_search({"query": "omnistudio 主要模块"}, tool_context())
    await adapter.knowledge_inspect({}, tool_context())

    recovered = adapter.decision_policy(
        runtime_run(),
        AgentLoopState.accepted(AgentLoopTask("介绍 OmniStudio 的主要模块。")),
        LLMDecision(
            LLMDecisionAction.CALL_TOOL,
            tool_name="knowledge_search",
            arguments={"query": "你可以通过 knowledge_search 查询 additional supporting evidence"},
        ),
    )

    assert recovered.action is LLMDecisionAction.CALL_TOOL
    assert recovered.tool_name == "grounded_answer"
    assert recovered.arguments == {}


@pytest.mark.asyncio
async def test_v8_policy_resolves_a_model_selected_qa_workspace_write() -> None:
    adapter, _search, _qa = tools(skill_version="0.8.0")
    context = tool_context()
    await adapter.knowledge_search({"query": "omnistudio 主要模块"}, context)
    await adapter.knowledge_inspect({}, context)
    await adapter.grounded_answer({}, context)
    await adapter.verify_answer({}, context)
    await adapter.finalize_answer({}, context)

    requested = LLMDecision(
        LLMDecisionAction.CALL_TOOL,
        tool_name="fs_write",
        arguments={
            "path": "omnistudio-modules.md",
            "content": "{{current_grounded_qa_answer}}",
        },
    )
    decision = adapter.decision_policy(
        runtime_run(),
        AgentLoopState.accepted(
            AgentLoopTask("介绍 OmniStudio 的主要模块，将结果保存为md文件，放在工作区内。")
        ),
        requested,
    )

    assert decision.action is LLMDecisionAction.CALL_TOOL
    assert decision.tool_name == "fs_write"
    assert decision.arguments["path"] == "omnistudio-modules.md"
    assert decision.arguments["content"].startswith("Authoritative QA answer")


def test_v8_policy_allows_workspace_tools_after_checkpointed_finalization() -> None:
    adapter, _search, _qa = tools(skill_version="0.8.0")
    task = AgentLoopTask("总结上传资料并保存到工作区。")
    state = AgentLoopState.accepted(task).start()
    state = (
        state.begin_iteration({"action": "call_tool"})
        .request_tool(
            name="finalize_answer",
            version="1.0.0",
            arguments={},
            idempotency_key="finalize-1",
            request_fingerprint="finalize-1",
        )
        .start_tool()
        .observe(
            AgentLoopToolObservation(
                iteration=1,
                tool_name="finalize_answer",
                tool_version="1.0.0",
                idempotency_key="finalize-1",
                input_summary="sha256:input",
                output_summary="sha256:output",
                model_output={"ready": True, "publication": "grounded_qa", "outcome": "answer"},
            )
        )
    )

    requested = LLMDecision(
        LLMDecisionAction.CALL_TOOL,
        tool_name="fs_list",
        arguments={"path": "."},
    )
    assert adapter.decision_policy(runtime_run(), state, requested) is requested


@pytest.mark.asyncio
async def test_knowledge_loop_policy_cannot_skip_search_inspect_or_followup() -> None:
    adapter, search, qa = tools(skill_version="0.6.0")
    result = await AgentLoopExecutor(
        tool_registry=adapter.tool_registry,
        allowed_tools=adapter.allowed_tools,
        system_prompt="Use only the fixed knowledge Tools.",
        model_gateway=cast(
            ModelGateway,
            DecisionGateway(*([('{"action":"complete","reason":"early"}')] * 8)),
        ),
        finalizer=adapter.finalizer(),
        decision_policy=adapter.decision_policy,
    ).execute(
        runtime_run(max_tool_calls=7),
        cast(PinnedSkill, object()),
        {"question": "architecture", "conversation_id": str(UUID(int=21))},
        goal="Explain the current architecture.",
    )

    assert result.error is None, (
        result.error,
        [item.tool_name for item in result.state.observations],
    )
    assert result.run.status.value == "completed"
    assert [item.tool_name for item in result.state.observations] == [
        "knowledge_search",
        "knowledge_inspect",
        "knowledge_search",
        "knowledge_inspect",
        "grounded_answer",
        "verify_answer",
        "finalize_answer",
    ]
    assert len(search.calls) == 2
    assert qa.execute_calls == [RUN_ID]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_reason"),
    [
        (QAOutcome.REFUSE, "evidence_insufficient"),
        (QAOutcome.CONFLICT, "conflict"),
    ],
)
async def test_refusal_and_conflict_are_safe_verified_terminal_outcomes(
    outcome: QAOutcome, expected_reason: str
) -> None:
    adapter, _search, qa = tools(outcome)
    context = tool_context()

    answer = await adapter.grounded_answer({}, context)
    verified = await adapter.verify_answer({}, context)
    finalization = await adapter.finalize_answer({}, context)
    output = await adapter.finalizer().finalize(
        run=runtime_run(),
        task=AgentLoopTask("Test a safe terminal outcome."),
        decision=LLMDecision(LLMDecisionAction.REFUSE, reason="QA requires a safe refusal."),
        state=AgentLoopState.accepted(AgentLoopTask("Test a safe terminal outcome.")),
        input_data={},
    )

    assert answer["status"] in {"completed", "refused"}
    assert verified["ready"] is True
    assert verified["current_run_only"] is True
    assert verified["terminal_reason"] == expected_reason
    assert finalization == {
        "ready": True,
        "publication": "grounded_qa",
        "outcome": outcome.value,
    }
    assert qa.execute_calls == [RUN_ID]
    assert output["publication"] == "grounded_qa"
