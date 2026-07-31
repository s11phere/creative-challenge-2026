from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest
from application.qa.persistence import InMemoryGroundedQARepository
from domain.grounded_qa import (
    Citation,
    CitationStatus,
    Claim,
    ConflictNotice,
    EvidenceCandidate,
    GroundedAnswer,
    QAAttempt,
    QAContractError,
    QAError,
    QAErrorCode,
    QAEvent,
    QAOutcome,
    QAResult,
    QAStatus,
    Refusal,
    RefusalReason,
    next_qa_attempt,
    transition_qa_status,
)
from domain.qa_persistence import (
    CitationRecord,
    ConversationRecord,
    EvidenceRecord,
    FeedbackDecision,
    FeedbackRecord,
    MessageRecord,
    MessageRole,
    QAPhase,
    QAPhaseTiming,
    QARunRecord,
    QARunUsage,
    QARunVersions,
)
from domain.retrieval import LocatorKind, SearchLocator

SPACE_ID = UUID(int=1)
OTHER_SPACE_ID = UUID(int=2)
CONVERSATION_ID = UUID(int=10)
QUESTION_ID = UUID(int=11)
RUN_ID = UUID(int=12)
ATTEMPT_ID = UUID(int=13)
EVIDENCE_ID = UUID(int=14)
SOURCE_ID = UUID(int=15)
DOCUMENT_ID = UUID(int=16)
VERSION_ID = UUID(int=17)
CHUNK_ID = UUID(int=18)
ANSWER_MESSAGE_ID = UUID(int=19)


def _versions() -> QARunVersions:
    return QARunVersions(
        skill_version="knowledge_qa-v1",
        profile_version="grounded-qa-provisional-v1",
        retrieval_profile_version="stage3-default-pending-formal-freeze",
        model_identity="fake-fast-chat-v1",
        prompt_version="grounded-qa-v1-provisional",
        output_schema_version="grounded-answer-v1",
        corpus_version="v0-provisional",
        dataset_version="knowledge-qa-v0-provisional",
    )


def _candidate(*, space_id: UUID = SPACE_ID) -> EvidenceCandidate:
    return EvidenceCandidate(
        evidence_id=EVIDENCE_ID,
        space_id=space_id,
        source_id=SOURCE_ID,
        document_id=DOCUMENT_ID,
        version_id=VERSION_ID,
        chunk_id=CHUNK_ID,
        source_key="repository_fixture/source",
        locators=(SearchLocator(LocatorKind.LINES, 1, 1),),
        excerpt_sha256="a" * 64,
        matched=True,
        context_only=False,
    )


def _citation() -> Citation:
    return Citation(
        evidence_id=EVIDENCE_ID,
        space_id=SPACE_ID,
        source_id=SOURCE_ID,
        document_id=DOCUMENT_ID,
        version_id=VERSION_ID,
        chunk_id=CHUNK_ID,
        locator=SearchLocator(LocatorKind.LINES, 1, 1),
        excerpt_sha256="a" * 64,
        status=CitationStatus.VALID,
    )


def _answer() -> QAResult:
    return QAResult(
        outcome=QAOutcome.ANSWER,
        answer=GroundedAnswer(
            text="Supported answer.",
            claims=(Claim("c1", "Supported answer.", (EVIDENCE_ID,)),),
            citations=(_citation(),),
        ),
    )


async def _seed_run(
    repo: InMemoryGroundedQARepository,
    *,
    space_id: UUID = SPACE_ID,
    conversation_id: UUID = CONVERSATION_ID,
    run_id: UUID = RUN_ID,
    attempt_id: UUID = ATTEMPT_ID,
    idempotency_key: str = "question-1",
    verify: bool = True,
) -> tuple[ConversationRecord, MessageRecord, QARunRecord]:
    conversation = ConversationRecord(
        conversation_id=conversation_id,
        space_id=space_id,
        owner_id="owner-1",
    )
    await repo.create_conversation(conversation)
    question = MessageRecord(
        message_id=QUESTION_ID,
        conversation_id=conversation_id,
        space_id=space_id,
        role=MessageRole.USER,
        content="What does the fixture say?",
        idempotency_key=idempotency_key,
    )
    await repo.append_message(question)
    run = QARunRecord(
        run_id=run_id,
        attempt=QAAttempt(run_id=run_id, attempt_id=attempt_id),
        conversation_id=conversation_id,
        question_message_id=QUESTION_ID,
        space_id=space_id,
        caller_id="owner-1",
        idempotency_key=idempotency_key,
        versions=_versions(),
    )
    await repo.create_run(run)
    if verify:
        await repo.transition_run(run_id, QAEvent.QUEUE)
        await repo.transition_run(run_id, QAEvent.START)
        await repo.transition_run(run_id, QAEvent.VERIFY)
    return conversation, question, run


