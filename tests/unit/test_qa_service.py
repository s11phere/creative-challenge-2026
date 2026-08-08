from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from application.qa.context_builder import ContextBuilder
from application.qa.evidence import EvidenceBindingService, EvidenceVerifier
from application.qa.generation import GroundedAnswerGenerator, StructuredAnswerParser
from application.qa.persistence import InMemoryGroundedQARepository
from application.qa.profile import QAGenerationProfileV1, QAPlanningProfileV1
from application.qa.query_planning import QASearchCoordinator, QueryPlanner
from application.qa.service import AgentRetrievalPlan, GroundedQAExecutionProfile, GroundedQAService
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


def test_agent_retrieval_preferences_are_clamped_to_trusted_server_limits() -> None:
    trusted = QAPlanningProfileV1()
    effective = AgentRetrievalPlan(
        max_evidence_items=100_000,
        max_input_tokens=100_000,
        max_tokens_per_evidence=100_000,
        max_evidence_per_source=100_000,
        max_chunks_per_document=100_000,
    ).apply(trusted)

    assert effective.max_evidence_items == trusted.max_evidence_items
    assert effective.max_input_tokens == trusted.max_input_tokens
    assert effective.max_tokens_per_evidence == trusted.max_tokens_per_evidence
    assert effective.max_evidence_per_source == trusted.max_evidence_per_source
    assert effective.max_chunks_per_document == trusted.max_chunks_per_document


class StaticSearchService:
    def __init__(self, result: SearchResult | RetrievalError) -> None:
        self._result = result
        self.requests: list[SearchRequest] = []

    async def search(self, request: SearchRequest, _profile: RetrievalProfileV1) -> SearchResult:
        self.requests.append(request)
        if isinstance(self._result, RetrievalError):
            raise self._result
        return self._result


class StaticRewriter:
    def __init__(self, queries: tuple[str, ...]) -> None:
        self._queries = queries
        self.calls = 0

    async def rewrite(self, _question: QuestionInput, *, max_queries: int) -> tuple[str, ...]:
        assert max_queries >= len(self._queries)
        self.calls += 1
        return self._queries


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


def _service(
    search_service: StaticSearchService, *, planner: QueryPlanner | None = None
) -> GroundedQAService:
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
        planner=planner or QueryPlanner(),
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
async def test_agent_supplied_queries_skip_the_generic_rewrite() -> None:
    # When the agent already chooses retrieval queries, the generic rewrite LLM
    # call is redundant and is skipped; the merged plan keeps the original
    # question plus the agent's queries.
    base_profile = _profile()
    profile = replace(
        base_profile,
        planning=replace(base_profile.planning, rewrite_enabled=True, max_subqueries=4),
    )
    rewriter = StaticRewriter(("llm query one", "llm query two"))
    search_service = StaticSearchService(_search_result())
    service = _service(search_service, planner=QueryPlanner(rewriter))
    _conversation, run_id = await _submitted_run(service, profile)

    inspected = await service.inspect_retrieval(
        run_id,
        profile=profile,
        agent_plan=AgentRetrievalPlan(additional_queries=("agent query",)),
    )

    assert rewriter.calls == 0
    assert len(inspected.diagnostics) == 2
    assert tuple(request.query for request in search_service.requests) == (
        "What does the synthetic fixture support?",
        "agent query",
    )


@pytest.mark.asyncio
async def test_execute_reuses_the_plan_from_an_earlier_inspect() -> None:
    # An agent flow that inspects retrieval and then runs grounded_qa without
    # adding queries must not re-run the rewrite LLM or duplicate the search.
    base_profile = _profile()
    profile = replace(
        base_profile,
        planning=replace(base_profile.planning, rewrite_enabled=True, max_subqueries=4),
    )
    rewriter = StaticRewriter(("llm query one",))
    search_service = StaticSearchService(_search_result())
    service = _service(search_service, planner=QueryPlanner(rewriter))
    _conversation, run_id = await _submitted_run(service, profile)

    inspected = await service.inspect_retrieval(
        run_id, profile=profile, agent_plan=AgentRetrievalPlan()
    )
    assert len(inspected.diagnostics) == 2

    completed = await service.execute(run_id, profile=profile, agent_plan=AgentRetrievalPlan())

    assert completed.status.value == "completed"
    assert rewriter.calls == 1
    # inspect searched the 2-query plan and execute re-searched the same reused
    # plan; the rewrite LLM ran exactly once instead of once per tool call.
    assert len(search_service.requests) == 4
    assert tuple(request.query for request in search_service.requests) == (
        "What does the synthetic fixture support?",
        "llm query one",
        "What does the synthetic fixture support?",
        "llm query one",
    )


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
async def test_comparison_answer_requires_citations_from_two_sources() -> None:
    profile = _profile()
    service = _service(StaticSearchService(_search_result()))
    conversation = await service.create_conversation(
        ConversationRecord(space_id=SPACE_ID, owner_id="synthetic-user")
    )
    submitted = await service.submit(
        QuestionInput(
            question="Compare the selected sources.",
            space_id=SPACE_ID,
            caller_id="synthetic-user",
            conversation_id=conversation.conversation_id,
            idempotency_key="comparison-1",
        ),
        versions=_versions(profile),
    )

    refused = await service.execute(submitted.run_id, profile=profile)

    assert refused.status.value == "refused"
    assert refused.result is not None
    assert refused.result.refusal is not None
    assert refused.result.refusal.code.value == "REFUSED_INSUFFICIENT_EVIDENCE"


@pytest.mark.asyncio
async def test_application_port_honors_persisted_cancellation_before_execution() -> None:
    profile = _profile()
    service = _service(StaticSearchService(_search_result()))
    _conversation, run_id = await _submitted_run(service, profile)

    await service.request_cancel(run_id)
    cancelled = await service.execute(run_id, profile=profile)

    assert cancelled.status.value == "cancelled"
    assert cancelled.error_code == "QA_CANCELLED"
