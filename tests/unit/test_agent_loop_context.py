from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from application.assistant import (
    AssistantTurnSubmission,
    ConversationContextService,
    ConversationRunService,
)
from application.qa import InMemoryGroundedQARepository
from domain.agent_loop import (
    AgentLoopState,
    AgentLoopTask,
    AgentLoopToolObservation,
)
from domain.conversation_context import ConversationEvidenceCoverage, ConversationSummary
from domain.conversation_run import (
    Clarification,
    ClarificationKind,
    ConversationRun,
    ConversationRunUsage,
)
from domain.grounded_qa import QAContractError
from domain.qa_persistence import ConversationRecord, MessageRecord
from model_gateway import ModelProvider


async def _turn() -> tuple[InMemoryGroundedQARepository, ConversationRecord, ConversationRun]:
    repository = InMemoryGroundedQARepository()
    conversation = ConversationRecord(
        conversation_id=UUID(int=801), space_id=UUID(int=802), owner_id="loop-context-user"
    )
    await repository.create_conversation(conversation)
    run = await ConversationRunService(conversations=repository, runs=repository).submit(
        AssistantTurnSubmission(
            conversation_id=conversation.conversation_id,
            content="Compare the two approved approaches and identify the open risk.",
            idempotency_key="loop-context-turn",
        )
    )
    return repository, conversation, run


@pytest.mark.asyncio
async def test_loop_snapshot_preserves_task_state_without_replaying_raw_history() -> None:
    repository, conversation, run = await _turn()
    state = (
        AgentLoopState.accepted(
            AgentLoopTask(
                "Compare approved approaches",
                ("Inspect the first approach", "Identify unresolved risks"),
            )
        )
        .start()
        .begin_iteration({"action": "call_tool"})
        .request_tool(
            name="knowledge_search",
            version="1.0.0",
            arguments={"query": "approved approaches"},
            idempotency_key="tool-1",
            request_fingerprint="request-1",
        )
        .start_tool()
        .observe(
            AgentLoopToolObservation(
                iteration=1,
                tool_name="knowledge_search",
                tool_version="1.0.0",
                idempotency_key="tool-1",
                input_summary="one approved source",
                output_summary="coverage found for the first approach",
            )
        )
        .begin_iteration({"action": "call_tool"})
        .request_tool(
            name="knowledge_inspect",
            version="1.0.0",
            arguments={"evidence": "candidate-1"},
            idempotency_key="tool-2",
            request_fingerprint="request-2",
        )
        .wait_for_approval("approval-42")
    )
    context = ConversationContextService(data=repository, runs=repository)

    snapshot = await context.snapshot(
        run,
        loop_state=state,
        evidence_coverage=ConversationEvidenceCoverage(
            candidate_count=3, covered_count=1, required_count=2, evidence_ids=("evidence-1",)
        ),
    )

    assert snapshot.current_goal == "Compare approved approaches"
    assert snapshot.subquestions == ("Inspect the first approach", "Identify unresolved risks")
    assert snapshot.unresolved_items == snapshot.subquestions
    assert snapshot.tool_history[0].tool_name == "knowledge_search"
    assert snapshot.approval_pending
    assert snapshot.approval_id == "approval-42"
    assert 'ratio="0.50"' in snapshot.router_input()
    standalone = snapshot.standalone_request()
    assert "knowledge_search" not in standalone
    assert "Compare approved approaches" in standalone
    assert "approval-42" not in standalone

    native = snapshot.continuation_for(
        ModelProvider.OPENAI_COMPATIBLE,
        responses_continuation_id="resp_123",
        native_continuation_supported=True,
    )
    assert native.continuation_id == "resp_123"
    assert native.replay_messages
    fallback = snapshot.continuation_for(ModelProvider.FAKE)
    assert fallback.continuation_id is None
    assert fallback.replay_messages[-1].content == standalone
    with pytest.raises(ValueError, match="Responses continuation"):
        snapshot.continuation_for(
            ModelProvider.FAKE,
            responses_continuation_id="resp_123",
        )


@pytest.mark.asyncio
async def test_decision_request_exposes_the_previous_user_request() -> None:
    repository, conversation, _first = await _turn()
    second = await ConversationRunService(conversations=repository, runs=repository).submit(
        AssistantTurnSubmission(
            conversation_id=conversation.conversation_id,
            content="What was my previous question?",
            idempotency_key="loop-context-previous-question",
        )
    )
    snapshot = await ConversationContextService(data=repository, runs=repository).snapshot(second)

    decision_request = snapshot.decision_request()
    assert "<previous-user-request>" in decision_request
    assert "Compare the two approved approaches and identify the open risk." in decision_request
    assert "What was my previous question?" in decision_request
    assert "previous-server-clarification" not in decision_request
    # Nested Skills keep their original isolated input contract.
    assert "<previous-user-request>" not in snapshot.standalone_request()


@pytest.mark.asyncio
async def test_decision_request_exposes_the_previous_server_clarification() -> None:
    repository, conversation, first = await _turn()
    await repository.publish_clarification(
        run_id=first.run_id,
        clarification=Clarification(
            clarification_id=f"clarify:{first.run_id.hex}",
            kind=ClarificationKind.INPUT_REQUIRED,
            message="Please provide the exact published document name.",
        ),
        usage=ConversationRunUsage(),
        model_identity="fake",
    )
    second = await ConversationRunService(conversations=repository, runs=repository).submit(
        AssistantTurnSubmission(
            conversation_id=conversation.conversation_id,
            content="What information do I need to provide?",
            idempotency_key="loop-context-clarification-followup",
        )
    )
    snapshot = await ConversationContextService(data=repository, runs=repository).snapshot(second)

    assert "<previous-server-clarification>" in snapshot.decision_request()
    assert "exact published document name" in snapshot.decision_request()
    assert "<previous-server-clarification>" not in snapshot.standalone_request()


@pytest.mark.asyncio
async def test_snapshot_rejects_space_or_summary_boundary_violations() -> None:
    repository, conversation, run = await _turn()
    context = ConversationContextService(data=repository, runs=repository)

    with pytest.raises(QAContractError, match="ownership boundary"):
        await context.snapshot(replace(run, space_id=uuid4()))

    messages = await repository.list_messages(conversation.conversation_id)
    summary = ConversationSummary(
        conversation_id=conversation.conversation_id,
        space_id=conversation.space_id,
        run_id=uuid4(),
        covered_start_message_id=messages[0].message_id,
        covered_end_message_id=messages[0].message_id,
        covered_message_count=1,
        content="Prior context that must remain in its own Space.",
        prompt_version="test-v1",
        model_identity="fake",
    )
    await repository.create_conversation_summary(summary)

    class CrossSpaceSummaries:
        async def get_conversation(self, conversation_id: UUID) -> ConversationRecord | None:
            return await repository.get_conversation(conversation_id)

        async def get_message(self, message_id: UUID) -> MessageRecord | None:
            return await repository.get_message(message_id)

        async def list_messages(self, conversation_id: UUID) -> tuple[MessageRecord, ...]:
            return await repository.list_messages(conversation_id)

        async def list_conversation_summaries(
            self, _conversation_id: UUID
        ) -> tuple[ConversationSummary, ...]:
            return (replace(summary, space_id=uuid4()),)

        async def create_conversation_summary(
            self, value: ConversationSummary
        ) -> ConversationSummary:
            return await repository.create_conversation_summary(value)

    with pytest.raises(QAContractError, match="summary crosses Space boundary"):
        await ConversationContextService(data=CrossSpaceSummaries(), runs=repository).snapshot(run)
