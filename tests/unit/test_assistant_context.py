from __future__ import annotations

from uuid import UUID

import pytest
from application.assistant import (
    AssistantTurnSubmission,
    ConversationCompactionService,
    ConversationContextService,
    ConversationRunService,
)
from application.qa import InMemoryGroundedQARepository
from domain.conversation_run import ConversationRunStatus
from domain.qa_persistence import ConversationRecord
from model_gateway import FakeModelGateway, FakeScenario


async def _conversation_with_messages(
    repository: InMemoryGroundedQARepository, count: int = 4
) -> tuple[ConversationRecord, object]:
    conversation = ConversationRecord(
        conversation_id=UUID(int=701), space_id=UUID(int=702), owner_id="context-user"
    )
    await repository.create_conversation(conversation)
    service = ConversationRunService(conversations=repository, runs=repository)
    latest = None
    for index in range(count):
        latest = await service.submit(
            AssistantTurnSubmission(
                conversation_id=conversation.conversation_id,
                content=f"message {index} with explicit reference",
                idempotency_key=f"context-turn-{index}",
            )
        )
    assert latest is not None
    return conversation, latest


@pytest.mark.asyncio
async def test_compaction_is_idempotent_and_preserves_append_only_messages() -> None:
    repository = InMemoryGroundedQARepository()
    conversation, _latest = await _conversation_with_messages(repository)
    context = ConversationContextService(
        data=repository, runs=repository, recent_message_limit=1, soft_token_limit=1
    )
    compaction_run = await context.request_manual_compaction(
        conversation.conversation_id, content="/compact", idempotency_key="compact-1"
    )
    duplicate = await context.request_manual_compaction(
        conversation.conversation_id, content="/compact", idempotency_key="compact-1"
    )
    assert duplicate.run_id == compaction_run.run_id
    await repository.claim_conversation_run(
        compaction_run.run_id, lease_owner="context-worker", lease_seconds=60
    )
    service = ConversationCompactionService(
        context=context, data=repository, runs=repository, gateway=FakeModelGateway()
    )
    completed = await service.execute(compaction_run.run_id)
    assert completed is not None
    assert completed.status is ConversationRunStatus.COMPLETED
    assert len(await repository.list_messages(conversation.conversation_id)) == 5
    summaries = await repository.list_conversation_summaries(conversation.conversation_id)
    assert len(summaries) == 1
    assert summaries[0].content_sha256
    next_turn = await ConversationRunService(conversations=repository, runs=repository).submit(
        AssistantTurnSubmission(
            conversation_id=conversation.conversation_id,
            content="follow-up using the previous reference",
            idempotency_key="context-follow-up",
        )
    )
    snapshot = await context.snapshot(next_turn)
    assert snapshot.summary is not None
    assert len(snapshot.recent_messages) <= 1
    assert snapshot.current_message_id == next_turn.user_message_id


@pytest.mark.asyncio
async def test_compaction_failure_does_not_modify_original_messages() -> None:
    repository = InMemoryGroundedQARepository()
    conversation, _latest = await _conversation_with_messages(repository)
    context = ConversationContextService(data=repository, runs=repository)
    compaction_run = await context.request_manual_compaction(
        conversation.conversation_id, content="/compact", idempotency_key="compact-fail"
    )
    before = await repository.list_messages(conversation.conversation_id)
    await repository.claim_conversation_run(
        compaction_run.run_id, lease_owner="context-worker", lease_seconds=60
    )
    failed = await ConversationCompactionService(
        context=context,
        data=repository,
        runs=repository,
        gateway=FakeModelGateway(scenario=FakeScenario.TIMEOUT),
    ).execute(compaction_run.run_id)
    assert failed is not None
    assert failed.status is ConversationRunStatus.FAILED
    assert failed.error_code == "RUN_CONTEXT_COMPACTION_FAILED"
    assert await repository.list_messages(conversation.conversation_id) == before
    assert await repository.list_conversation_summaries(conversation.conversation_id) == ()
