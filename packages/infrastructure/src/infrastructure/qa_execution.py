"""Shared Grounded QA execution assembly for the independent Worker."""

from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import UUID

from agent_runtime import (
    DeterministicWorkflowExecutor,
    FileSystemSkillRegistry,
    PinnedSkill,
    SkillRegistryError,
    SkillRegistryErrorCode,
)
from application.qa import (
    ContextBuilder,
    EvidenceBindingService,
    EvidenceVerifier,
    GroundedAnswerGenerator,
    GroundedQAExecutionProfile,
    GroundedQAService,
    QAGenerationProfileV1,
    QAPlanningProfileV1,
    QASearchCoordinator,
    QueryPlanner,
    StructuredAnswerParser,
)
from application.skills import (
    KnowledgeAgentSkillAdapter,
    KnowledgeAgentSkillConfig,
    KnowledgeQASkillAdapter,
    KnowledgeQASkillConfig,
)
from domain.agent_runtime import AgentRun, AgentRunContext, RunStatus
from domain.grounded_qa import QAErrorCode, QAEvent, QAStatus
from domain.qa_persistence import (
    GroundedQARepository,
    MessageRole,
    QARunRecord,
    QARunVersions,
)
from domain.qa_sse import QAEventStore, QAEventType
from domain.retrieval import RetrievalProfileV1
from model_gateway import (
    CapabilityAlias,
    ChatRequest,
    ChatResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    FakeModelGateway,
    GatewayStatus,
    ModelGateway,
    ModelUsage,
    RerankRequest,
    RerankResponse,
)

from .config import settings
from .database import Database
from .qa import DatabaseSearchService, PostgresCitationTargetPort
from .runtime_state import PostgresRuntimeStateStore

_ROOT = Path(__file__).resolve().parents[4]
_SCHEMA = _ROOT / "cases/evals/configs/grounded-answer-v1.schema.json"
_PROMPT = _ROOT / "cases/evals/prompts/grounded-qa-v1-provisional.txt"
_EVIDENCE = re.compile(
    r'<evidence id="([0-9a-f-]+)"[^>]*>\s*<<<UNTRUSTED_EVIDENCE>>>\s*(.*?)\s*'
    r"<<<END_UNTRUSTED_EVIDENCE>>>",
    re.DOTALL,
)
_TERMINAL = frozenset(
    {QAStatus.COMPLETED, QAStatus.REFUSED, QAStatus.FAILED, QAStatus.CANCELLED, QAStatus.TIMED_OUT}
)


class StructuredFakeGateway:
    """Make the default fake provider return a minimal grounded extractive answer."""

    def __init__(self, delegate: ModelGateway) -> None:
        self._delegate = delegate

    @property
    def status(self) -> GatewayStatus:
        return self._delegate.status

    async def chat(
        self,
        request: ChatRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.FAST_CHAT,
    ) -> ChatResponse:
        if not isinstance(self._delegate, FakeModelGateway):
            return await self._delegate.chat(request, capability=capability)
        user_content = request.messages[-1].content
        match = _EVIDENCE.search(user_content)
        if match is None:
            payload: dict[str, object] = {
                "schema_version": "grounded-answer-v1",
                "result_type": "refuse",
                "reason": "insufficient_evidence",
                "message": "No usable evidence was retrieved.",
                "limitations": ["Provisional local answer mode."],
            }
        else:
            evidence_id, raw_text = match.groups()
            claim = " ".join(raw_text.split())[:1200]
            payload = {
                "schema_version": "grounded-answer-v1",
                "result_type": "answer",
                "answer": claim,
                "claims": [
                    {"claim_id": "extractive-1", "text": claim, "evidence_ids": [evidence_id]}
                ],
                "limitations": [
                    "Deterministic extractive fallback; answer quality is not formally frozen."
                ],
            }
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return ChatResponse(
            text=text,
            finish_reason="stop",
            usage=ModelUsage(
                input_tokens=sum(max(1, len(item.content.split())) for item in request.messages),
                output_tokens=max(1, len(text.split())),
            ),
            capability=capability,
            latency_ms=0.0,
        )

    async def embed(
        self,
        request: EmbeddingRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.EMBEDDING_ZH,
    ) -> EmbeddingResponse:
        return await self._delegate.embed(request, capability=capability)

    async def rerank(
        self,
        request: RerankRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.RERANKER_MULTILINGUAL,
    ) -> RerankResponse:
        return await self._delegate.rerank(request, capability=capability)

    async def aclose(self) -> None:
        return None


