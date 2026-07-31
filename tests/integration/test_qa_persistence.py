from __future__ import annotations

import os
from uuid import UUID, uuid4

import pytest
from domain.grounded_qa import (
    Citation,
    Claim,
    EvidenceCandidate,
    GroundedAnswer,
    QAAttempt,
    QAEvent,
    QAOutcome,
    QAResult,
)
from domain.models import Space
from domain.qa_persistence import (
    CitationRecord,
    ConversationRecord,
    EvidenceRecord,
    FeedbackDecision,
    FeedbackRecord,
    MessageRecord,
    MessageRole,
    QARunRecord,
    QARunUsage,
    QARunVersions,
)
from domain.qa_sse import QAEventType
from domain.retrieval import LocatorKind, SearchLocator
from infrastructure.config import settings
from infrastructure.database import Database
from infrastructure.qa_persistence import PostgresGroundedQARepository, PostgresQAEventStore
from infrastructure.repositories import SpaceRepository

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION") != "1",
        reason="set RUN_INTEGRATION=1 with an isolated migrated PostgreSQL database",
    ),
]

SPACE_ID = UUID("10000000-0000-0000-0000-000000000001")
SOURCE_ID = UUID("10000000-0000-0000-0000-000000000002")
DOCUMENT_ID = UUID("10000000-0000-0000-0000-000000000003")
VERSION_ID = UUID("10000000-0000-0000-0000-000000000004")
CHUNK_ID = UUID("10000000-0000-0000-0000-000000000005")


def _versions() -> QARunVersions:
    return QARunVersions(
        skill_version="knowledge_qa-0.1.0-provisional",
        profile_version="qa-profile-v1",
        retrieval_profile_version="retrieval-profile-v1",
        model_identity="fake-fast-chat-v1",
        prompt_version="grounded-qa-v1-provisional",
        output_schema_version="grounded-answer-v1",
        corpus_version="local-live-provisional",
        dataset_version="knowledge-qa-v0-provisional",
    )


async def _database() -> Database:
    database = Database(settings.database_url)
    async with database.transaction() as session:
        repository = SpaceRepository(session)
        if await repository.get(SPACE_ID) is None:
            await repository.create(
                Space(id=SPACE_ID, name="QA persistence integration", owner_id="integration")
            )
    return database


async def _queued_run(
    repository: PostgresGroundedQARepository,
    *,
    key: str,
) -> tuple[ConversationRecord, QARunRecord]:
    conversation = await repository.create_conversation(
        ConversationRecord(space_id=SPACE_ID, owner_id="integration")
    )
    question = await repository.append_message(
        MessageRecord(
            conversation_id=conversation.conversation_id,
            space_id=SPACE_ID,
            role=MessageRole.USER,
            content="What is persisted?",
            idempotency_key=key,
        )
    )
    run_id = uuid4()
    created = await repository.create_run(
        QARunRecord(
            run_id=run_id,
            attempt=QAAttempt(run_id=run_id),
            conversation_id=conversation.conversation_id,
            question_message_id=question.message_id,
            space_id=SPACE_ID,
            caller_id="integration",
            idempotency_key=key,
            versions=_versions(),
        )
    )
    return conversation, await repository.transition_run(created.run_id, QAEvent.QUEUE)