async def _save_evidence(repo: InMemoryGroundedQARepository, run: QARunRecord) -> None:
    await repo.save_evidence(
        EvidenceRecord(
            run_id=run.run_id,
            attempt_id=run.attempt.attempt_id,
            candidate=_candidate(space_id=run.space_id),
        )
    )


def _answer_message(run: QARunRecord) -> MessageRecord:
    return MessageRecord(
        message_id=ANSWER_MESSAGE_ID,
        conversation_id=run.conversation_id,
        space_id=run.space_id,
        role=MessageRole.ASSISTANT,
        content="Supported answer.",
        run_id=run.run_id,
    )


def _citation_record(run: QARunRecord) -> CitationRecord:
    return CitationRecord(
        run_id=run.run_id,
        attempt_id=run.attempt.attempt_id,
        message_id=ANSWER_MESSAGE_ID,
        citation=_citation(),
    )


@pytest.mark.asyncio
async def test_conversation_run_and_evidence_are_space_scoped_and_idempotent() -> None:
    repo = InMemoryGroundedQARepository()
    _conversation, question, run = await _seed_run(repo)

    duplicate_question = await repo.append_message(replace(question, message_id=UUID(int=101)))
    assert duplicate_question.message_id == question.message_id
    assert (await repo.get_run(run.run_id)).attempt.attempt_id == run.attempt.attempt_id

    with pytest.raises(QAContractError, match="Space"):
        await repo.save_evidence(
            EvidenceRecord(
                run_id=run.run_id,
                attempt_id=run.attempt.attempt_id,
                candidate=_candidate(space_id=OTHER_SPACE_ID),
            )
        )


@pytest.mark.asyncio
async def test_conversation_creation_is_idempotent_only_for_the_same_owner_and_space() -> None:
    repo = InMemoryGroundedQARepository()
    conversation = ConversationRecord(
        conversation_id=CONVERSATION_ID,
        space_id=SPACE_ID,
        owner_id="owner-1",
    )
    assert await repo.create_conversation(conversation) == conversation
    assert (
        await repo.create_conversation(replace(conversation, updated_at=datetime.now(UTC)))
        == conversation
    )

    with pytest.raises(QAContractError, match="another owner"):
        await repo.create_conversation(replace(conversation, owner_id="owner-2"))
    with pytest.raises(QAContractError, match="another owner"):
        await repo.create_conversation(replace(conversation, space_id=OTHER_SPACE_ID))


@pytest.mark.asyncio
async def test_invalid_terminal_publication_leaves_no_partial_message_or_result() -> None:
    repo = InMemoryGroundedQARepository()
    _conversation, _question, run = await _seed_run(repo)
    await _save_evidence(repo, run)

    with pytest.raises(QAContractError, match="incomplete"):
        await repo.publish_terminal(
            run_id=run.run_id,
            result=_answer(),
            answer_message=_answer_message(run),
        )

    stored = await repo.get_run(run.run_id)
    assert stored is not None
    assert stored.status is QAStatus.VERIFYING
    assert stored.result is None
    assert await repo.get_message(ANSWER_MESSAGE_ID) is None
    assert await repo.list_citations(run.attempt.attempt_id) == ()


@pytest.mark.asyncio
async def test_terminal_answer_is_atomic_and_replayed_idempotently() -> None:
    repo = InMemoryGroundedQARepository()
    _conversation, _question, run = await _seed_run(repo)
    await _save_evidence(repo, run)
    message = _answer_message(run)
    citation = _citation_record(run)
    result = _answer()
    usage = QARunUsage(
        input_tokens=12,
        output_tokens=8,
        model_calls=1,
        repair_attempts=0,
        model_latency_ms=4.5,
        phase_timings=(QAPhaseTiming(QAPhase.GENERATION, 4.5),),
    )
    assert (await repo.save_usage(run.run_id, usage)).usage == usage

    published = await repo.publish_terminal(
        run_id=run.run_id,
        result=result,
        answer_message=message,
        citations=(citation,),
    )
    replayed = await repo.publish_terminal(
        run_id=run.run_id,
        result=result,
        answer_message=message,
        citations=(citation,),
    )

    assert published.status is QAStatus.COMPLETED
    assert published.usage == usage
    assert replayed == published
    assert await repo.get_message(message.message_id) == message
    assert await repo.list_citations(run.attempt.attempt_id) == (citation,)

    with pytest.raises(QAContractError, match="different terminal"):
        await repo.publish_terminal(
            run_id=run.run_id,
            result=QAResult(
                outcome=QAOutcome.REFUSE,
                refusal=Refusal(RefusalReason.INSUFFICIENT_EVIDENCE, "No evidence."),
            ),
            answer_message=replace(message, content="No evidence."),
        )


