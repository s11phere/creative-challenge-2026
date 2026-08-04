from __future__ import annotations

import os
from dataclasses import replace
from uuid import uuid4

import pytest
from agent_runtime.checkpoints import build_checkpoint
from domain.agent_runtime import (
    AgentRun,
    AgentRunContext,
    RecoveryRejectedError,
    RunBudget,
    RunEvent,
    RunStep,
    ToolCallRecord,
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
from infrastructure.orm import QACitationModel, SpaceModel
from infrastructure.qa_persistence import PostgresGroundedQARepository
from infrastructure.repositories import SpaceRepository
from infrastructure.runtime_approval import PostgresApprovalPort, PostgresDerivedKnowledgeStore
from infrastructure.runtime_state import PostgresRuntimeStateStore
from infrastructure.skill_references import PostgresSkillReferenceChecker
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
        created_run = await qa.create_run(
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
        citation_id = uuid4()
        async with database.transaction() as session:
            session.add(
                QACitationModel(
                    id=uuid4(),
                    run_id=run_id,
                    attempt_id=created_run.attempt.attempt_id,
                    message_id=message.message_id,
                    evidence_id=citation_id,
                    payload={},
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
        with pytest.raises(RecoveryRejectedError, match="digest"):
            await store.commit(run, replace(checkpoint, state_sha256="c" * 64))
        with pytest.raises(RecoveryRejectedError, match="Skill identity"):
            await store.commit(run, replace(checkpoint, skill_version="9.9.9"))
        persisted, saved = await store.commit(run, checkpoint)
        assert persisted.checkpoint_sequence == 1
        assert saved.state_sha256 == checkpoint.state_sha256
        assert await PostgresRuntimeStateStore(database).get_run(run_id) == persisted
        assert await PostgresRuntimeStateStore(database).get_latest(run_id) == checkpoint
        replayed, _ = await store.commit(run, checkpoint)
        assert replayed.checkpoint_sequence == 1
        report = await PostgresSkillReferenceChecker(database).references(
            skill_name="knowledge_agent",
            skill_version="0.1.0",
            content_sha256="a" * 64,
        )
        assert report.qa_runs == 1
        assert report.runtime_runs == 1
        assert report.checkpoints == 1
    finally:
        async with database.transaction() as session:
            await session.execute(delete(SpaceModel).where(SpaceModel.id == space_id))
        await database.dispose()


@pytest.mark.asyncio
async def test_approval_and_derived_write_are_durable_and_idempotent() -> None:
    database = Database(settings.database_url)
    space_id = uuid4()
    run_id = uuid4()
    caller_id = "runtime-write-integration"
    qa = PostgresGroundedQARepository(database)
    try:
        async with database.transaction() as session:
            await SpaceRepository(session).create(
                Space(id=space_id, name="runtime-write-test", owner_id=caller_id)
            )
        conversation = ConversationRecord(space_id=space_id, owner_id=caller_id)
        await qa.create_conversation(conversation)
        message = await qa.append_message(
            MessageRecord(
                conversation_id=conversation.conversation_id,
                space_id=space_id,
                role=MessageRole.USER,
                content="review card fixture",
                idempotency_key="review-card-question",
            )
        )
        created_run = await qa.create_run(
            QARunRecord(
                run_id=run_id,
                attempt=QAAttempt(run_id=run_id),
                conversation_id=conversation.conversation_id,
                question_message_id=message.message_id,
                space_id=space_id,
                caller_id=caller_id,
                idempotency_key="review-card-run",
                versions=QARunVersions(
                    skill_name="create_review_cards",
                    skill_version="0.1.0",
                    skill_content_sha256="b" * 64,
                    profile_version="qa-profile-v1",
                    retrieval_profile_version="retrieval-profile-v1",
                    model_identity="fake",
                    prompt_version="prompt-v1",
                    output_schema_version="review-cards-skill-output-v1",
                    corpus_version="corpus-v1",
                    dataset_version="dataset-v1",
                ),
            )
        )
        citation_id = uuid4()
        async with database.transaction() as session:
            session.add(
                QACitationModel(
                    id=uuid4(),
                    run_id=run_id,
                    attempt_id=created_run.attempt.attempt_id,
                    message_id=message.message_id,
                    evidence_id=citation_id,
                    payload={},
                )
            )
        context = AgentRunContext(
            run_id=run_id,
            space_id=space_id,
            skill_name="create_review_cards",
            skill_version="0.1.0",
            skill_content_sha256="b" * 64,
            trace_id="2" * 32,
            caller_id=caller_id,
            granted_permissions=frozenset({ToolPermission.WRITE_KNOWLEDGE}),
        )
        tool = ToolCallRecord(
            tool_name="write_review_cards",
            tool_version="1.0.0",
            permissions=frozenset({ToolPermission.WRITE_KNOWLEDGE}),
            idempotency_key="review-card-write",
        )
        approvals = PostgresApprovalPort(database)
        approval_id = await approvals.request(context, tool)
        assert await approvals.request(context, tool) == approval_id
        assert not await approvals.is_approved(approval_id, context)
        assert await approvals.decide(approval_id, approved=True, decided_by=caller_id)
        assert await PostgresApprovalPort(database).is_approved(approval_id, context)
        assert not await PostgresApprovalPort(database).is_approved_for_tool(
            approval_id, context, tool_name="other_write", tool_version="1.0.0"
        )

        writer = PostgresDerivedKnowledgeStore(database)
        with pytest.raises(ValueError, match="Space"):
            await writer.write_review_cards(
                run_id=run_id,
                space_id=uuid4(),
                created_by=caller_id,
                idempotency_key="wrong-space",
                content={"cards": []},
                citation_ids=(str(citation_id),),
            )
        first = await writer.write_review_cards(
            run_id=run_id,
            space_id=space_id,
            created_by=caller_id,
            idempotency_key="review-card-write",
            content={"cards": [{"front": "fixture", "back": "supported"}]},
            citation_ids=(str(citation_id),),
        )
        replay = await PostgresDerivedKnowledgeStore(database).write_review_cards(
            run_id=run_id,
            space_id=space_id,
            created_by=caller_id,
            idempotency_key="review-card-write",
            content={"cards": [{"front": "fixture", "back": "supported"}]},
            citation_ids=first.citation_ids,
        )
        assert replay.id == first.id
        approval_record = await approvals.get(approval_id, run_id=run_id)
        assert approval_record is not None
        assert approval_record.status == "approved"
        assert len(await approvals.list_for_run(run_id)) == 1
        derived = await writer.get(first.id, run_id=run_id)
        assert derived is not None
        assert derived.status == "active"
        assert len(await writer.list_for_run(run_id)) == 1
        revoked = await writer.revoke(
            first.id, run_id=run_id, space_id=space_id, revoked_by=caller_id
        )
        assert revoked is not None
        assert revoked.status == "revoked"
        assert await approvals.revoke(approval_id, revoked_by=caller_id)
        assert not await PostgresApprovalPort(database).is_approved(approval_id, context)
    finally:
        async with database.transaction() as session:
            await session.execute(delete(SpaceModel).where(SpaceModel.id == space_id))
        await database.dispose()