@pytest.mark.asyncio
async def test_terminal_answer_events_and_feedback_survive_repository_restart() -> None:
    database = await _database()
    repository = PostgresGroundedQARepository(database)
    events = PostgresQAEventStore(database)
    conversation, queued = await _queued_run(repository, key=f"durable-answer-{uuid4()}")
    await events.append(queued.run_id, QAEventType.ACCEPTED, {"status": "queued"})
    running = await repository.transition_run(queued.run_id, QAEvent.START)
    locator = SearchLocator(LocatorKind.LINES, 4, 8)
    evidence = EvidenceCandidate(
        evidence_id=uuid4(),
        space_id=SPACE_ID,
        source_id=SOURCE_ID,
        document_id=DOCUMENT_ID,
        version_id=VERSION_ID,
        chunk_id=CHUNK_ID,
        source_key="integration/persistence",
        locators=(locator,),
        excerpt_sha256="a" * 64,
        matched=True,
        context_only=False,
    )
    await repository.save_evidence(
        EvidenceRecord(
            run_id=running.run_id,
            attempt_id=running.attempt.attempt_id,
            candidate=evidence,
        )
    )
    verifying = await repository.transition_run(running.run_id, QAEvent.VERIFY)
    citation = Citation(
        evidence_id=evidence.evidence_id,
        space_id=SPACE_ID,
        source_id=SOURCE_ID,
        document_id=DOCUMENT_ID,
        version_id=VERSION_ID,
        chunk_id=CHUNK_ID,
        locator=locator,
        excerpt_sha256=evidence.excerpt_sha256,
    )
    result = QAResult(
        QAOutcome.ANSWER,
        answer=GroundedAnswer(
            text="The durable result is persisted.",
            claims=(Claim("claim-1", "The durable result is persisted.", (evidence.evidence_id,)),),
            citations=(citation,),
        ),
    )
    message = MessageRecord(
        conversation_id=conversation.conversation_id,
        space_id=SPACE_ID,
        role=MessageRole.ASSISTANT,
        content="The durable result is persisted.",
        run_id=verifying.run_id,
    )
    await repository.save_usage(
        verifying.run_id, QARunUsage(input_tokens=10, output_tokens=6, model_calls=1)
    )
    completed = await repository.publish_terminal(
        run_id=verifying.run_id,
        result=result,
        answer_message=message,
        citations=(
            CitationRecord(
                run_id=verifying.run_id,
                attempt_id=verifying.attempt.attempt_id,
                message_id=message.message_id,
                citation=citation,
            ),
        ),
    )
    await events.append(completed.run_id, QAEventType.COMPLETED, {"status": "completed"})

    restarted_repository = PostgresGroundedQARepository(database)
    restarted_events = PostgresQAEventStore(database)
    restored = await restarted_repository.get_run(completed.run_id)
    assert restored is not None
    assert restored.result == result
    assert restored.answer_message_id == message.message_id
    assert len(await restarted_repository.list_evidence(restored.attempt.attempt_id)) == 1
    assert len(await restarted_repository.list_citations(restored.attempt.attempt_id)) == 1
    assert [event.event_type for event in await restarted_events.replay(restored.run_id)] == [
        QAEventType.ACCEPTED,
        QAEventType.COMPLETED,
    ]

    feedback = FeedbackRecord(
        conversation_id=conversation.conversation_id,
        message_id=message.message_id,
        run_id=restored.run_id,
        attempt_id=restored.attempt.attempt_id,
        space_id=SPACE_ID,
        caller_id="integration",
        idempotency_key="feedback-1",
        decision=FeedbackDecision.POSITIVE,
    )
    assert await restarted_repository.submit_feedback(feedback) == feedback
    assert await restarted_repository.submit_feedback(feedback) == feedback
    await database.dispose()


@pytest.mark.asyncio
async def test_recovery_requeues_interrupted_attempt_and_removes_unpublished_evidence() -> None:
    database = await _database()
    repository = PostgresGroundedQARepository(database)
    _conversation, queued = await _queued_run(repository, key=f"interrupted-run-{uuid4()}")
    running = await repository.transition_run(queued.run_id, QAEvent.START)
    await repository.save_evidence(
        EvidenceRecord(
            run_id=running.run_id,
            attempt_id=running.attempt.attempt_id,
            candidate=EvidenceCandidate(
                evidence_id=uuid4(),
                space_id=SPACE_ID,
                source_id=SOURCE_ID,
                document_id=DOCUMENT_ID,
                version_id=VERSION_ID,
                chunk_id=CHUNK_ID,
                source_key="integration/interrupted",
                locators=(SearchLocator(LocatorKind.LINES, 1, 1),),
                excerpt_sha256="b" * 64,
                matched=True,
                context_only=False,
            ),
        )
    )

    assert queued.run_id in await repository.prepare_recovery()
    recovered = await PostgresGroundedQARepository(database).get_run(queued.run_id)
    assert recovered is not None
    assert recovered.status.value == "queued"
    assert await repository.list_evidence(recovered.attempt.attempt_id) == ()
    await database.dispose()