@pytest.mark.asyncio
async def test_usage_updates_are_monotonic_and_terminal_usage_is_immutable() -> None:
    repo = InMemoryGroundedQARepository()
    _conversation, _question, run = await _seed_run(repo)
    usage = QARunUsage(input_tokens=5, model_calls=1)
    await repo.save_usage(run.run_id, usage)

    with pytest.raises(QAContractError, match="monotonic"):
        await repo.save_usage(run.run_id, QARunUsage(input_tokens=4, model_calls=1))

    await _save_evidence(repo, run)
    await repo.publish_terminal(
        run_id=run.run_id,
        result=_answer(),
        answer_message=_answer_message(run),
        citations=(_citation_record(run),),
    )
    with pytest.raises(QAContractError, match="terminal"):
        await repo.save_usage(run.run_id, QARunUsage(input_tokens=6, model_calls=1))


@pytest.mark.asyncio
async def test_terminal_replay_ignores_transport_timestamps_but_not_business_content() -> None:
    repo = InMemoryGroundedQARepository()
    _conversation, _question, run = await _seed_run(repo)
    await _save_evidence(repo, run)
    message = _answer_message(run)
    citation = _citation_record(run)
    result = _answer()

    await repo.publish_terminal(
        run_id=run.run_id,
        result=result,
        answer_message=message,
        citations=(citation,),
    )
    replayed = await repo.publish_terminal(
        run_id=run.run_id,
        result=result,
        answer_message=replace(message, created_at=datetime.now(UTC)),
        citations=(replace(citation, created_at=datetime.now(UTC)),),
    )

    assert replayed.status is QAStatus.COMPLETED


@pytest.mark.asyncio
async def test_non_publishable_citation_is_rejected_without_partial_terminal_state() -> None:
    repo = InMemoryGroundedQARepository()
    _conversation, _question, run = await _seed_run(repo)
    await _save_evidence(repo, run)
    invalid_citation = replace(_citation(), status=CitationStatus.WITHDRAWN)
    result = QAResult(
        outcome=QAOutcome.ANSWER,
        answer=GroundedAnswer(
            text="Supported answer.",
            claims=(Claim("c1", "Supported answer.", (EVIDENCE_ID,)),),
            citations=(invalid_citation,),
        ),
    )
    citation = replace(_citation_record(run), citation=invalid_citation)

    with pytest.raises(QAContractError, match="non-publishable"):
        await repo.publish_terminal(
            run_id=run.run_id,
            result=result,
            answer_message=_answer_message(run),
            citations=(citation,),
        )

    stored = await repo.get_run(run.run_id)
    assert stored is not None
    assert stored.status is QAStatus.VERIFYING
    assert await repo.get_message(ANSWER_MESSAGE_ID) is None


@pytest.mark.asyncio
async def test_cancel_and_terminal_publication_are_serialized_without_partial_state() -> None:
    repo = InMemoryGroundedQARepository()
    _conversation, _question, run = await _seed_run(repo)
    await _save_evidence(repo, run)
    message = _answer_message(run)
    citation = _citation_record(run)
    result = _answer()

    outcomes = await asyncio.gather(
        repo.request_cancel(run.run_id),
        repo.publish_terminal(
            run_id=run.run_id,
            result=result,
            answer_message=message,
            citations=(citation,),
        ),
        return_exceptions=True,
    )
    stored = await repo.get_run(run.run_id)
    assert stored is not None
    if stored.status is QAStatus.COMPLETED:
        assert stored.result is not None
        assert await repo.get_message(message.message_id) == message
    else:
        assert stored.status is QAStatus.CANCEL_REQUESTED
        assert stored.result is None
        assert await repo.get_message(message.message_id) is None
    assert any(isinstance(outcome, QARunRecord) for outcome in outcomes)


@pytest.mark.asyncio
async def test_failed_attempt_retries_with_new_identity_without_reopening() -> None:
    repo = InMemoryGroundedQARepository()
    _conversation, _question, run = await _seed_run(repo, verify=False)
    failed = await repo.transition_run(
        run.run_id,
        QAEvent.FAIL,
        error_code=QAErrorCode.MODEL_FAILED.value,
    )
    retry_attempt = next_qa_attempt(
        failed.attempt,
        QAError(QAErrorCode.MODEL_FAILED, "provider unavailable", retryable=True),
        attempt_id=UUID(int=103),
    )
    retry = replace(
        failed,
        attempt=retry_attempt,
        idempotency_key="question-1-retry",
        status=QAStatus.CREATED,
        error_code=None,
        result=None,
        answer_message_id=None,
    )
    created = await repo.create_run(retry)

    assert created.attempt.attempt_id == UUID(int=103)
    assert (await repo.get_run(run.run_id)).attempt.attempt_id == UUID(int=103)
    with pytest.raises(QAContractError, match="invalid"):
        transition_qa_status(failed.status, QAEvent.START)
    await repo.transition_run(run.run_id, QAEvent.QUEUE)
    started = await repo.transition_run(run.run_id, QAEvent.START)
    assert started.status is QAStatus.RUNNING


