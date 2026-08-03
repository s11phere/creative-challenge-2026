from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest
from agent_runtime.checkpoints import (
    InMemoryRuntimeStateStore,
    build_checkpoint,
    checkpoint_state_sha256,
)
from domain.agent_runtime import (
    AgentRun,
    AgentRunContext,
    RecoveryRejectedError,
    RunBudget,
    RunEvent,
    RunStep,
)


def make_run() -> AgentRun:
    run = AgentRun(
        context=AgentRunContext(
            run_id=UUID(int=1),
            space_id=UUID(int=2),
            skill_name="fixture",
            skill_version="1.0.0",
            skill_content_sha256="a" * 64,
            trace_id="trace-fixture",
            caller_id="user-fixture",
        ),
        budget=RunBudget(max_steps=4),
    )
    return run.transition(RunEvent.START).consume(steps=1)


@pytest.mark.asyncio
async def test_commit_atomically_persists_run_and_verified_checkpoint() -> None:
    store = InMemoryRuntimeStateStore()
    run, checkpoint = build_checkpoint(
        make_run(),
        state={"evidence_ids": ["fixture-evidence"]},
        next_step=RunStep.RETRIEVING,
        next_node="retrieve",
    )

    stored_run, stored_checkpoint = await store.commit(run, checkpoint)

    assert stored_run.checkpoint_sequence == stored_checkpoint.sequence == 1
    assert stored_checkpoint.state_sha256 == checkpoint_state_sha256(stored_checkpoint.state)
    assert await store.get_run(run.context.run_id) == stored_run
    assert await store.get_latest(run.context.run_id) == stored_checkpoint


@pytest.mark.asyncio
async def test_invalid_commit_leaves_previous_run_and_checkpoint_unchanged() -> None:
    store = InMemoryRuntimeStateStore()
    first_run, first_checkpoint = build_checkpoint(
        make_run(), state={}, next_step=RunStep.RETRIEVING, next_node="retrieve"
    )
    await store.commit(first_run, first_checkpoint)
    next_run, next_checkpoint = build_checkpoint(
        first_run.transition(RunEvent.RETRIEVE).consume(steps=1),
        state={"result": "fixture"},
        next_step=RunStep.EXECUTING,
        next_node="generate",
    )

    with pytest.raises(RecoveryRejectedError, match="digest"):
        await store.commit(next_run, replace(next_checkpoint, state={"result": "tampered"}))

    assert await store.get_run(first_run.context.run_id) == first_run
    assert await store.get_latest(first_run.context.run_id) == first_checkpoint


@pytest.mark.asyncio
async def test_commit_rejects_sequence_gap_and_usage_rollback() -> None:
    store = InMemoryRuntimeStateStore()
    first_run, first_checkpoint = build_checkpoint(
        make_run(), state={}, next_step=RunStep.RETRIEVING, next_node="retrieve"
    )
    await store.commit(first_run, first_checkpoint)

    gap_run, gap_checkpoint = build_checkpoint(
        replace(first_run, checkpoint_sequence=2),
        state={},
        next_step=RunStep.RETRIEVING,
        next_node="retrieve",
    )
    with pytest.raises(RecoveryRejectedError, match="contiguous"):
        await store.commit(gap_run, gap_checkpoint)

    rollback_run, rollback_checkpoint = build_checkpoint(
        replace(first_run, usage=replace(first_run.usage, steps=0)),
        state={},
        next_step=RunStep.RETRIEVING,
        next_node="retrieve",
    )
    with pytest.raises(RecoveryRejectedError, match="decrease"):
        await store.commit(rollback_run, rollback_checkpoint)


@pytest.mark.asyncio
async def test_commit_replays_an_existing_sequence_without_duplicate_checkpoint() -> None:
    store = InMemoryRuntimeStateStore()
    run, checkpoint = build_checkpoint(
        make_run(), state={"answer": "fixture"}, next_step=RunStep.RETRIEVING, next_node="retrieve"
    )
    await store.commit(run, checkpoint)

    replayed_run, replayed_checkpoint = await store.commit(run, checkpoint)

    assert replayed_run == run
    assert replayed_checkpoint == checkpoint


@pytest.mark.asyncio
async def test_commit_rejects_checkpoint_skill_identity_mismatch() -> None:
    store = InMemoryRuntimeStateStore()
    run, checkpoint = build_checkpoint(
        make_run(), state={}, next_step=RunStep.RETRIEVING, next_node="retrieve"
    )
    mismatched = replace(checkpoint, skill_version="9.9.9")

    with pytest.raises(RecoveryRejectedError, match="Skill identity"):
        await store.commit(run, mismatched)
