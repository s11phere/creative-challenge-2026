from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from agent_runtime import (
    DeterministicWorkflowExecutor,
    FileSystemSkillRegistry,
    JSONValue,
    PinnedSkill,
    SkillRegistryError,
)
from application.qa.profile import QAPlanningProfileV1
from application.qa.service import GroundedQAExecutionProfile
from application.skills import (
    DerivedKnowledgeWriter,
    KnowledgeAgentSkillAdapter,
    KnowledgeAgentSkillConfig,
    KnowledgeQASkillAdapter,
    KnowledgeQASkillConfig,
)
from domain.agent_runtime import (
    AgentRun,
    AgentRunContext,
    ApprovalPort,
    RunStatus,
    ToolCallRecord,
)
from domain.grounded_qa import (
    Citation,
    Claim,
    GroundedAnswer,
    QAAttempt,
    QAOutcome,
    QAResult,
    QAStatus,
    QuestionInput,
    Refusal,
    RefusalReason,
)
from domain.qa_persistence import ConversationRecord, QARetrievalScope, QARunRecord, QARunVersions
from domain.retrieval import LocatorKind, RetrievalProfileV1, SearchLocator
from jsonschema import Draft202012Validator
from model_gateway import (
    CapabilityAlias,
    ChatRequest,
    ChatResponse,
    FakeModelGateway,
    ModelUsage,
)

ROOT = Path(__file__).parents[2]
SKILLS_ROOT = ROOT / "skills"
PACKAGE_PATH = "knowledge_qa"
SPACE_ID = UUID(int=1)
RUN_ID = UUID(int=2)
QA_RUN_ID = UUID(int=3)
ATTEMPT_ID = UUID(int=4)
MESSAGE_ID = UUID(int=5)
EVIDENCE_ID = UUID(int=6)


def versions() -> QARunVersions:
    return QARunVersions(
        skill_version="0.1.0",
        profile_version="grounded-qa-provisional-v1",
        retrieval_profile_version="retrieval-profile-v1",
        model_identity="fake-fast-chat-v1",
        prompt_version="grounded-qa-v1-provisional",
        output_schema_version="grounded-answer-v1",
        corpus_version="v0-provisional",
        dataset_version="knowledge-qa-v0-provisional",
    )


def profile() -> GroundedQAExecutionProfile:
    return GroundedQAExecutionProfile(
        planning=QAPlanningProfileV1(),
        retrieval=RetrievalProfileV1(
            profile_version="retrieval-profile-v1", embedding_version="embedding-v1"
        ),
    )


def answer_result() -> QAResult:
    citation = Citation(
        evidence_id=EVIDENCE_ID,
        space_id=SPACE_ID,
        source_id=UUID(int=7),
        document_id=UUID(int=8),
        version_id=UUID(int=9),
        chunk_id=UUID(int=10),
        locator=SearchLocator(LocatorKind.LINES, 1, 2),
        excerpt_sha256="a" * 64,
    )
    return QAResult(
        outcome=QAOutcome.ANSWER,
        answer=GroundedAnswer(
            text="Synthetic grounded answer.",
            claims=(Claim("claim-1", "Synthetic grounded answer.", (EVIDENCE_ID,)),),
            citations=(citation,),
        ),
    )


class FakeGroundedQA:
    def __init__(self, *, status: QAStatus = QAStatus.COMPLETED) -> None:
        self.status = status
        self.conversations: list[ConversationRecord] = []
        self.questions: list[QuestionInput] = []
        self.executed_run_ids: list[UUID] = []
        self.run: QARunRecord | None = None

    async def create_conversation(self, conversation: ConversationRecord) -> ConversationRecord:
        self.conversations.append(conversation)
        return conversation

    async def submit(self, question: QuestionInput, *, versions: QARunVersions) -> QARunRecord:
        self.questions.append(question)
        assert question.conversation_id is not None
        self.run = QARunRecord(
            run_id=QA_RUN_ID,
            attempt=QAAttempt(run_id=QA_RUN_ID, attempt_id=ATTEMPT_ID),
            conversation_id=question.conversation_id,
            question_message_id=UUID(int=11),
            space_id=question.space_id,
            caller_id=question.caller_id,
            idempotency_key=question.idempotency_key or "missing",
            versions=versions,
            status=QAStatus.QUEUED,
        )
        return self.run

    async def execute(
        self,
        run_id: UUID,
        *,
        profile: GroundedQAExecutionProfile,
        agent_plan: object | None = None,
    ) -> QARunRecord:
        _ = agent_plan
        self.executed_run_ids.append(run_id)
        assert profile == globals()["profile"]()
        assert self.run is not None
        assert run_id == self.run.run_id
        if self.status is QAStatus.COMPLETED:
            return replace(
                self.run,
                status=QAStatus.COMPLETED,
                result=answer_result(),
                answer_message_id=MESSAGE_ID,
            )
        if self.status is QAStatus.REFUSED:
            return replace(
                self.run,
                status=QAStatus.REFUSED,
                result=QAResult(
                    QAOutcome.REFUSE,
                    refusal=Refusal(
                        RefusalReason.INSUFFICIENT_EVIDENCE,
                        "Insufficient synthetic evidence.",
                    ),
                ),
                answer_message_id=MESSAGE_ID,
            )
        return replace(
            self.run,
            status=QAStatus.FAILED,
            error_code="QA_RETRIEVAL_FAILED",
        )

    async def request_cancel(self, run_id: UUID) -> QARunRecord:
        raise AssertionError(f"unexpected cancellation for {run_id}")


