from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from domain.assistant_sse import AssistantEventLog, AssistantEventType
from domain.conversation_run import ConversationRun, ConversationRunStatus
from worker.assistant_tasks import _fail_claimed_assistant_run
from worker.qa_tasks import _wait_for_execution


@pytest.mark.asyncio
async def test_lease_loss_cancels_in_flight_execution() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def work() -> None:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    execution = asyncio.create_task(work())
    await started.wait()
    lease_lost = asyncio.Event()
    lease_lost.set()

    assert await _wait_for_execution(execution, lease_lost, run_id=uuid4()) is False
    assert cancelled.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "cancellation_requested", "error_code", "event_type"),
    [
        (
            ConversationRunStatus.FAILED,
            False,
            "RUN_ASSISTANT_RUNTIME_FAILED",
            AssistantEventType.FAILED,
        ),
        (ConversationRunStatus.CANCELLED, True, None, AssistantEventType.CANCELLED),
    ],
)
async def test_unhandled_assistant_failure_projects_the_persisted_terminal_status(
    status: ConversationRunStatus,
    cancellation_requested: bool,
    error_code: str | None,
    event_type: AssistantEventType,
) -> None:
    run = ConversationRun(
        caller_id="synthetic-worker",
        idempotency_key="terminal-projection",
        status=status,
        cancellation_requested=cancellation_requested,
        error_code=error_code,
    )

    class Runs:
        def __init__(self) -> None:
            self.calls: list[tuple[object, str]] = []

        async def fail_conversation_run(
            self, run_id: object, *, error_code: str
        ) -> ConversationRun:
            self.calls.append((run_id, error_code))
            return run

    runs = Runs()
    events = AssistantEventLog()

    await _fail_claimed_assistant_run(runs, events, run.run_id)  # type: ignore[arg-type]

    assert runs.calls == [(run.run_id, "RUN_ASSISTANT_RUNTIME_FAILED")]
    replayed = await events.replay(run.run_id)
    assert [(event.event_type, event.payload["status"]) for event in replayed] == [
        (event_type, status.value)
    ]
