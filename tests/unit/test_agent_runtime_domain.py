from __future__ import annotations

from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from domain.agent_runtime import (
    AgentRun,
    AgentRunContext,
    BudgetExceededError,
    BudgetUsage,
    InvalidRunTransitionError,
    RecoveryRejectedError,
    RunBudget,
    RunCheckpoint,
    RunError,
    RunErrorCategory,
    RunEvent,
    RunStatus,
    RunStep,
    validate_recovery,
)


def make_run() -> AgentRun:
    return AgentRun(
        context=AgentRunContext(
            run_id=uuid4(),
            space_id=uuid4(),
            skill_name="knowledge_agent",
            skill_version="1.0.0",
            skill_content_sha256="a" * 64,
            trace_id="trace-1",
            caller_id="user-1",
        )
    )


def test_main_status_path_and_step_are_separate() -> None:
    run = make_run().transition(RunEvent.START)
    assert run.status == RunStatus.RUNNING
    assert run.current_step == RunStep.PLANNING
    run = run.transition(RunEvent.RETRIEVE).transition(RunEvent.EXECUTE)
    assert run.status == RunStatus.RUNNING
    assert run.current_step == RunStep.EXECUTING
    assert run.transition(RunEvent.VERIFY).transition(RunEvent.COMPLETE).current_step is None


def test_invalid_and_terminal_transitions_are_rejected() -> None:
    with pytest.raises(InvalidRunTransitionError):
        make_run().transition(RunEvent.EXECUTE)
    completed = make_run().transition(RunEvent.START).transition(RunEvent.FAIL)
    with pytest.raises(InvalidRunTransitionError):
        completed.transition(RunEvent.START)
    with pytest.raises(ValueError):
        AgentRun(
            context=make_run().context,
            status=RunStatus.COMPLETED,
            current_step=RunStep.EXECUTING,
        )


def test_cancel_and_timeout_are_terminal() -> None:
    run = make_run().transition(RunEvent.START).transition(RunEvent.REQUEST_CANCEL)
    assert run.status == RunStatus.CANCEL_REQUESTED
    run = run.transition(RunEvent.CANCEL)
    assert run.status == RunStatus.CANCELLED
    with pytest.raises(InvalidRunTransitionError):
        run.transition(RunEvent.START)
    timed_out = make_run().transition(RunEvent.START).transition(RunEvent.TIMEOUT)
    assert timed_out.status == RunStatus.TIMED_OUT


def test_waiting_approval_resumes_the_same_safe_step() -> None:
    run = (
        make_run()
        .transition(RunEvent.START)
        .transition(RunEvent.RETRIEVE)
        .transition(RunEvent.EXECUTE)
        .transition(RunEvent.WAIT_APPROVAL)
    )
    assert run.status == RunStatus.WAITING_APPROVAL
    assert run.current_step == RunStep.EXECUTING
    resumed = run.transition(RunEvent.APPROVE)
    assert resumed.status == RunStatus.RUNNING
    assert resumed.current_step == RunStep.EXECUTING


def test_budget_usage_only_increases_and_enforces_limits() -> None:
    budget = RunBudget(max_steps=2, max_tool_calls=1, max_input_tokens=10, max_output_tokens=10)
    usage = BudgetUsage().add(steps=1, tool_calls=1, input_tokens=3, budget=budget)
    assert usage.steps == 1
    with pytest.raises(BudgetExceededError, match=r"tool_calls=2>1"):
        usage.add(tool_calls=1, budget=budget)
    with pytest.raises(ValueError):
        usage.add(steps=-1)


def test_recovery_requires_verified_matching_checkpoint() -> None:
    run = make_run().transition(RunEvent.START)
    checkpoint = RunCheckpoint(
        run_id=run.context.run_id,
        sequence=1,
        schema_version=1,
        skill_name=run.context.skill_name,
        skill_version=run.context.skill_version,
        skill_content_sha256=run.context.skill_content_sha256,
        next_step=RunStep.PLANNING,
        next_node="plan",
        verified=True,
    )
    run = replace(run, checkpoint_sequence=1)
    validate_recovery(run, checkpoint, caller_id="user-1", space_id=run.context.space_id)
    with pytest.raises(RecoveryRejectedError):
        validate_recovery(run, checkpoint, caller_id="other", space_id=run.context.space_id)
    with pytest.raises(RecoveryRejectedError):
        validate_recovery(
            run,
            RunCheckpoint(
                run_id=run.context.run_id,
                sequence=2,
                schema_version=1,
                skill_name=run.context.skill_name,
                skill_version=run.context.skill_version,
                skill_content_sha256=run.context.skill_content_sha256,
            ),
            caller_id="user-1",
            space_id=run.context.space_id,
        )
    terminal = run.transition(RunEvent.FAIL)
    with pytest.raises(RecoveryRejectedError):
        validate_recovery(terminal, checkpoint, caller_id="user-1", space_id=run.context.space_id)
    used_run = run.consume(steps=1)
    with pytest.raises(RecoveryRejectedError):
        validate_recovery(used_run, checkpoint, caller_id="user-1", space_id=run.context.space_id)


def test_checkpoint_and_budget_reject_invalid_values() -> None:
    with pytest.raises(ValueError):
        RunBudget(max_steps=0)
    with pytest.raises(ValueError):
        RunCheckpoint(
            run_id=UUID(int=0),
            sequence=-1,
            schema_version=1,
            skill_name="x",
            skill_version="1",
            skill_content_sha256="a",
        )
    with pytest.raises(ValueError):
        RunError(
            code="invalid",
            category=RunErrorCategory.INPUT,
            message="safe message",
        )