class AgentGateway(FakeModelGateway):
    def __init__(self) -> None:
        super().__init__()
        self.responses = [
            '{"action":"call_tool","tool_name":"grounded_qa","arguments":{}}',
            '{"action":"complete","reason":"Grounded QA completed."}',
        ]

    async def chat(
        self,
        _request: ChatRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.FAST_CHAT,
    ) -> ChatResponse:
        return ChatResponse(
            text=self.responses.pop(0),
            finish_reason="stop",
            usage=ModelUsage(input_tokens=3, output_tokens=2),
            capability=capability,
            latency_ms=0.0,
        )


def runtime(
    qa: FakeGroundedQA,
    *,
    execute_existing_run: bool = False,
    package_path: str = PACKAGE_PATH,
    retrieval_scope: QARetrievalScope | None = None,
    approval_port: ApprovalPort | None = None,
    approval_id: str | None = None,
    derived_writer: DerivedKnowledgeWriter | None = None,
) -> tuple[DeterministicWorkflowExecutor, PinnedSkill, AgentRun]:
    registry = FileSystemSkillRegistry(SKILLS_ROOT)
    package = registry.register(registry.load(package_path))
    registry.activate(package.manifest.name, package.manifest.version)
    pin = registry.pin(package.manifest.name)
    fixed_versions = replace(
        versions(), skill_name=pin.name, skill_content_sha256=pin.content_sha256
    )
    if execute_existing_run:
        qa.run = QARunRecord(
            run_id=RUN_ID,
            attempt=QAAttempt(run_id=RUN_ID, attempt_id=ATTEMPT_ID),
            conversation_id=UUID(int=12),
            question_message_id=UUID(int=11),
            space_id=SPACE_ID,
            caller_id="synthetic-user",
            idempotency_key=str(RUN_ID),
            versions=fixed_versions,
            retrieval_scope=retrieval_scope or QARetrievalScope(),
            status=QAStatus.QUEUED,
        )
    adapter = KnowledgeQASkillAdapter(
        qa=qa,
        config=KnowledgeQASkillConfig(
            profile=profile(),
            versions=fixed_versions,
            execute_existing_run=execute_existing_run,
            skill_name=pin.name,
            output_schema_version={
                "knowledge_qa": "knowledge-qa-skill-output-v1",
                "summarize_document": "summarize-document-skill-output-v1",
                "compare_sources": "compare-sources-skill-output-v1",
                "create_review_cards": "review-cards-skill-output-v1",
            }[pin.name],
            preview_only_write=pin.name == "create_review_cards" and derived_writer is None,
            approval_port=approval_port,
            approval_id=approval_id,
            derived_writer=derived_writer,
        ),
    )
    run = AgentRun(
        context=AgentRunContext(
            run_id=RUN_ID,
            space_id=SPACE_ID,
            skill_name=pin.name,
            skill_version=pin.version,
            skill_content_sha256=pin.content_sha256,
            trace_id="trace-knowledge-qa-fixture",
            caller_id="synthetic-user",
            granted_permissions=package.manifest.permissions,
        ),
        budget=package.manifest.budgets,
    )
    executor = DeterministicWorkflowExecutor(
        skill_registry=registry,
        model_gateway=FakeModelGateway(),
        handlers=adapter.handlers(),
        clock_ms=lambda: 0,
    )
    return executor, pin, run