@pytest.mark.asyncio
async def test_feedback_is_bound_to_published_message_and_is_idempotent() -> None:
    repo = InMemoryGroundedQARepository()
    conversation, _question, run = await _seed_run(repo)
    await _save_evidence(repo, run)
    message = _answer_message(run)
    await repo.publish_terminal(
        run_id=run.run_id,
        result=_answer(),
        answer_message=message,
        citations=(_citation_record(run),),
    )
    feedback = FeedbackRecord(
        conversation_id=conversation.conversation_id,
        message_id=message.message_id,
        run_id=run.run_id,
        attempt_id=run.attempt.attempt_id,
        space_id=SPACE_ID,
        caller_id="owner-1",
        idempotency_key="feedback-1",
        decision=FeedbackDecision.POSITIVE,
    )
    first = await repo.submit_feedback(feedback)
    second = await repo.submit_feedback(replace(feedback, feedback_id=UUID(int=104)))
    assert first == second
    assert first.review_status.value == "pending_review"

    with pytest.raises(QAContractError, match="published QA result"):
        await repo.submit_feedback(replace(feedback, message_id=QUESTION_ID))


@pytest.mark.asyncio
async def test_duplicate_citation_records_cannot_publish_a_partial_answer() -> None:
    repo = InMemoryGroundedQARepository()
    _conversation, _question, run = await _seed_run(repo)
    first = _candidate()
    second = replace(
        first,
        evidence_id=UUID(int=201),
        document_id=UUID(int=202),
        version_id=UUID(int=203),
        chunk_id=UUID(int=204),
        excerpt_sha256="b" * 64,
    )
    for candidate in (first, second):
        await repo.save_evidence(
            EvidenceRecord(
                run_id=run.run_id,
                attempt_id=run.attempt.attempt_id,
                candidate=candidate,
            )
        )
    second_citation = Citation(
        evidence_id=second.evidence_id,
        space_id=second.space_id,
        source_id=second.source_id,
        document_id=second.document_id,
        version_id=second.version_id,
        chunk_id=second.chunk_id,
        locator=second.locators[0],
        excerpt_sha256=second.excerpt_sha256,
    )
    result = QAResult(
        outcome=QAOutcome.ANSWER,
        answer=GroundedAnswer(
            text="First.\nSecond.",
            claims=(
                Claim("c1", "First.", (first.evidence_id,)),
                Claim("c2", "Second.", (second.evidence_id,)),
            ),
            citations=(_citation(), second_citation),
        ),
    )
    message = replace(_answer_message(run), content="First.\nSecond.")
    duplicate = _citation_record(run)

    with pytest.raises(QAContractError, match="duplicate Citation"):
        await repo.publish_terminal(
            run_id=run.run_id,
            result=result,
            answer_message=message,
            citations=(duplicate, duplicate),
        )

    assert await repo.get_message(message.message_id) is None


@pytest.mark.asyncio
async def test_single_source_conflict_is_rejected_at_the_repository_boundary() -> None:
    repo = InMemoryGroundedQARepository()
    _conversation, _question, run = await _seed_run(repo)
    first = _candidate()
    second = replace(
        first,
        evidence_id=UUID(int=211),
        document_id=UUID(int=212),
        version_id=UUID(int=213),
        chunk_id=UUID(int=214),
        excerpt_sha256="c" * 64,
    )
    for candidate in (first, second):
        await repo.save_evidence(
            EvidenceRecord(
                run_id=run.run_id,
                attempt_id=run.attempt.attempt_id,
                candidate=candidate,
            )
        )
    result = QAResult(
        outcome=QAOutcome.CONFLICT,
        conflict=ConflictNotice(
            (first.evidence_id, second.evidence_id),
            "Synthetic statements disagree.",
        ),
    )
    message = replace(_answer_message(run), content="Synthetic statements disagree.")

    with pytest.raises(QAContractError, match="at least two sources"):
        await repo.publish_terminal(
            run_id=run.run_id,
            result=result,
            answer_message=message,
        )

    assert await repo.get_message(message.message_id) is None
