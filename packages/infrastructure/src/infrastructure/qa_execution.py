"""Shared Grounded QA execution assembly for the independent Worker."""

from __future__ import annotations

import json
import logging
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
    DerivedKnowledgeWriter,
    KnowledgeAgentSkillAdapter,
    KnowledgeAgentSkillConfig,
    KnowledgeQASkillAdapter,
    KnowledgeQASkillConfig,
)
from domain.agent_runtime import AgentRun, AgentRunContext, ApprovalPort, RunStatus
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
from .qa_debug_trace import QADebugTrace, TracingModelGateway, TracingToolRegistry
from .runtime_state import PostgresRuntimeStateStore

logger = logging.getLogger(__name__)

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


def assistant_skill_registry() -> FileSystemSkillRegistry:
    """Build the v2 invocation catalog without changing the legacy QA pointer."""
    registry = FileSystemSkillRegistry(Path(settings.skill_root_path))
    registry.reload()
    for name in (
        "knowledge_qa",
        "summarize_document",
        "compare_sources",
        "create_review_cards",
    ):
        registry.activate(name, "0.2.0")
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
        approval_port: ApprovalPort | None = None,
        approval_id: str | None = None,
        derived_writer: DerivedKnowledgeWriter | None = None,
    ) -> None:
        self._database = database
        planning, retrieval, generation = _profiles()
        self.profile = GroundedQAExecutionProfile(planning=planning, retrieval=retrieval)
        self._gateway = gateway
        self._repository = repository
        self._events = events
        self._skill_registry = skill_registry
        self._approval_port = approval_port
        self._approval_id = approval_id
        self._derived_writer = derived_writer
        self._generation = generation

    def _build_service(
        self,
        gateway: ModelGateway,
        *,
        generation_gateway: ModelGateway,
    ) -> GroundedQAService:
        return GroundedQAService(
            repository=self._repository,
            planner=QueryPlanner(),
            search=QASearchCoordinator(DatabaseSearchService(self._database, gateway)),
            evidence_binding=EvidenceBindingService(),
            context_builder=ContextBuilder(),
            generator=GroundedAnswerGenerator(
                gateway=generation_gateway,
                parser=StructuredAnswerParser(json.loads(_SCHEMA.read_text(encoding="utf-8"))),
                verifier=EvidenceVerifier(PostgresCitationTargetPort(self._database)),
                profile=self._generation,
                prompt_contract=_PROMPT.read_text(encoding="utf-8"),
                corpus_version="local-live-provisional",
                dataset_version="knowledge-qa-v0-provisional",
            ),
        )

    async def execute(self, run_id: UUID, *, trace_id: str) -> QARunRecord | None:
        await self._events.append(run_id, QAEventType.STARTED, {"status": QAStatus.RUNNING.value})
        trace = QADebugTrace.from_settings(run_id=run_id, trace_id=trace_id, settings=settings)
        initial = await self._repository.get_run(run_id)
        await trace.record(
            "run_started",
            skill_name=initial.versions.skill_name if initial is not None else None,
            skill_version=initial.versions.skill_version if initial is not None else None,
        )
        resolved_run: QARunRecord | None
        try:
            persisted = initial
            if persisted is None or persisted.status in _TERMINAL:
                resolved_run = persisted
            else:
                resolved_run = await self._execute_skill(persisted, trace_id=trace_id, trace=trace)
        except SkillRegistryError as error:
            logger.error(
                "qa_run_failed",
                extra={"error_code": "QA_SKILL_INVALID", "error_type": type(error).__name__},
            )
            await trace.record(
                "run_error", error=_safe_error(error), error_code=QAErrorCode.SKILL_INVALID.value
            )
            recovered_run = await self._repository.get_run(run_id)
            if recovered_run is not None and recovered_run.status not in _TERMINAL:
                recovered_run = await self._repository.transition_run(
                    run_id, QAEvent.FAIL, error_code=QAErrorCode.SKILL_INVALID.value
                )
            resolved_run = recovered_run
        except Exception as error:
            logger.error(
                "qa_run_failed",
                extra={"error_code": "QA_RUNTIME_FAILED", "error_type": type(error).__name__},
            )
            await trace.record(
                "run_error", error=_safe_error(error), error_code="QA_RUNTIME_FAILED"
            )
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
        await trace.record(
            "run_result",
            status=resolved_run.status.value,
            error_code=resolved_run.error_code,
            outcome=resolved_run.result.outcome.value if resolved_run.result is not None else None,
            result=resolved_run.result,
            usage=resolved_run.usage,
        )
        return resolved_run

    async def _execute_skill(
        self, run: QARunRecord, *, trace_id: str, trace: QADebugTrace
    ) -> QARunRecord:
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
        service = self._build_service(
            self._gateway,
            generation_gateway=TracingModelGateway(
                StructuredFakeGateway(self._gateway), trace, phase="grounded_generation"
            ),
        )
        question = await self._repository.get_message(run.question_message_id)
        if question is None or question.role is not MessageRole.USER:
            return await self._repository.transition_run(
                run.run_id, QAEvent.FAIL, error_code="QA_RUNTIME_FAILED"
            )
        runtime_gateway: ModelGateway
        if run.versions.skill_name == "knowledge_agent":
            agent_adapter = KnowledgeAgentSkillAdapter(
                qa=service,
                config=KnowledgeAgentSkillConfig(
                    profile=self.profile,
                    versions=run.versions,
                    system_prompt=(package.root / package.manifest.prompts[0]).read_text(
                        encoding="utf-8"
                    ),
                ),
            )
            runtime_gateway = TracingModelGateway(
                StructuredAgentGateway(self._gateway), trace, phase="agent_decision"
            )
            runtime_tool_registry = TracingToolRegistry(agent_adapter.tool_registry, trace)
            agent_adapter.replace_tool_registry(runtime_tool_registry)
            runtime_handlers = agent_adapter.handlers()
        else:
            qa_adapter = KnowledgeQASkillAdapter(
                qa=service,
                config=KnowledgeQASkillConfig(
                    profile=self.profile,
                    versions=run.versions,
                    execute_existing_run=True,
                    skill_name=run.versions.skill_name,
                    output_schema_version=_skill_output_schema(run.versions.skill_name),
                    preview_only_write=(
                        run.versions.skill_name == "create_review_cards"
                        and self._derived_writer is None
                    ),
                    approval_port=self._approval_port,
                    approval_id=self._approval_id,
                    derived_writer=self._derived_writer,
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
        if result.error is not None:
            logger.error(
                "qa_runtime_result_failed",
                extra={
                    "error_code": persisted.error_code or result.error.code,
                    "retryable": result.error.retryable,
                },
            )
            await trace.record(
                "runtime_result",
                status=result.run.status.value,
                error={
                    "error_code": result.error.code,
                    "category": result.error.category.value,
                    "message": result.error.message,
                    "retryable": result.error.retryable,
                },
                qa_error_code=persisted.error_code,
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


def _safe_error(error: BaseException) -> dict[str, str]:
    """Keep terminal diagnostics useful without logging request/document bodies."""
    payload = {"error_type": type(error).__name__, "message": str(error)[:2000]}
    code = getattr(error, "code", None)
    if code is not None:
        payload["error_code"] = getattr(code, "value", str(code))
    return payload


__all__ = [
    "GroundedQAExecutor",
    "StructuredFakeGateway",
    "active_knowledge_qa_pin",
    "assistant_skill_registry",
    "knowledge_qa_registry",
    "qa_execution_versions",
]