@pytest.mark.asyncio
async def test_provisional_package_delegates_to_qa_port_and_reuses_its_output() -> None:
    qa = FakeGroundedQA()
    executor, pin, run = runtime(qa)

    result = await executor.execute(run, pin, {"question": "Use the synthetic fixture."})

    assert result.run.status is RunStatus.COMPLETED
    assert result.refused is False
    assert isinstance(result.output, dict)
    assert result.output["status"] == "completed"
    projected = result.output["result"]
    assert isinstance(projected, dict)
    assert projected["type"] == "answer"
    assert qa.questions[0].space_id == SPACE_ID
    assert qa.questions[0].caller_id == "synthetic-user"
    assert qa.questions[0].idempotency_key == str(RUN_ID)
    schema = json.loads(
        (SKILLS_ROOT / PACKAGE_PATH / "schemas/output.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(result.output)


class ApprovedWrite(ApprovalPort):
    async def request(self, _context: AgentRunContext, _tool: ToolCallRecord) -> str:
        return "approval-1"

    async def is_approved(self, approval_id: str, _context: AgentRunContext) -> bool:
        return approval_id == "approval-1"


class RecordingDerivedWriter(DerivedKnowledgeWriter):
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def write_review_cards(
        self,
        *,
        run_id: UUID,
        space_id: UUID,
        created_by: str,
        idempotency_key: str,
        content: dict[str, JSONValue],
        citation_ids: tuple[str, ...],
    ) -> object:
        self.calls.append(
            {
                "run_id": run_id,
                "space_id": space_id,
                "created_by": created_by,
                "idempotency_key": idempotency_key,
                "content": content,
                "citation_ids": citation_ids,
            }
        )
        return object()


@pytest.mark.asyncio
async def test_review_cards_write_requires_approval_and_calls_idempotent_port() -> None:
    writer = RecordingDerivedWriter()
    qa = FakeGroundedQA()
    executor, pin, run = runtime(
        qa,
        execute_existing_run=True,
        package_path="create_review_cards",
        approval_port=ApprovedWrite(),
        approval_id="approval-1",
        derived_writer=writer,
    )

    result = await executor.execute(
        run,
        pin,
        {"question": "Create cards.", "conversation_id": str(UUID(int=12))},
    )

    assert result.run.status is RunStatus.COMPLETED
    assert result.output["write"] == {"status": "persisted", "side_effects": 1}
    assert len(writer.calls) == 1
    assert writer.calls[0]["idempotency_key"] == str(RUN_ID)


@pytest.mark.asyncio
async def test_refusal_is_normal_result_but_dependency_failure_is_not() -> None:
    refused_runtime, pin, run = runtime(FakeGroundedQA(status=QAStatus.REFUSED))
    refused = await refused_runtime.execute(run, pin, {"question": "Unknown synthetic fact?"})
    assert refused.run.status is RunStatus.COMPLETED
    assert refused.refused is True
    assert isinstance(refused.output, dict)
    assert refused.output["status"] == "refused"

    failed_runtime, failed_pin, failed_run = runtime(FakeGroundedQA(status=QAStatus.FAILED))
    failed = await failed_runtime.execute(
        failed_run, failed_pin, {"question": "Unavailable synthetic fact?"}
    )
    assert failed.run.status is RunStatus.FAILED
    assert failed.error is not None
    assert failed.error.code == "DEPENDENCY_RETRIEVAL_FAILED"
    assert failed.error.retryable is True


@pytest.mark.asyncio
async def test_client_cannot_supply_space_caller_evidence_or_fixed_skill_fields() -> None:
    qa = FakeGroundedQA()
    executor, pin, run = runtime(qa)
    forbidden = ("space_id", "caller_id", "evidence", "skill_version", "system_prompt")
    for field in forbidden:
        result = await executor.execute(
            run, pin, {"question": "Synthetic question.", field: "attacker-controlled"}
        )
        assert result.run.status is RunStatus.FAILED
        assert result.error is not None
        assert result.error.code == "SKILL_INPUT_INVALID"
    assert qa.questions == []


@pytest.mark.asyncio
async def test_worker_mode_executes_the_existing_run_without_submitting_another() -> None:
    qa = FakeGroundedQA()
    executor, pin, run = runtime(qa, execute_existing_run=True)

    result = await executor.execute(
        run,
        pin,
        {"question": "Use the existing run.", "conversation_id": str(UUID(int=12))},
    )

    assert result.run.status is RunStatus.COMPLETED
    assert qa.questions == []
    assert qa.conversations == []
    assert qa.executed_run_ids == [RUN_ID]
    assert isinstance(result.output, dict)
    assert result.output["run_id"] == str(RUN_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("skill_name", "schema_version"),
    [
        ("summarize_document", "summarize-document-skill-output-v1"),
        ("compare_sources", "compare-sources-skill-output-v1"),
        ("create_review_cards", "review-cards-skill-output-v1"),
    ],
)
async def test_organization_skills_execute_the_fixed_qa_run(
    skill_name: str, schema_version: str
) -> None:
    qa = FakeGroundedQA()
    scope = QARetrievalScope(
        source_ids=frozenset({UUID(int=7), UUID(int=17)}),
        document_ids=frozenset({UUID(int=8), UUID(int=18)}),
        version_ids=frozenset({UUID(int=9), UUID(int=19)}),
    )
    executor, pin, run = runtime(
        qa,
        execute_existing_run=True,
        package_path=skill_name,
        retrieval_scope=scope,
    )

    result = await executor.execute(
        run,
        pin,
        {"question": "Use the fixed organization scope.", "conversation_id": str(UUID(int=12))},
    )

    assert result.run.status is RunStatus.COMPLETED
    assert isinstance(result.output, dict)
    assert result.output["schema_version"] == schema_version
    assert result.output["operation"] == skill_name
    fixed_scope = result.output["fixed_scope"]
    assert isinstance(fixed_scope, dict)
    assert fixed_scope["version_ids"] == [str(UUID(int=9)), str(UUID(int=19))]
    if skill_name == "create_review_cards":
        assert result.output["write"] == {
            "status": "blocked",
            "code": "SKILL_WRITE_REQUIRES_APPROVAL",
            "side_effects": 0,
        }


@pytest.mark.asyncio
async def test_knowledge_agent_calls_grounded_qa_for_the_existing_run() -> None:
    registry = FileSystemSkillRegistry(SKILLS_ROOT)
    package = registry.register(registry.load("knowledge_agent"))
    registry.activate(package.manifest.name, package.manifest.version)
    pin = registry.pin(package.manifest.name)
    fixed_versions = replace(
        versions(),
        skill_name=pin.name,
        skill_content_sha256=pin.content_sha256,
        output_schema_version="knowledge-agent-skill-output-v1",
    )
    qa = FakeGroundedQA()
    qa.run = QARunRecord(
        run_id=RUN_ID,
        attempt=QAAttempt(run_id=RUN_ID, attempt_id=ATTEMPT_ID),
        conversation_id=UUID(int=12),
        question_message_id=UUID(int=11),
        space_id=SPACE_ID,
        caller_id="synthetic-user",
        idempotency_key=str(RUN_ID),
        versions=fixed_versions,
        status=QAStatus.QUEUED,
    )
    adapter = KnowledgeAgentSkillAdapter(
        qa=qa,
        config=KnowledgeAgentSkillConfig(
            profile=profile(),
            versions=fixed_versions,
            system_prompt=(SKILLS_ROOT / "knowledge_agent/prompts/system.md").read_text(
                encoding="utf-8"
            ),
        ),
    )
    run = AgentRun(
        context=AgentRunContext(
            run_id=RUN_ID,
            space_id=SPACE_ID,
            skill_name=pin.name,
            skill_version=pin.version,
            skill_content_sha256=pin.content_sha256,
            trace_id="trace-knowledge-agent-fixture",
            caller_id="synthetic-user",
            granted_permissions=package.manifest.permissions,
        ),
        budget=package.manifest.budgets,
    )
    gateway = AgentGateway()
    executor = DeterministicWorkflowExecutor(
        skill_registry=registry,
        model_gateway=gateway,
        handlers=adapter.handlers(),
        tool_registry=adapter.tool_registry,
        clock_ms=lambda: 0,
    )

    result = await executor.execute(
        run,
        pin,
        {"question": "Use the existing run.", "conversation_id": str(UUID(int=12))},
    )

    assert result.run.status is RunStatus.COMPLETED
    assert result.run.usage.tool_calls == 1
    assert result.run.usage.input_tokens == 3
    assert result.run.usage.output_tokens == 2
    assert result.output == {
        "action": "complete",
        "reason": "Terminal Tool grounded_qa completed.",
    }
    assert qa.questions == []
    assert qa.conversations == []
    assert qa.executed_run_ids == [RUN_ID]
    assert qa.run.versions.skill_name == "knowledge_agent"
    assert qa.run.versions.skill_version == "0.1.0"
    assert len(gateway.responses) == 1


def test_provisional_package_is_bulk_installed_but_activation_is_explicit() -> None:
    registry = FileSystemSkillRegistry(SKILLS_ROOT)
    loaded = registry.reload()
    package = next(package for package in loaded if package.manifest.name == "knowledge_qa")
    assert registry.names() == (
        "compare_sources",
        "course_project_workflow",
        "create_review_cards",
        "exam_preparation_workflow",
        "knowledge_agent",
        "knowledge_qa",
        "research_reading_workflow",
        "summarize_document",
    )
    assert registry.versions("knowledge_qa") == ("0.1.0", "0.2.0")
    with pytest.raises(SkillRegistryError, match="active version"):
        registry.active_version("knowledge_qa")
    registry.activate("knowledge_qa", "0.1.0")
    pin = registry.pin("knowledge_qa")
    assert pin.content_sha256 == package.content_sha256
    assert len(pin.content_sha256) == 64
