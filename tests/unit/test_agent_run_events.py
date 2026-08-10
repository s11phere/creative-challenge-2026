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


def test_agent_run_event_contract_accepts_bounded_query_preview_and_rejects_controls() -> None:
    run_id = UUID("00000000-0000-4000-8000-000000000093")
    event = AgentRunStreamEvent(
        run_id=run_id,
        sequence=1,
        event_type=AgentRunEventType.TOOL_REQUESTED,
        payload={
            "status": "requested",
            "iteration": 1,
            "tool_name": "knowledge_search",
            "tool_version": "1.1.0",
            "query_preview": "What is indexed?",
        },
        event_key="query-preview",
    )
    assert event.payload["query_preview"] == "What is indexed?"
    for preview in ("line\nbreak", "x" * 513):
        with pytest.raises(AgentRunEventContractError):
            AgentRunStreamEvent(
                run_id=run_id,
                sequence=2,
                event_type=AgentRunEventType.TOOL_REQUESTED,
                payload={
                    "status": "requested",
                    "iteration": 1,
                    "tool_name": "knowledge_search",
                    "tool_version": "1.1.0",
                    "query_preview": preview,
                },
                event_key=f"invalid-preview-{len(preview)}",
            )
    with pytest.raises(AgentRunEventContractError):
        AgentRunStreamEvent(
            run_id=run_id,
            sequence=3,
            event_type=AgentRunEventType.TOOL_OUTPUT,
            payload={
                "status": "succeeded",
                "iteration": 1,
                "tool_name": "other_tool",
                "tool_version": "1.0.0",
                "query_preview": "not allowed",
            },
            event_key="generic-preview",
        )


def test_agent_run_event_contract_accepts_safe_skill_and_document_details() -> None:
    run_id = UUID("00000000-0000-4000-8000-000000000094")
    activation = AgentRunStreamEvent(
        run_id=run_id,
        sequence=1,
        event_type=AgentRunEventType.SKILL_ACTIVATED,
        payload={
            "status": "activated",
            "iteration": 0,
            "skill_name": "assistant_agent",
            "skill_version": "0.1.0",
        },
        event_key="skill-activated",
    )
    assert activation.payload["skill_name"] == "assistant_agent"
    document = AgentRunStreamEvent(
        run_id=run_id,
        sequence=2,
        event_type=AgentRunEventType.TOOL_REQUESTED,
        payload={
            "status": "requested",
            "iteration": 1,
            "tool_name": "summarize_document",
            "tool_version": "1.1.0",
            "resource_reference": "CLAUDE.md",
        },
        event_key="document-reference",
    )
    assert document.payload["resource_reference"] == "CLAUDE.md"
    with pytest.raises(AgentRunEventContractError):
        AgentRunStreamEvent(
            run_id=run_id,
            sequence=3,
            event_type=AgentRunEventType.TOOL_OUTPUT,
            payload={
                "status": "succeeded",
                "iteration": 1,
                "tool_name": "knowledge_search",
                "tool_version": "1.1.0",
                "resource_reference": "CLAUDE.md",
            },
            event_key="invalid-document-reference",
        )