class StructuredAgentGateway:
    """Return deterministic Agent decisions only for the default fake provider."""

    def __init__(self, delegate: ModelGateway) -> None:
        self._delegate = delegate

    @property
    def status(self) -> GatewayStatus:
        return self._delegate.status

    async def chat(
        self,
        request: ChatRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.FAST_CHAT,
    ) -> ChatResponse:
        if not isinstance(self._delegate, FakeModelGateway):
            return await self._delegate.chat(request, capability=capability)
        has_tool_result = '"tool_result"' in request.messages[-1].content
        payload = (
            {"action": "complete", "reason": "Grounded QA completed."}
            if has_tool_result
            else {"action": "call_tool", "tool_name": "grounded_qa", "arguments": {}}
        )
        text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        return ChatResponse(
            text=text,
            finish_reason="stop",
            usage=ModelUsage(
                input_tokens=sum(max(1, len(item.content.split())) for item in request.messages),
                output_tokens=max(1, len(text.split())),
            ),
            capability=capability,
            latency_ms=0.0,
        )

    async def embed(
        self,
        request: EmbeddingRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.EMBEDDING_ZH,
    ) -> EmbeddingResponse:
        return await self._delegate.embed(request, capability=capability)

    async def rerank(
        self,
        request: RerankRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.RERANKER_MULTILINGUAL,
    ) -> RerankResponse:
        return await self._delegate.rerank(request, capability=capability)

    async def aclose(self) -> None:
        return None


def qa_execution_versions(
    skill_registry: FileSystemSkillRegistry | None = None,
    *,
    skill_name: str = "knowledge_qa",
) -> QARunVersions:
    planning, retrieval, generation = _profiles()
    registry = skill_registry or knowledge_qa_registry()
    pin = registry.pin(skill_name)
    return QARunVersions(
        skill_name=pin.name,
        skill_version=pin.version,
        skill_content_sha256=pin.content_sha256,
        profile_version=planning.profile_id,
        retrieval_profile_version=retrieval.profile_version,
        model_identity=generation.model_identity,
        prompt_version=generation.prompt_template_id,
        output_schema_version=generation.structured_output_schema,
        corpus_version="local-live-provisional",
        dataset_version="knowledge-qa-v0-provisional",
    )


def knowledge_qa_registry() -> FileSystemSkillRegistry:
    """Load the configured trusted root and rebuild its active QA pointer."""
    registry = FileSystemSkillRegistry(Path(settings.skill_root_path))
    registry.reload()
    registry.activate("knowledge_qa", settings.knowledge_qa_skill_version)
    return registry


def active_knowledge_qa_pin(
    skill_registry: FileSystemSkillRegistry | None = None,
) -> PinnedSkill:
    registry = skill_registry or knowledge_qa_registry()
    return registry.pin("knowledge_qa")


