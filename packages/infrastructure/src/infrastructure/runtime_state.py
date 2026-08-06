"""PostgreSQL persistence for the generic Runtime snapshot and checkpoints."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any, cast
from uuid import UUID

from agent_runtime.checkpoints import checkpoint_state_sha256
from domain.agent_runtime import (
    AgentRun,
    AgentRunContext,
    BudgetUsage,
    RecoveryRejectedError,
    RunBudget,
    RunCheckpoint,
    RunError,
    RunErrorCategory,
    RunStatus,
    RunStep,
    ToolPermission,
)
from sqlalchemy import select

from .database import Database
from .orm import RuntimeCheckpointModel, RuntimeRunModel


class PostgresRuntimeStateStore:
    """Atomically store one Runtime transition and its verified checkpoint.

    The Runtime row is keyed by the shared ConversationRun ID. Checkpoints are
    append-only by sequence, so a retry can safely replay an already committed
    checkpoint without creating a second side effect.
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    async def commit(
        self, run: AgentRun, checkpoint: RunCheckpoint
    ) -> tuple[AgentRun, RunCheckpoint]:
        _validate_commit(run, checkpoint)
        async with self._database.transaction() as session:
            stored = await session.get(RuntimeRunModel, run.context.run_id, with_for_update=True)
            if stored is None:
                if run.checkpoint_sequence != 1:
                    raise RecoveryRejectedError("first checkpoint sequence must be one")
                stored = RuntimeRunModel(
                    run_id=run.context.run_id,
                    space_id=run.context.space_id,
                    caller_id=run.context.caller_id,
                    trace_id=run.context.trace_id,
                    skill_name=run.context.skill_name,
                    skill_version=run.context.skill_version,
                    skill_content_sha256=run.context.skill_content_sha256,
                )
                session.add(stored)
            else:
                _validate_identity(stored, run)
                expected = stored.checkpoint_sequence + 1
                if run.checkpoint_sequence != expected:
                    if run.checkpoint_sequence == stored.checkpoint_sequence:
                        replay = await session.get(
                            RuntimeCheckpointModel,
                            (run.context.run_id, run.checkpoint_sequence),
                        )
                        if replay is not None:
                            return _run(stored), _checkpoint(replay)
                    raise RecoveryRejectedError("checkpoint sequence is not contiguous")
                if _usage_decreased(stored, run):
                    raise RecoveryRejectedError("stored run usage cannot decrease")

            stored.granted_permissions = sorted(
                permission.value for permission in run.context.granted_permissions
            )
            stored.budget = _budget(run.budget)
            stored.usage = _usage(run.usage)
            stored.status = run.status.value
            stored.current_step = run.current_step.value if run.current_step else None
            stored.checkpoint_sequence = run.checkpoint_sequence
            stored.last_error = _error(run.last_error)
            # Flush the parent snapshot before inserting its checkpoint so the
            # foreign key is valid on the first commit as well as on replay.
            await session.flush()
            session.add(
                RuntimeCheckpointModel(
                    run_id=checkpoint.run_id,
                    sequence=checkpoint.sequence,
                    schema_version=checkpoint.schema_version,
                    skill_name=checkpoint.skill_name,
                    skill_version=checkpoint.skill_version,
                    skill_content_sha256=checkpoint.skill_content_sha256,
                    state=cast(dict[str, Any], deepcopy(dict(checkpoint.state))),
                    state_sha256=checkpoint.state_sha256,
                    usage=_usage(checkpoint.usage),
                    next_step=cast(RunStep, checkpoint.next_step).value,
                    next_node=cast(str, checkpoint.next_node),
                    verified=checkpoint.verified,
                    created_at=checkpoint.created_at,
                )
            )
            await session.flush()
            return _run(stored), checkpoint

    async def get_run(self, run_id: UUID) -> AgentRun | None:
        async with self._database.session() as session:
            stored = await session.get(RuntimeRunModel, run_id)
            return _run(stored) if stored is not None else None

    async def get_latest(self, run_id: UUID) -> RunCheckpoint | None:
        async with self._database.session() as session:
            result = await session.execute(
                select(RuntimeCheckpointModel)
                .where(RuntimeCheckpointModel.run_id == run_id)
                .order_by(RuntimeCheckpointModel.sequence.desc())
                .limit(1)
            )
            stored = result.scalar_one_or_none()
            return _checkpoint(stored) if stored is not None else None


