from __future__ import annotations

from uuid import uuid4

import dramatiq
import pytest
from worker import qa_tasks


def test_qa_message_contains_control_metadata_only() -> None:
    payload = {"run_id": str(uuid4()), "trace_id": "1" * 32, "event_version": 1}

    message = qa_tasks.qa_run.message(**payload)

    assert message.queue_name == "qa"
    assert message.kwargs == payload
    assert qa_tasks.qa_run.options["max_retries"] == 12
    assert qa_tasks.qa_run.options["time_limit"] == 120_000
    assert qa_tasks.qa_run.options["notify_shutdown"] is True


def test_qa_actor_retries_while_another_worker_holds_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qa_tasks, "_run_qa_sync", lambda _run_id: False)

    with pytest.raises(dramatiq.Retry, match="QA attempt lease is active"):
        qa_tasks.qa_run.fn(run_id=str(uuid4()), trace_id="2" * 32, event_version=1)


def test_enqueue_qa_run_canonicalizes_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class Message:
        message_id = "message-1"

    def send(**kwargs: object) -> Message:
        captured.update(kwargs)
        return Message()

    monkeypatch.setattr(qa_tasks.qa_run, "send", send)
    run_id = str(uuid4())

    message = qa_tasks.enqueue_qa_run(
        run_id=run_id,
        trace_id="00000000-0000-0000-0000-000000000003",
    )

    assert message.message_id == "message-1"
    assert captured == {"run_id": run_id, "trace_id": "0" * 31 + "3", "event_version": 1}