class GroundedQAExecutor:
    """Run the single QA Application Port and publish privacy-safe terminal events."""

    def __init__(
        self,
        *,
        database: Database,
        gateway: ModelGateway,
        repository: GroundedQARepository,
        events: QAEventStore,
        skill_registry: FileSystemSkillRegistry | None = None,
    ) -> None:
        self._database = database
        planning, retrieval, generation = _profiles()
        self.profile = GroundedQAExecutionProfile(planning=planning, retrieval=retrieval)
        self._gateway = gateway
        self._repository = repository
        self._events = events
        self._skill_registry = skill_registry
        self._service = GroundedQAService(
            repository=repository,
            planner=QueryPlanner(),
            search=QASearchCoordinator(DatabaseSearchService(database, gateway)),
            evidence_binding=EvidenceBindingService(),
            context_builder=ContextBuilder(),
            generator=GroundedAnswerGenerator(
                gateway=StructuredFakeGateway(gateway),
                parser=StructuredAnswerParser(json.loads(_SCHEMA.read_text(encoding="utf-8"))),
                verifier=EvidenceVerifier(PostgresCitationTargetPort(database)),
                profile=generation,
                prompt_contract=_PROMPT.read_text(encoding="utf-8"),
                corpus_version="local-live-provisional",
                dataset_version="knowledge-qa-v0-provisional",
            ),
        )

    async def execute(self, run_id: UUID, *, trace_id: str) -> QARunRecord | None:
        await self._events.append(run_id, QAEventType.STARTED, {"status": QAStatus.RUNNING.value})
        resolved_run: QARunRecord | None
        try:
            persisted = await self._repository.get_run(run_id)
            if persisted is None or persisted.status in _TERMINAL:
                resolved_run = persisted
            else:
                resolved_run = await self._execute_skill(persisted, trace_id=trace_id)
        except SkillRegistryError:
            recovered_run = await self._repository.get_run(run_id)
            if recovered_run is not None and recovered_run.status not in _TERMINAL:
                recovered_run = await self._repository.transition_run(
                    run_id, QAEvent.FAIL, error_code=QAErrorCode.SKILL_INVALID.value
                )
            resolved_run = recovered_run
        except Exception:
            recovered_run = await self._repository.get_run(run_id)
            if recovered_run is not None and recovered_run.status not in _TERMINAL:
                recovered_run = await self._repository.transition_run(
                    run_id, QAEvent.FAIL, error_code="QA_RUNTIME_FAILED"
                )
            resolved_run = recovered_run
        if resolved_run is None:
            return None
        event_type = {
            QAStatus.COMPLETED: QAEventType.COMPLETED,
            QAStatus.REFUSED: QAEventType.REFUSED,
            QAStatus.CANCELLED: QAEventType.CANCELLED,
            QAStatus.TIMED_OUT: QAEventType.TIMED_OUT,
        }.get(resolved_run.status, QAEventType.FAILED)
        await self._events.append(
            run_id,
            event_type,
            {"status": resolved_run.status.value, "error_code": resolved_run.error_code},
        )
        return resolved_run

    async def _execute_skill(self, run: QARunRecord, *, trace_id: str) -> QARunRecord:
        registry = self._skill_registry or knowledge_qa_registry()
        pin = registry.pin(run.versions.skill_name, run.versions.skill_version)
        if (
            run.versions.skill_content_sha256 is None
            or pin.content_sha256 != run.versions.skill_content_sha256
        ):
            return await self._repository.transition_run(
                run.run_id, QAEvent.FAIL, error_code=QAErrorCode.SKILL_INVALID.value
            )
        package = registry.validate_pin(pin)
        question = await self._repository.get_message(run.question_message_id)
        if question is None or question.role is not MessageRole.USER:
            return await self._repository.transition_run(
                run.run_id, QAEvent.FAIL, error_code="QA_RUNTIME_FAILED"
            )
        if run.versions.skill_name == "knowledge_agent":
            agent_adapter = KnowledgeAgentSkillAdapter(
                qa=self._service,
                config=KnowledgeAgentSkillConfig(
                    profile=self.profile,
                    versions=run.versions,
                    system_prompt=(package.root / package.manifest.prompts[0]).read_text(
                        encoding="utf-8"
                    ),
                ),
            )
            runtime_gateway: ModelGateway = StructuredAgentGateway(self._gateway)
            runtime_tool_registry = agent_adapter.tool_registry
            runtime_handlers = agent_adapter.handlers()
        else:
            qa_adapter = KnowledgeQASkillAdapter(
                qa=self._service,
                config=KnowledgeQASkillConfig(
                    profile=self.profile,
                    versions=run.versions,
                    execute_existing_run=True,
                    skill_name=run.versions.skill_name,
                    output_schema_version=_skill_output_schema(run.versions.skill_name),
                    preview_only_write=run.versions.skill_name == "create_review_cards",
                ),
            )
            runtime_gateway = self._gateway
            runtime_tool_registry = None
            runtime_handlers = qa_adapter.handlers()
        runtime_run = AgentRun(
            context=AgentRunContext(
                run_id=run.run_id,
                space_id=run.space_id,
                skill_name=pin.name,
                skill_version=pin.version,
                skill_content_sha256=pin.content_sha256,
                trace_id=trace_id,
                caller_id=run.caller_id,
                granted_permissions=package.manifest.permissions,
            ),
            budget=package.manifest.budgets,
        )
        runtime_executor = DeterministicWorkflowExecutor(
            skill_registry=registry,
            model_gateway=runtime_gateway,
            handlers=runtime_handlers,
            tool_registry=runtime_tool_registry,
            state_store=PostgresRuntimeStateStore(self._database),
        )
        runtime_input = {
            "question": question.content,
            "conversation_id": str(run.conversation_id),
        }
        state_store = PostgresRuntimeStateStore(self._database)
        persisted_runtime = await state_store.get_run(run.run_id)
        checkpoint = await state_store.get_latest(run.run_id)
        if (
            persisted_runtime is not None
            and checkpoint is not None
            and persisted_runtime.status
            not in {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
                RunStatus.TIMED_OUT,
            }
        ):
            result = await runtime_executor.resume(
                persisted_runtime,
                pin,
                checkpoint,
                runtime_input,
                caller_id=run.caller_id,
                space_id=run.space_id,
            )
        else:
            result = await runtime_executor.execute(runtime_run, pin, runtime_input)
        persisted = await self._repository.get_run(run.run_id)
        if persisted is None:
            raise RuntimeError("Grounded QA run disappeared during Skill execution")
        if result.error is not None and persisted.status not in _TERMINAL:
            error_code = (
                QAErrorCode.SKILL_INVALID.value
                if result.error.code.startswith("SKILL_")
                else "QA_RUNTIME_FAILED"
            )
            return await self._repository.transition_run(
                run.run_id, QAEvent.FAIL, error_code=error_code
            )
        return persisted


