from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from uuid import UUID

import pytest
from application.qa.context_builder import ContextBuilder
from application.qa.evidence import EvidenceBindingService, EvidenceVerifier
from application.qa.generation import GroundedAnswerGenerator, StructuredAnswerParser
from application.qa.persistence import InMemoryGroundedQARepository
from application.qa.profile import QAGenerationProfileV1, QAPlanningProfileV1
from application.qa.query_planning import QASearchCoordinator, QueryPlanner
from application.qa.service import GroundedQAExecutionProfile, GroundedQAService
from domain.grounded_qa import (
    CitationContentKind,
    CitationTargetQuery,
    CitationTargetSnapshot,
    QuestionInput,
)
from domain.parsing import ParseMetadata
from domain.qa_persistence import ConversationRecord, QARunVersions
from domain.retrieval import (
    CandidateCounts,
    LocatorKind,
    RetrievalError,
    RetrievalErrorCode,
    RetrievalMode,
    RetrievalProfileV1,
    SearchDiagnostics,
    SearchHit,
    SearchHitSummary,
    SearchLocator,
    SearchRequest,
    SearchResult,
)
from model_gateway import CapabilityAlias, ChatRequest, ChatResponse, ModelUsage

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPOSITORY_ROOT / "cases/evals/configs/grounded-answer-v1.schema.json"
PROMPT_PATH = REPOSITORY_ROOT / "cases/evals/prompts/grounded-qa-v1-provisional.txt"
SPACE_ID = UUID(int=1)
SOURCE_ID = UUID(int=2)
DOCUMENT_ID = UUID(int=3)
VERSION_ID = UUID(int=4)
CHUNK_ID = UUID(int=5)


class StaticSearchService:
    def __init__(self, result: SearchResult | RetrievalError) -> None:
        self._result = result
        self.requests: list[SearchRequest] = []

    async def search(self, request: SearchRequest, _profile: RetrievalProfileV1) -> SearchResult:
        self.requests.append(request)
        if isinstance(self._result, RetrievalError):
            raise self._result
        return self._result


class StaticTargets:
    async def get_target(self, query: CitationTargetQuery) -> CitationTargetSnapshot | None:
        if (
            query.space_id,
            query.source_id,
            query.document_id,
            query.version_id,
            query.chunk_id,
        ) != (SPACE_ID, SOURCE_ID, DOCUMENT_ID, VERSION_ID, CHUNK_ID):
            return None
        raw = b"synthetic evidence"
        return CitationTargetSnapshot(
            query=query,
            current_version_id=VERSION_ID,
            locators=(SearchLocator(LocatorKind.LINES, 1, 2),),
            blob_hash=hashlib.sha256(raw).hexdigest(),
            storage_key="repository_fixture/synthetic.txt",
            content_kind=CitationContentKind.TEXT,
            metadata=ParseMetadata(
                file_name="synthetic.txt",
                file_size=len(raw),
                mime_type="text/plain",
                encoding="utf-8",
            ),
        )


class StructuredChatGateway:
    async def chat(
        self, request: ChatRequest, *, capability: CapabilityAlias = CapabilityAlias.FAST_CHAT
    ) -> ChatResponse:
        assert capability is CapabilityAlias.FAST_CHAT
        evidence_ids = re.findall(r'<evidence id="([^"]+)"', request.messages[-1].content)
        assert len(evidence_ids) == 1
        response = json.dumps(
            {
                "schema_version": "grounded-answer-v1",
                "result_type": "answer",
                "answer": "Synthetic evidence supports this answer.",
                "claims": [
                    {
                        "claim_id": "c1",
                        "text": "Synthetic evidence supports this answer.",
                        "evidence_ids": evidence_ids,
                    }
                ],
                "limitations": ["Synthetic fixture only."],
            }
        )
        return ChatResponse(
            text=response,
            finish_reason="stop",
            usage=ModelUsage(input_tokens=4, output_tokens=5),
            capability=CapabilityAlias.FAST_CHAT,
            latency_ms=1.0,
        )


def _hit() -> SearchHit:
    text = "Synthetic evidence supports this answer."
    return SearchHit(
        chunk_id=CHUNK_ID,
        version_id=VERSION_ID,
        document_id=DOCUMENT_ID,
        source_id=SOURCE_ID,
        source_key="repository_fixture/synthetic",
        text=text,
        chunk_hash="a" * 64,
        safe_summary=SearchHitSummary(
            chunk_hash="a" * 64,
            text_length=len(text),
            locator_count=1,
        ),
        locators=(SearchLocator(LocatorKind.LINES, 1, 2),),
        final_rank=1,
    )


