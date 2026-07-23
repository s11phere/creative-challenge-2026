from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from domain.agent_runtime import AgentRun, AgentRunContext, RunEvent
from domain.grounded_qa import (
    Citation,
    CitationStatus,
    Claim,
    ConflictNotice,
    EvidenceCandidate,
    GroundedAnswer,
    InvalidQATransitionError,
    QAContractError,
    QAError,
    QAErrorCode,
    QAEvent,
    QAOutcome,
    QAResult,
    QAStatus,
    QueryPlan,
    QuestionInput,
    Refusal,
    RefusalReason,
    project_qa_status,
    transition_qa_status,
    validate_answer_citations,
)
from domain.retrieval import LocatorKind, SearchLocator

SPACE_ID = UUID(int=1)
SOURCE_ID = UUID(int=2)
DOCUMENT_ID = UUID(int=3)
VERSION_ID = UUID(int=4)
CHUNK_ID = UUID(int=5)
EVIDENCE_ID = UUID(int=6)


def _evidence(*, evidence_id: UUID = EVIDENCE_ID) -> EvidenceCandidate:
    return EvidenceCandidate(
        evidence_id=evidence_id,
        space_id=SPACE_ID,
        source_id=SOURCE_ID,
        document_id=DOCUMENT_ID,
        version_id=VERSION_ID,
        chunk_id=CHUNK_ID,
        source_key="test/source",
        locators=(SearchLocator(LocatorKind.LINES, 10, 12),),
        excerpt_sha256="a" * 64,
        matched=True,
        context_only=False,
    )


def _citation(*, evidence_id: UUID = EVIDENCE_ID, space_id: UUID = SPACE_ID) -> Citation:
    return Citation(
        evidence_id=evidence_id,
        space_id=space_id,
        source_id=SOURCE_ID,
        document_id=DOCUMENT_ID,
        version_id=VERSION_ID,
        chunk_id=CHUNK_ID,
        locator=SearchLocator(LocatorKind.LINES, 10, 12),
        excerpt_sha256="a" * 64,
        status=CitationStatus.VALID,
    )


def _answer(*, evidence_id: UUID = EVIDENCE_ID) -> GroundedAnswer:
    return GroundedAnswer(
        text="Supported fact",
        claims=(Claim(claim_id="c1", text="Supported fact", evidence_ids=(evidence_id,)),),
        citations=(_citation(evidence_id=evidence_id),),
    )


def test_question_and_query_plan_keep_a_normalized_original_query() -> None:
    question = QuestionInput(question="  C++\n vector? ", space_id=SPACE_ID, caller_id="user-1")
    plan = QueryPlan(
        original_question=question.question,
        queries=(question.question, "vector growth"),
        rewrite_applied=True,
    )

    assert question.question == "C++ vector?"
    assert plan.queries[0] == question.question
    with pytest.raises(QAContractError, match="first query"):
        QueryPlan(original_question=question.question, queries=("vector growth",))


def test_grounded_answer_rejects_unreferenced_claim_and_duplicate_evidence_ids() -> None:
    with pytest.raises(QAContractError, match="reference evidence"):
        Claim(claim_id="c1", text="Unsupported", evidence_ids=())
    with pytest.raises(QAContractError, match="evidence IDs must be unique"):
        Claim(claim_id="c1", text="Duplicated", evidence_ids=(EVIDENCE_ID, EVIDENCE_ID))
    with pytest.raises(QAContractError, match="only cite evidence"):
        GroundedAnswer(
            text="Fact",
            claims=(Claim(claim_id="c1", text="Fact", evidence_ids=(EVIDENCE_ID,)),),
            citations=(_citation(evidence_id=UUID(int=99)),),
        )