def _validate_commit(run: AgentRun, checkpoint: RunCheckpoint) -> None:
    if run.context.run_id != checkpoint.run_id:
        raise RecoveryRejectedError("checkpoint belongs to another run")
    if (
        checkpoint.skill_name,
        checkpoint.skill_version,
        checkpoint.skill_content_sha256,
    ) != (
        run.context.skill_name,
        run.context.skill_version,
        run.context.skill_content_sha256,
    ):
        raise RecoveryRejectedError("checkpoint Skill identity does not match run")
    if run.checkpoint_sequence != checkpoint.sequence:
        raise RecoveryRejectedError("run and checkpoint sequence differ")
    if run.usage != checkpoint.usage or not checkpoint.verified:
        raise RecoveryRejectedError("checkpoint usage or verification is invalid")
    state = cast(Mapping[str, Any], checkpoint.state)
    if checkpoint.state_sha256 != checkpoint_state_sha256(state):
        raise RecoveryRejectedError("checkpoint state digest is invalid")


def _validate_identity(stored: RuntimeRunModel, run: AgentRun) -> None:
    if (
        stored.space_id != run.context.space_id
        or stored.caller_id != run.context.caller_id
        or stored.trace_id != run.context.trace_id
        or stored.skill_name != run.context.skill_name
        or stored.skill_version != run.context.skill_version
        or stored.skill_content_sha256 != run.context.skill_content_sha256
    ):
        raise RecoveryRejectedError("stored run identity is immutable")


def _budget(value: RunBudget) -> dict[str, int]:
    return {
        "max_steps": value.max_steps,
        "max_tool_calls": value.max_tool_calls,
        "max_input_tokens": value.max_input_tokens,
        "max_output_tokens": value.max_output_tokens,
        "timeout_seconds": value.timeout_seconds,
    }


def _usage(value: BudgetUsage) -> dict[str, int]:
    return {
        "steps": value.steps,
        "tool_calls": value.tool_calls,
        "input_tokens": value.input_tokens,
        "output_tokens": value.output_tokens,
        "elapsed_ms": value.elapsed_ms,
    }


def _error(value: RunError | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "code": value.code,
        "category": value.category.value,
        "message": value.message,
        "retryable": value.retryable,
        "safe_summary": value.safe_summary,
    }


def _run(value: RuntimeRunModel) -> AgentRun:
    context = AgentRunContext(
        run_id=value.run_id,
        space_id=value.space_id,
        skill_name=value.skill_name,
        skill_version=value.skill_version,
        skill_content_sha256=value.skill_content_sha256,
        trace_id=value.trace_id,
        caller_id=value.caller_id,
        granted_permissions=frozenset(
            ToolPermission(permission) for permission in value.granted_permissions
        ),
    )
    raw_error = value.last_error
    error = (
        RunError(
            code=raw_error["code"],
            category=RunErrorCategory(raw_error["category"]),
            message=raw_error["message"],
            retryable=bool(raw_error.get("retryable", False)),
            safe_summary=raw_error.get("safe_summary", ""),
        )
        if raw_error
        else None
    )
    return AgentRun(
        context=context,
        budget=RunBudget(**value.budget),
        usage=BudgetUsage(**value.usage),
        status=RunStatus(value.status),
        current_step=RunStep(value.current_step) if value.current_step else None,
        checkpoint_sequence=value.checkpoint_sequence,
        last_error=error,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def _checkpoint(value: RuntimeCheckpointModel) -> RunCheckpoint:
    state = cast(Mapping[str, Any], value.state)
    if value.state_sha256 != checkpoint_state_sha256(state):
        raise RecoveryRejectedError("stored checkpoint state digest is invalid")
    return RunCheckpoint(
        run_id=value.run_id,
        sequence=value.sequence,
        schema_version=value.schema_version,
        skill_name=value.skill_name,
        skill_version=value.skill_version,
        skill_content_sha256=value.skill_content_sha256,
        state=cast(Mapping[str, object], value.state),
        state_sha256=value.state_sha256,
        usage=BudgetUsage(**value.usage),
        next_step=RunStep(value.next_step),
        next_node=value.next_node,
        verified=value.verified,
        created_at=value.created_at,
    )


def _usage_decreased(previous: RuntimeRunModel, current: AgentRun) -> bool:
    before = previous.usage
    after = current.usage
    return any(
        new < old
        for old, new in zip(
            (
                int(before.get("steps", 0)),
                int(before.get("tool_calls", 0)),
                int(before.get("input_tokens", 0)),
                int(before.get("output_tokens", 0)),
                int(before.get("elapsed_ms", 0)),
            ),
            (
                after.steps,
                after.tool_calls,
                after.input_tokens,
                after.output_tokens,
                after.elapsed_ms,
            ),
            strict=True,
        )
    )


__all__ = ["PostgresRuntimeStateStore"]