def _search_result() -> SearchResult:
    return SearchResult(
        hits=(_hit(),),
        diagnostics=SearchDiagnostics(
            requested_mode=RetrievalMode.HYBRID_RERANK,
            executed_mode=RetrievalMode.HYBRID_RERANK,
            profile_version="retrieval-profile-v1",
            embedding_version="embedding-v1",
            reranker_version="reranker-v1",
            keyword_index_version="fts-v1",
            dense_index_version="dense-v1",
            candidate_counts=CandidateCounts(final=1),
            stage_timings=(),
        ),
    )


def _profile() -> GroundedQAExecutionProfile:
    return GroundedQAExecutionProfile(
        planning=QAPlanningProfileV1(),
        retrieval=RetrievalProfileV1(
            profile_version="retrieval-profile-v1", embedding_version="embedding-v1"
        ),
    )


def _versions(profile: GroundedQAExecutionProfile) -> QARunVersions:
    return QARunVersions(
        skill_version="provisional",
        profile_version=profile.planning.profile_id,
        retrieval_profile_version=profile.retrieval.profile_version,
        model_identity="fake-fast-chat-v1",
        prompt_version="grounded-qa-v1-provisional",
        output_schema_version="grounded-answer-v1",
        corpus_version="v0-provisional",
        dataset_version="knowledge-qa-v0-provisional",
    )


def _service(search_service: StaticSearchService) -> GroundedQAService:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    generator = GroundedAnswerGenerator(
        gateway=StructuredChatGateway(),
        parser=StructuredAnswerParser(schema),
        verifier=EvidenceVerifier(StaticTargets()),
        profile=QAGenerationProfileV1(),
        prompt_contract=PROMPT_PATH.read_text(encoding="utf-8"),
        corpus_version="v0-provisional",
        dataset_version="knowledge-qa-v0-provisional",
    )
    return GroundedQAService(
        repository=InMemoryGroundedQARepository(),
        planner=QueryPlanner(),
        search=QASearchCoordinator(search_service),
        evidence_binding=EvidenceBindingService(),
        context_builder=ContextBuilder(),
        generator=generator,
    )


async def _submitted_run(
    service: GroundedQAService, profile: GroundedQAExecutionProfile
) -> tuple[ConversationRecord, UUID]:
    conversation = await service.create_conversation(
        ConversationRecord(space_id=SPACE_ID, owner_id="synthetic-user")
    )
    run = await service.submit(
        QuestionInput(
            question="What does the synthetic fixture support?",
            space_id=SPACE_ID,
            caller_id="synthetic-user",
            conversation_id=conversation.conversation_id,
            idempotency_key="question-1",
        ),
        versions=_versions(profile),
    )
    return conversation, run.run_id


@pytest.mark.asyncio
async def test_application_port_submits_idempotently_and_publishes_a_grounded_answer() -> None:
    profile = _profile()
    search_service = StaticSearchService(_search_result())
    service = _service(search_service)
    conversation, run_id = await _submitted_run(service, profile)

    replayed = await service.submit(
        QuestionInput(
            question="What does the synthetic fixture support?",
            space_id=SPACE_ID,
            caller_id="synthetic-user",
            conversation_id=conversation.conversation_id,
            idempotency_key="question-1",
        ),
        versions=_versions(profile),
    )
    assert replayed.run_id == run_id

    completed = await service.execute(run_id, profile=profile)

    assert completed.status.value == "completed"
    assert completed.result is not None
    assert completed.result.answer is not None
    assert completed.result.answer.text == "Synthetic evidence supports this answer."
    assert completed.usage.model_calls == 1
    assert len(search_service.requests) == 1
    assert search_service.requests[0].space_id == SPACE_ID


@pytest.mark.asyncio
async def test_application_port_keeps_retrieval_failure_distinct_from_refusal() -> None:
    profile = _profile()
    service = _service(
        StaticSearchService(
            RetrievalError(
                RetrievalErrorCode.RETRIEVAL_TIMEOUT,
                "synthetic timeout",
                retryable=True,
            )
        )
    )
    _conversation, run_id = await _submitted_run(service, profile)

    failed = await service.execute(run_id, profile=profile)

    assert failed.status.value == "failed"
    assert failed.error_code == "QA_RETRIEVAL_FAILED"
    assert failed.result is None


@pytest.mark.asyncio
async def test_application_port_honors_persisted_cancellation_before_execution() -> None:
    profile = _profile()
    service = _service(StaticSearchService(_search_result()))
    _conversation, run_id = await _submitted_run(service, profile)

    await service.request_cancel(run_id)
    cancelled = await service.execute(run_id, profile=profile)

    assert cancelled.status.value == "cancelled"
    assert cancelled.error_code == "QA_CANCELLED"
