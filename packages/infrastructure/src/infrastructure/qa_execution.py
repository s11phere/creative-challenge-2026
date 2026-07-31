"""Shared Grounded QA execution assembly for the independent Worker."""

from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import UUID

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
from domain.grounded_qa import QAEvent, QAStatus
from domain.qa_persistence import GroundedQARepository, QARunRecord, QARunVersions
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


def qa_execution_versions() -> QARunVersions:
    planning, retrieval, generation = _profiles()
    return QARunVersions(
        skill_version="knowledge_qa-0.1.0-provisional",
        profile_version=planning.profile_id,
        retrieval_profile_version=retrieval.profile_version,
        model_identity=generation.model_identity,
        prompt_version=generation.prompt_template_id,
        output_schema_version=generation.structured_output_schema,
        corpus_version="local-live-provisional",
        dataset_version="knowledge-qa-v0-provisional",
    )


class GroundedQAExecutor:
    """Run the single QA Application Port and publish privacy-safe terminal events."""

    def __init__(
        self,
        *,
        database: Database,
        gateway: ModelGateway,
        repository: GroundedQARepository,
        events: QAEventStore,
    ) -> None:
        planning, retrieval, generation = _profiles()
        self.profile = GroundedQAExecutionProfile(planning=planning, retrieval=retrieval)
        self.versions = qa_execution_versions()
        self._repository = repository
        self._events = events
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
                corpus_version=self.versions.corpus_version,
                dataset_version=self.versions.dataset_version,
            ),
        )

    async def execute(self, run_id: UUID) -> QARunRecord | None:
        await self._events.append(run_id, QAEventType.STARTED, {"status": QAStatus.RUNNING.value})
        resolved_run: QARunRecord | None
        try:
            resolved_run = await self._service.execute(run_id, profile=self.profile)
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


def _profiles() -> tuple[QAPlanningProfileV1, RetrievalProfileV1, QAGenerationProfileV1]:
    identity = settings.active_embedding_identity()
    retrieval = RetrievalProfileV1(embedding_version=identity.version)
    planning = QAPlanningProfileV1()
    generation = QAGenerationProfileV1(
        retrieval_profile_reference=retrieval.profile_version,
        model_identity=(settings.fast_chat_model or "fake-fast-chat-v1"),
    )
    return planning, retrieval, generation


__all__ = ["GroundedQAExecutor", "StructuredFakeGateway", "qa_execution_versions"]