def test_citation_validation_rejects_forged_or_cross_space_identity() -> None:
    answer = _answer()
    validate_answer_citations(answer, (_evidence(),), space_id=SPACE_ID)

    with pytest.raises(QAContractError, match="outside the current run"):
        validate_answer_citations(
            _answer(evidence_id=UUID(int=99)), (_evidence(),), space_id=SPACE_ID
        )
    with pytest.raises(QAContractError, match="requested Space"):
        validate_answer_citations(
            GroundedAnswer(
                text="Fact",
                claims=(Claim(claim_id="c1", text="Fact", evidence_ids=(EVIDENCE_ID,)),),
                citations=(_citation(space_id=UUID(int=99)),),
            ),
            (_evidence(),),
            space_id=SPACE_ID,
        )
    duplicate_evidence = (_evidence(), _evidence())
    with pytest.raises(QAContractError, match="Run Evidence IDs must be unique"):
        validate_answer_citations(answer, duplicate_evidence, space_id=SPACE_ID)


def test_result_payloads_keep_refusal_conflict_and_failure_distinct() -> None:
    assert QAResult(outcome=QAOutcome.ANSWER, answer=_answer()).outcome is QAOutcome.ANSWER
    assert (
        QAResult(
            outcome=QAOutcome.REFUSE,
            refusal=Refusal(RefusalReason.INSUFFICIENT_EVIDENCE, "No supporting evidence."),
        ).outcome
        is QAOutcome.REFUSE
    )
    assert (
        QAResult(
            outcome=QAOutcome.CONFLICT,
            conflict=ConflictNotice((EVIDENCE_ID, UUID(int=7)), "Sources disagree."),
        ).outcome
        is QAOutcome.CONFLICT
    )
    assert (
        QAResult(outcome=QAOutcome.FAILED, error_code=QAErrorCode.MODEL_FAILED).outcome
        is QAOutcome.FAILED
    )
    with pytest.raises(QAContractError, match="exactly one payload"):
        QAResult(
            outcome=QAOutcome.REFUSE,
            refusal=Refusal(RefusalReason.INSUFFICIENT_EVIDENCE, "No evidence."),
            error_code=QAErrorCode.RETRIEVAL_FAILED,
        )
    assert QAError(QAErrorCode.MODEL_FAILED, "Model unavailable.").code is QAErrorCode.MODEL_FAILED


def test_qa_lifecycle_is_terminal_safe_and_maps_refusal_separately() -> None:
    status = QAStatus.CREATED
    for event in (QAEvent.QUEUE, QAEvent.START, QAEvent.VERIFY, QAEvent.REFUSE):
        status = transition_qa_status(status, event)
    assert status is QAStatus.REFUSED
    with pytest.raises(InvalidQATransitionError):
        transition_qa_status(status, QAEvent.START)

    cancelled = transition_qa_status(QAStatus.RUNNING, QAEvent.REQUEST_CANCEL)
    assert transition_qa_status(cancelled, QAEvent.CANCEL) is QAStatus.CANCELLED
    assert transition_qa_status(QAStatus.VERIFYING, QAEvent.TIMEOUT) is QAStatus.TIMED_OUT


def test_qa_status_projects_the_shared_agent_run_without_a_second_budget() -> None:
    run = AgentRun(
        context=AgentRunContext(
            run_id=uuid4(),
            space_id=SPACE_ID,
            skill_name="knowledge_qa",
            skill_version="1.0.0",
            skill_content_sha256="a" * 64,
            trace_id="trace-1",
            caller_id="user-1",
        )
    )
    assert project_qa_status(run, queued=True) is QAStatus.QUEUED
    verifying = (
        run.transition(RunEvent.START)
        .transition(RunEvent.RETRIEVE)
        .transition(RunEvent.EXECUTE)
        .transition(RunEvent.VERIFY)
    )
    assert project_qa_status(verifying) is QAStatus.VERIFYING
    completed = verifying.transition(RunEvent.COMPLETE)
    refusal = QAResult(
        outcome=QAOutcome.REFUSE,
        refusal=Refusal(RefusalReason.INSUFFICIENT_EVIDENCE, "No evidence."),
    )
    assert project_qa_status(completed, result=refusal) is QAStatus.REFUSED
