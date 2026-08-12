from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from domain.conversation_context import ConversationSensitivity
from domain.usage_traces import UsageOutcome, UsagePatternSnapshot, UsageTrace, pattern_key
from infrastructure.config import settings
from infrastructure.database import Database
from infrastructure.orm import ConversationModel, ConversationRunModel, UsagePatternModel
from infrastructure.usage_traces import PostgresUsagePatternRepository, PostgresUsageTraceRepository
from sqlalchemy import delete

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION") != "1",
        reason="set RUN_INTEGRATION=1 with an isolated migrated PostgreSQL database",
    ),
]


async def _create_parent_run(database: Database) -> tuple[object, object]:
    """Insert a minimal Conversation + completed Skill ConversationRun."""
    conversation_id = uuid4()
    run_id = uuid4()
    async with database.transaction() as session:
        session.add(
            ConversationModel(
                id=conversation_id,
                space_id=conversation_id,
                owner_id="usage-trace-integration",
            )
        )
        session.add(
            ConversationRunModel(
                id=run_id,
                conversation_id=conversation_id,
                space_id=conversation_id,
                caller_id="usage-trace-integration",
                user_message_id=uuid4(),
                idempotency_key=f"usage-trace-{run_id.hex}",
                run_kind="skill",
                selection_source="auto",
                status="completed",
                cancellation_requested=False,
                router_version="router-v1",
                core_prompt_version="prompt-v1",
                model_identity="fake",
                skill_name="knowledge_agent",
                skill_version="1.0.0",
                skill_content_sha256="a" * 64,
                usage={},
                result=None,
            )
        )
    return run_id, conversation_id


@pytest.mark.asyncio
async def test_usage_trace_repository_roundtrip_and_idempotent_save() -> None:
    database = Database(settings.database_url)
    run_id, conversation_id = await _create_parent_run(database)
    repository = PostgresUsageTraceRepository(database)
    trace = UsageTrace(
        run_id=run_id,
        conversation_id=conversation_id,
        input_summary="Summarize this document",
        tools_used=("knowledge_search", "grounded_answer"),
        outcome=UsageOutcome.COMPLETED,
        model="fake",
        sensitivity=ConversationSensitivity.PRIVATE_LOCAL,
        skill_name="knowledge_agent",
    )
    try:
        saved = await repository.save(trace)
        assert saved == trace
        assert await repository.get(run_id) == trace
        # A second save for the same Run must not create a duplicate.
        await repository.save(trace)
        assert len(await repository.list(limit=100)) >= 1
        listed = await repository.list(since=datetime.now(UTC) - timedelta(minutes=1))
        assert any(item.run_id == run_id for item in listed)
    finally:
        async with database.transaction() as session:
            await session.execute(
                delete(ConversationRunModel).where(ConversationRunModel.id == run_id)
            )
        await database.dispose()


@pytest.mark.asyncio
async def test_usage_pattern_repository_replace_all_and_list() -> None:
    database = Database(settings.database_url)
    repository = PostgresUsagePatternRepository(database)
    first = datetime(2026, 8, 1, tzinfo=UTC)
    last = datetime(2026, 8, 12, tzinfo=UTC)
    patterns = (
        UsagePatternSnapshot(
            key=pattern_key(
                skill_name="knowledge_agent",
                task_category="question",
                tool_sequence="knowledge_search,grounded_answer",
                input_type="zh",
            ),
            skill_name="knowledge_agent",
            task_category="question",
            tool_sequence="knowledge_search,grounded_answer",
            input_type="zh",
            frequency=5,
            first_seen_at=first,
            last_seen_at=last,
        ),
        UsagePatternSnapshot(
            key=pattern_key(
                skill_name=None,
                task_category="general",
                tool_sequence="none",
                input_type="en",
            ),
            skill_name=None,
            task_category="general",
            tool_sequence="none",
            input_type="en",
            frequency=2,
            first_seen_at=first,
            last_seen_at=last,
        ),
    )
    try:
        await repository.replace_all(patterns)
        listed = await repository.list()
        assert len(listed) == 2
        assert listed[0].frequency == 5  # frequency-desc ordering
        # replace_all is a full snapshot swap, not an accumulation.
        await repository.replace_all(patterns[:1])
        assert len(await repository.list()) == 1
    finally:
        async with database.transaction() as session:
            await session.execute(delete(UsagePatternModel))
        await database.dispose()
