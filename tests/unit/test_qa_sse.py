from uuid import UUID

import pytest
from domain.qa_sse import QAEventLog, QAEventType, QAStreamEvent

RUN_ID = UUID(int=1)


@pytest.mark.asyncio
async def test_sse_events_have_monotonic_sequence_and_replay_cursor() -> None:
    log = QAEventLog()
    accepted = await log.append(RUN_ID, QAEventType.ACCEPTED, {"status": "queued"})
    started = await log.append(RUN_ID, QAEventType.STARTED, {"status": "running"})
    assert (accepted.sequence, started.sequence) == (1, 2)
    assert [event.sequence for event in await log.replay(RUN_ID, after_sequence=1)] == [2]
    assert accepted.as_dict()["payload"] == {"status": "queued"}


@pytest.mark.asyncio
async def test_replayed_command_does_not_duplicate_the_latest_event() -> None:
    log = QAEventLog()
    first = await log.append(RUN_ID, QAEventType.ACCEPTED, {"status": "queued"})
    replay = await log.append(RUN_ID, QAEventType.ACCEPTED, {"status": "queued"})
    assert replay == first
    assert len(await log.replay(RUN_ID)) == 1


@pytest.mark.asyncio
async def test_terminal_event_is_single_and_requires_safe_status() -> None:
    log = QAEventLog()
    completed = await log.append(RUN_ID, QAEventType.COMPLETED, {"status": "completed"})
    duplicate = await log.append(RUN_ID, QAEventType.FAILED, {"status": "failed"})
    assert duplicate == completed
    assert len(await log.replay(RUN_ID)) == 1
    with pytest.raises(ValueError, match="terminal SSE"):
        QAStreamEvent(run_id=RUN_ID, sequence=1, event_type=QAEventType.FAILED, payload={})


def test_event_rejects_non_positive_sequence() -> None:
    with pytest.raises(ValueError, match="positive"):
        QAStreamEvent(run_id=RUN_ID, sequence=0, event_type=QAEventType.HEARTBEAT, payload={})


def test_event_rejects_private_content_fields() -> None:
    with pytest.raises(ValueError, match="private content"):
        QAStreamEvent(
            run_id=RUN_ID,
            sequence=1,
            event_type=QAEventType.PHASE,
            payload={"details": {"prompt": "do not emit"}},
        )
