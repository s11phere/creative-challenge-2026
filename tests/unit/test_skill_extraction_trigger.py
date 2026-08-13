from __future__ import annotations

import pytest
import worker.skill_extraction as module
from worker.skill_extraction import maybe_enqueue_skill_pattern_extract


class FakeRedisClient:
    def __init__(self, *, available: bool = True, acquired: bool = True) -> None:
        self._available = available
        self._acquired = acquired
        self.set_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def set(self, *args: object, **kwargs: object):
        self.set_calls.append((args, kwargs))
        if not self._available:
            raise RuntimeError("redis down")
        return True if self._acquired else None


class StubBroker:
    def __init__(self, client: FakeRedisClient) -> None:
        self.client = client


def _patch(monkeypatch: pytest.MonkeyPatch, *, client: FakeRedisClient) -> list[int]:
    enqueued: list[int] = []
    monkeypatch.setattr(module, "broker", StubBroker(client))

    def _enqueue(*, trace_id: str | None = None, event_version: int = 1) -> object:
        del trace_id
        enqueued.append(event_version)
        return object()

    monkeypatch.setattr(module, "enqueue_skill_pattern_extract", _enqueue)
    return enqueued


def test_enqueues_when_not_throttled(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeRedisClient(acquired=True)
    enqueued = _patch(monkeypatch, client=client)

    assert maybe_enqueue_skill_pattern_extract(throttle_seconds=60) is True
    assert enqueued == [1]
    assert client.set_calls[0][1]["nx"] is True
    assert client.set_calls[0][1]["ex"] == 60


def test_skips_when_already_throttled(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeRedisClient(acquired=False)
    enqueued = _patch(monkeypatch, client=client)

    assert maybe_enqueue_skill_pattern_extract(throttle_seconds=60) is False
    assert enqueued == []


def test_graceful_when_redis_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeRedisClient(available=False)
    enqueued = _patch(monkeypatch, client=client)

    assert maybe_enqueue_skill_pattern_extract(throttle_seconds=60) is False
    assert enqueued == []
