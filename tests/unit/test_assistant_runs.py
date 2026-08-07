from uuid import UUID

import pytest
from application.assistant import AssistantTurnSubmission, ConversationRunService
from application.qa import InMemoryGroundedQARepository
from domain.conversation_run import ConversationRunStatus
from domain.grounded_qa import QAContractError
from domain.qa_persistence import ConversationRecord


@pytest.mark.asyncio
async def test_assistant_turn_is_durable_idempotent_and_recoverable_without_a_model() -> None:
    repository = InMemoryGroundedQARepository()
    conversation = ConversationRecord(
        conversation_id=UUID(int=101), space_id=UUID(int=102), owner_id="local-user"
    )
    await repository.create_conversation(conversation)
    service = ConversationRunService(conversations=repository, runs=repository)

    first = await service.submit(
        AssistantTurnSubmission(
            conversation_id=conversation.conversation_id,
            content="Hello, Assistant.",
            idempotency_key="assistant-turn-1",
        )
    )
    replayed = await service.submit(
        AssistantTurnSubmission(
            conversation_id=conversation.conversation_id,
            content="Hello, Assistant.",
            idempotency_key="assistant-turn-1",
        )
    )

    assert replayed.run_id == first.run_id
    assert first.status is ConversationRunStatus.CREATED
    assert len(await repository.list_messages(conversation.conversation_id)) == 1
    assert await service.get(first.run_id) == first
    assert await repository.prepare_conversation_recovery() == (first.run_id,)

    cancelled = await service.cancel(first.run_id)

    assert cancelled.status is ConversationRunStatus.CANCEL_REQUESTED
    assert cancelled.cancellation_requested is True
    assert await repository.prepare_conversation_recovery() == (first.run_id,)

    with pytest.raises(QAContractError, match="conflicting content"):
        await service.submit(
            AssistantTurnSubmission(
                conversation_id=conversation.conversation_id,
                content="Different content.",
                idempotency_key="assistant-turn-1",
            )
        )
