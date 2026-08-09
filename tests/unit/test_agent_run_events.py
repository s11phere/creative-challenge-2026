from __future__ import annotations

from uuid import UUID

import pytest
from domain.agent_sse import (
    AgentRunEventConflictError,
    AgentRunEventContractError,
    AgentRunEventLog,
    AgentRunEventType,
    AgentRunEventVersionError,
    AgentRunStreamEvent,
)


@pytest.mark.asyncio
async def test_agent_run_events_page_and_reject_second_terminal_outcome() -> None:
    run_id = UUID("00000000-0000-4000-8000-000000000091")
    events = AgentRunEventLog()
    await events.append(
        run_id,
        AgentRunEventType.ACCEPTED,
        {"status": "accepted"},
        event_key="accepted",
    )
    await events.append(
        run_id,
        AgentRunEventType.COMPLETED,
        {"status": "completed", "stop_reason": "goal_complete", "iteration": 1},
        event_key="terminal",
    )

    page = await events.page(run_id, limit=1)

    assert [event.sequence for event in page.events] == [1]
    assert page.next_sequence == 1
    assert page.has_more
    with pytest.raises(AgentRunEventConflictError):
        await events.append(
            run_id,
            AgentRunEventType.FAILED,
            {"status": "failed", "stop_reason": "failed", "error_code": "RUN_FAILED"},
            event_key="terminal:retry",
        )


def test_agent_run_event_contract_rejects_private_fields_and_unknown_versions() -> None:
    run_id = UUID("00000000-0000-4000-8000-000000000092")
    with pytest.raises(AgentRunEventContractError):
        AgentRunStreamEvent(
            run_id=run_id,
            sequence=1,
            event_type=AgentRunEventType.TOOL_OUTPUT,
            payload={"status": "succeeded", "prompt": "private"},
            event_key="unsafe",
        )
    with pytest.raises(AgentRunEventVersionError):
        AgentRunStreamEvent(
            run_id=run_id,
            sequence=1,
            event_type=AgentRunEventType.ACCEPTED,
            payload={"status": "accepted"},
            event_key="unknown-version",
            schema_version="agent-run-sse-v4",
        )
