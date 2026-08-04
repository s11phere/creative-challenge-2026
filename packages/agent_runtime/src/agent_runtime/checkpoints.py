"""Deterministic checkpoint construction and an in-memory transactional adapter."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from typing import cast
from uuid import UUID

from domain.agent_runtime import (
    AgentRun,
    RecoveryRejectedError,
    RunCheckpoint,
    RunStatus,
    RunStep,
)

from .tools import JSONValue

CHECKPOINT_SCHEMA_VERSION = 1


def checkpoint_state_sha256(state: Mapping[str, JSONValue]) -> str:
    try:
        payload = json.dumps(
            state,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RecoveryRejectedError("checkpoint state is not canonical JSON") from exc
    return hashlib.sha256(payload).hexdigest()


def build_checkpoint(
    run: AgentRun,
    *,
    state: Mapping[str, JSONValue],
    next_step: RunStep,
    next_node: str,
) -> tuple[AgentRun, RunCheckpoint]:
    if run.status is not RunStatus.RUNNING or not next_node:
        raise RecoveryRejectedError("checkpoint requires an active run and safe continuation")
    sequence = run.checkpoint_sequence + 1
    stored_state = deepcopy(dict(state))
    checkpoint = RunCheckpoint(
        run_id=run.context.run_id,
        sequence=sequence,
        schema_version=CHECKPOINT_SCHEMA_VERSION,
        skill_name=run.context.skill_name,
        skill_version=run.context.skill_version,
        skill_content_sha256=run.context.skill_content_sha256,
        state=stored_state,
        state_sha256=checkpoint_state_sha256(stored_state),
        usage=run.usage,
        next_step=next_step,
        next_node=next_node,
        verified=True,
    )
    return replace(run, checkpoint_sequence=sequence), checkpoint


class InMemoryRuntimeStateStore:
    """Transaction double with the same append-only semantics as PostgreSQL."""

    def __init__(self) -> None:
        self._runs: dict[UUID, AgentRun] = {}
        self._checkpoints: dict[UUID, dict[int, RunCheckpoint]] = {}
        self._lock = asyncio.Lock()

    async def commit(
        self, run: AgentRun, checkpoint: RunCheckpoint
    ) -> tuple[AgentRun, RunCheckpoint]:
        self._validate_commit(run, checkpoint)
        async with self._lock:
            existing = self._runs.get(run.context.run_id)
            if existing is not None:
                if existing.context != run.context or existing.budget != run.budget:
                    raise RecoveryRejectedError("stored run identity is immutable")
                if run.checkpoint_sequence == existing.checkpoint_sequence:
                    replay = self._checkpoints[run.context.run_id].get(run.checkpoint_sequence)
                    if replay is not None:
                        return deepcopy(existing), deepcopy(replay)
                if run.checkpoint_sequence != existing.checkpoint_sequence + 1:
                    raise RecoveryRejectedError("checkpoint sequence is not contiguous")
                if _usage_decreased(existing, run):
                    raise RecoveryRejectedError("stored run usage cannot decrease")
            elif run.checkpoint_sequence != 1:
                raise RecoveryRejectedError("first checkpoint sequence must be one")
            stored_run = deepcopy(run)
            stored_checkpoint = deepcopy(checkpoint)
            self._runs[run.context.run_id] = stored_run
            self._checkpoints.setdefault(run.context.run_id, {})[stored_checkpoint.sequence] = (
                stored_checkpoint
            )
            return deepcopy(stored_run), deepcopy(stored_checkpoint)

    async def get_run(self, run_id: UUID) -> AgentRun | None:
        async with self._lock:
            run = self._runs.get(run_id)
            return deepcopy(run) if run is not None else None

    async def get_latest(self, run_id: UUID) -> RunCheckpoint | None:
        async with self._lock:
            checkpoints = self._checkpoints.get(run_id)
            checkpoint = (
                max(checkpoints.values(), key=lambda item: item.sequence) if checkpoints else None
            )
            return deepcopy(checkpoint) if checkpoint is not None else None

    @staticmethod
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
        state = cast(Mapping[str, JSONValue], checkpoint.state)
        if checkpoint.state_sha256 != checkpoint_state_sha256(state):
            raise RecoveryRejectedError("checkpoint state digest is invalid")


def _usage_decreased(previous: AgentRun, current: AgentRun) -> bool:
    before = previous.usage
    after = current.usage
    return any(
        new < old
        for old, new in zip(
            (
                before.steps,
                before.tool_calls,
                before.input_tokens,
                before.output_tokens,
                before.elapsed_ms,
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


__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "InMemoryRuntimeStateStore",
    "build_checkpoint",
    "checkpoint_state_sha256",
]