def _profiles() -> tuple[QAPlanningProfileV1, RetrievalProfileV1, QAGenerationProfileV1]:
    identity = settings.active_embedding_identity()
    retrieval = RetrievalProfileV1(embedding_version=identity.version)
    planning = QAPlanningProfileV1()
    generation = QAGenerationProfileV1(
        retrieval_profile_reference=retrieval.profile_version,
        model_identity=(settings.fast_chat_model or "fake-fast-chat-v1"),
    )
    return planning, retrieval, generation


def _skill_output_schema(skill_name: str) -> str:
    schemas = {
        "knowledge_qa": "knowledge-qa-skill-output-v1",
        "summarize_document": "summarize-document-skill-output-v1",
        "compare_sources": "compare-sources-skill-output-v1",
        "create_review_cards": "review-cards-skill-output-v1",
        "knowledge_agent": "knowledge-agent-skill-output-v1",
    }
    try:
        return schemas[skill_name]
    except KeyError as exc:
        raise SkillRegistryError(
            SkillRegistryErrorCode.NOT_FOUND,
            "No Grounded QA adapter is registered for this Skill.",
        ) from exc


__all__ = [
    "GroundedQAExecutor",
    "StructuredFakeGateway",
    "active_knowledge_qa_pin",
    "knowledge_qa_registry",
    "qa_execution_versions",
]
