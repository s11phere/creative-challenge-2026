from datetime import UTC, datetime
from uuid import UUID

import pytest
from domain.conversation_run import (
    AssistantResult,
    AssistantResultKind,
    Clarification,
    ClarificationKind,
    ConversationRun,
    ConversationRunStatus,
    ResourceCandidate,
)
from infrastructure.conversation_runs import _is_orphaned_native_approval
from infrastructure.orm import ConversationRunModel, RuntimeCheckpointModel


def test_completed_run_requires_a_safe_result_reference() -> None:
    completed = ConversationRun(
        run_id=UUID(int=1),
        conversation_id=UUID(int=2),
        space_id=UUID(int=3),
        caller_id="local-user",
        user_message_id=UUID(int=4),
        idempotency_key="turn-1",
        status=ConversationRunStatus.COMPLETED,
        result=AssistantResult(AssistantResultKind.SKILL_RESULT, message_id=UUID(int=5)),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    assert completed.result is not None
    assert completed.result.message_id == UUID(int=5)

    with pytest.raises(ValueError, match="business terminal"):
        ConversationRun(
            caller_id="local-user",
            idempotency_key="turn-2",
            status=ConversationRunStatus.COMPLETED,
        )


def test_clarification_keeps_only_bounded_safe_resource_metadata() -> None:
    result = AssistantResult(
        AssistantResultKind.CLARIFICATION,
        clarification=Clarification(
            clarification_id="clarification-1",
            kind=ClarificationKind.RESOURCE_AMBIGUOUS,
            message="Choose a document.",
            resource_candidates=(
                ResourceCandidate(
                    candidate_id="candidate-1",
                    resource_type="document",
                    label="Architecture",
                    source_label="Team notes",
                ),
            ),
        ),
    )

    assert result.clarification is not None
    assert result.clarification.resource_candidates[0].label == "Architecture"


def test_recovery_only_marks_legacy_native_approval_orphans() -> None:
    run = ConversationRunModel(cancellation_requested=False)
    checkpoint = RuntimeCheckpointModel(
        run_id=UUID(int=1),
        sequence=1,
        schema_version=1,
        skill_name="assistant_agent",
        skill_version="1.0.0",
        skill_content_sha256="a" * 64,
        state={
            "schema_version": "native-tool-use-loop-state-v2",
            "pending_call": {"call_id": "call-1", "tool_name": "fs_write"},
            "approval_id": None,
        },
        state_sha256="b" * 64,
        usage={},
        next_step="executing",
        next_node="native_tool_use_loop",
        verified=True,
    )

    assert _is_orphaned_native_approval(run, checkpoint)
    checkpoint.state["approval_id"] = "approval-1"
    assert not _is_orphaned_native_approval(run, checkpoint)
    checkpoint.state["approval_id"] = None
    checkpoint.next_node = "agent_loop"
    assert not _is_orphaned_native_approval(run, checkpoint)
