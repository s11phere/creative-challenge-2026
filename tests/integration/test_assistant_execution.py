"""Isolated PostgreSQL checks for provisional direct Assistant execution."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from application.assistant import (
    AssistantAgentService,
    AssistantTurnSubmission,
    ConversationRunService,
)
from domain.assistant_sse import AssistantEventType
from domain.conversation_run import ConversationRunStatus
from domain.models import Space
from domain.qa_persistence import ConversationRecord
from infrastructure.assistant_events import PostgresAssistantEventStore
from infrastructure.config import settings
from infrastructure.conversation_runs import PostgresConversationRunRepository
from infrastructure.database import Database
from infrastructure.orm import SpaceModel
from infrastructure.qa_persistence import PostgresGroundedQARepository
from infrastructure.repositories import SpaceRepository
from model_gateway import FakeModelGateway
from sqlalchemy import delete

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION") != "1",
        reason="set RUN_INTEGRATION=1 with an isolated migrated PostgreSQL database",
    ),
]


@pytest.mark.asyncio
async def test_direct_assistant_message_and_v2_events_survive_repository_restart() -> None:
    database = Database(settings.database_url)
    space_id = uuid4()
    caller_id = "assistant-integration"
    qa = PostgresGroundedQARepository(database)
    runs = PostgresConversationRunRepository(database)
    events = PostgresAssistantEventStore(database)
    try:
        async with database.transaction() as session:
            await SpaceRepository(session).create(
                Space(id=space_id, name="Assistant integration", owner_id=caller_id)
            )
        conversation = await qa.create_conversation(
            ConversationRecord(
                space_id=space_id,
                owner_id=caller_id,
            )
        )
        submitted = await ConversationRunService(conversations=qa, runs=runs).submit(
            AssistantTurnSubmission(
                conversation_id=conversation.conversation_id,
                content="Synthetic direct conversation fixture.",
                idempotency_key=f"assistant-direct-{uuid4()}",
            )
        )
        await events.append(
            submitted.run_id,
            AssistantEventType.ACCEPTED,
            {"status": submitted.status.value},
        )
        claimed = await runs.claim_conversation_run(
            submitted.run_id,
            lease_owner="integration-worker",
            lease_seconds=30,
        )
        assert claimed is not None
        assert claimed.status is ConversationRunStatus.RUNNING
        completed = await AssistantAgentService(
            runs=runs,
            messages=qa,
            gateway=FakeModelGateway(),
            events=events,
        ).execute(submitted.run_id)

        assert completed is not None
        assert completed.status is ConversationRunStatus.COMPLETED
        assert completed.result is not None
        assert completed.result.message_id is not None
        restored = await PostgresConversationRunRepository(database).get_conversation_run(
            submitted.run_id
        )
        assert restored == completed
        message = await PostgresGroundedQARepository(database).get_message(
            completed.result.message_id
        )
        assert message is not None
        assert message.content.startswith("fake-response-")
        replayed = await PostgresAssistantEventStore(database).replay(submitted.run_id)
        assert [event.event_type for event in replayed] == [
            AssistantEventType.ACCEPTED,
            AssistantEventType.ROUTING,
            AssistantEventType.COMPLETED,
        ]
    finally:
        async with database.transaction() as session:
            await session.execute(delete(SpaceModel).where(SpaceModel.id == space_id))
        await database.dispose()
