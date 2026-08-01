from __future__ import annotations

import os
from uuid import uuid4

import pytest
from agent_runtime.checkpoints import build_checkpoint
from domain.agent_runtime import (
    AgentRun,
    AgentRunContext,
    RunBudget,
    RunEvent,
    RunStep,
    ToolPermission,
)
from domain.grounded_qa import QAAttempt
from domain.models import Space
from domain.qa_persistence import (
    ConversationRecord,
    MessageRecord,
    MessageRole,
    QARunRecord,
    QARunVersions,
)
from infrastructure.config import settings
from infrastructure.database import Database
from infrastructure.orm import SpaceModel
from infrastructure.qa_persistence import PostgresGroundedQARepository
from infrastructure.repositories import SpaceRepository
from infrastructure.runtime_state import PostgresRuntimeStateStore
from sqlalchemy import delete

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION") != "1",
        reason="set RUN_INTEGRATION=1 with an isolated migrated PostgreSQL database",
    ),
]


@pytest.mark.asyncio
async def test_runtime_checkpoint_is_persistent_and_idempotent() -> None:
    database = Database(settings.database_url)
    space_id = uuid4()
    run_id = uuid4()
    caller_id = "runtime-integration"
    qa = PostgresGroundedQARepository(database)
    try:
        async with database.transaction() as session:
            await SpaceRepository(session).create(
                Space(id=space_id, name="runtime-test", owner_id=caller_id)
            )
        conversation = ConversationRecord(space_id=space_id, owner_id=caller_id)
        await qa.create_conversation(conversation)
        message = await qa.append_message(
            MessageRecord(
                conversation_id=conversation.conversation_id,
                space_id=space_id,
                role=MessageRole.USER,
                content="runtime checkpoint fixture",
                idempotency_key="runtime-question",
            )
        )
        await qa.create_run(
            QARunRecord(
                run_id=run_id,
                attempt=QAAttempt(run_id=run_id),
                conversation_id=conversation.conversation_id,
                question_message_id=message.message_id,
                space_id=space_id,
                caller_id=caller_id,
                idempotency_key="runtime-run",
                versions=QARunVersions(
                    skill_name="knowledge_agent",
                    skill_version="0.1.0",
                    skill_content_sha256="a" * 64,
                    profile_version="qa-profile-v1",
                    retrieval_profile_version="retrieval-profile-v1",
                    model_identity="fake",
                    prompt_version="prompt-v1",
                    output_schema_version="schema-v1",
                    corpus_version="corpus-v1",
                    dataset_version="dataset-v1",
                ),
            )
        )
        run = AgentRun(
            context=AgentRunContext(
                run_id=run_id,
                space_id=space_id,
                skill_name="knowledge_agent",
                skill_version="0.1.0",
                skill_content_sha256="a" * 64,
                trace_id="1" * 32,
                caller_id=caller_id,
                granted_permissions=frozenset({ToolPermission.READ_KNOWLEDGE}),
            ),
            budget=RunBudget(max_steps=4, max_tool_calls=1),
        ).transition(RunEvent.START)
        run, checkpoint = build_checkpoint(
            run,
            state={"question": "runtime checkpoint fixture"},
            next_step=RunStep.RETRIEVING,
            next_node="retrieve",
        )
        store = PostgresRuntimeStateStore(database)
        persisted, saved = await store.commit(run, checkpoint)
        assert persisted.checkpoint_sequence == 1
        assert saved.state_sha256 == checkpoint.state_sha256
        assert await PostgresRuntimeStateStore(database).get_run(run_id) == persisted
        assert await PostgresRuntimeStateStore(database).get_latest(run_id) == checkpoint
        replayed, _ = await store.commit(run, checkpoint)
        assert replayed.checkpoint_sequence == 1
    finally:
        async with database.transaction() as session:
            await session.execute(delete(SpaceModel).where(SpaceModel.id == space_id))
        await database.dispose()
