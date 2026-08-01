from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
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
