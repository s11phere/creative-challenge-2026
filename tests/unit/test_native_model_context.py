"""Synthetic tests for bounded v2 model context and prompt-cache hints."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import cast
from uuid import UUID

import pytest
from agent_runtime import (
    InMemoryToolRegistry,
    JSONValue,
    NativeDecisionHistoryItem,
    NativeModelContextV2,
    NativeModelObservation,
    NativeToolUseAgentLoopExecutor,
    project_model_observation,
)
from agent_runtime.skills import PinnedSkill
from application.assistant import ConversationContextSnapshot
from domain.agent_runtime import (
    AgentRun,
    AgentRunContext,
    RunBudget,
    ToolPermission,
)
from domain.conversation_context import ConversationSensitivity
from model_gateway import (
    CapabilityAlias,
    CapabilityStatus,
    ChatRequest,
    ChatResponse,
    FakeModelGateway,
    GatewayStatus,
    ModelGateway,
    ModelProvider,
    ModelUsage,
)

RUN_ID = UUID("00000000-0000-4000-8000-000000000061")
SPACE_ID = UUID("00000000-0000-4000-8000-000000000062")


@dataclass
class RecordingTrace:
    events: list[dict[str, object]] = field(default_factory=list)

    async def record(self, event_type: str, **payload: object) -> None:
        self.events.append({"event": event_type, **payload})


class CacheGateway:
    def __init__(self, *responses: ChatResponse) -> None:
        self._responses = list(responses)
        self.requests: list[ChatRequest] = []

    @property
    def status(self) -> GatewayStatus:
        return GatewayStatus(
            available=True,
            code="MODEL_FAKE_READY",
            provider=ModelProvider.FAKE,
            capabilities=(CapabilityAlias.FAST_CHAT,),
            capability_statuses=(
                CapabilityStatus(
                    capability=CapabilityAlias.FAST_CHAT,
                    available=True,
                    code="MODEL_FAKE_READY",
                    supports_native_tool_use=True,
                    supports_prompt_caching=True,
                ),
            ),
            model_identity="fake-fast-chat-v1",
        )

    async def chat(
        self,
        request: ChatRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.FAST_CHAT,
    ) -> ChatResponse:
        self.requests.append(request)
        assert capability is CapabilityAlias.FAST_CHAT
        return self._responses.pop(0)


def _response(text: str = "Synthetic terminal.") -> ChatResponse:
    return ChatResponse(
        text=text,
        finish_reason="stop",
        usage=ModelUsage(input_tokens=2, output_tokens=1),
        capability=CapabilityAlias.FAST_CHAT,
        latency_ms=0.0,
    )


def _run(*, space_id: UUID = SPACE_ID) -> AgentRun:
    return AgentRun(
        context=AgentRunContext(
            run_id=RUN_ID,
            space_id=space_id,
            skill_name="native_fixture",
            skill_version="1.0.0",
            skill_content_sha256="a" * 64,
            trace_id="native-context-trace",
            caller_id="synthetic-user",
            granted_permissions=frozenset({ToolPermission.READ_KNOWLEDGE, ToolPermission.MODEL}),
        ),
        budget=RunBudget(
            max_steps=4,
            max_tool_calls=4,
            max_input_tokens=100,
            max_output_tokens=100,
            timeout_seconds=30,
        ),
    )


def _executor(
    gateway: ModelGateway,
    *,
    prompt_caching_allowed: Callable[[AgentRunContext], bool] | None = None,
    trace: RecordingTrace | None = None,
) -> NativeToolUseAgentLoopExecutor:
    return NativeToolUseAgentLoopExecutor(
        tool_registry=InMemoryToolRegistry(handlers={}),
        allowed_tools=(),
        system_prompt="Native base prompt.",
        model_gateway=gateway,
        debug_trace=trace,
        prompt_caching_allowed=prompt_caching_allowed,
    )


def _context(*, decisions: int = 1, observations: int = 1) -> NativeModelContextV2:
    return NativeModelContextV2(
        goal="Synthetic goal.",
        selected_skills=(),
        decision_history=tuple(
            NativeDecisionHistoryItem(
                iteration=index,
                kind="tool",
                tool_name="synthetic_lookup",
                summary="Synthetic decision.",
            )
            for index in range(1, decisions + 1)
        ),
        observations=tuple(
            NativeModelObservation(
                iteration=index,
                tool_name="synthetic_lookup",
                summary="Synthetic observation.",
            )
            for index in range(1, observations + 1)
        ),
        progress_summary="Synthetic progress.",
    )


def test_projection_keeps_only_declared_fields_and_bounds_strings() -> None:
    schema: dict[str, JSONValue] = {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "summary", "matched_count"],
        "properties": {
            "status": {"type": "string"},
            "summary": {"type": "string", "maxLength": 8},
            "matched_count": {"type": "integer", "minimum": 0},
        },
    }
    projected = project_model_observation(
        {
            "status": "succeeded",
            "summary": "a very long private summary",
            "matched_count": 3,
            "secret": "must-not-escape",
        },
        schema,
        summary="safe summary",
    )

    assert projected == {
        "status": "succeeded",
        "summary": "a very l",
        "matched_count": 3,
    }
    assert "must-not-escape" not in json.dumps(projected)


def test_native_model_context_enforces_schema_bounds() -> None:
    with pytest.raises(ValueError, match="decision history is too large"):
        _context(decisions=13)
    with pytest.raises(ValueError, match="observations are too large"):
        _context(observations=9)
    with pytest.raises(ValueError, match="progress summary is too large"):
        NativeModelContextV2(
            goal="Synthetic goal.",
            selected_skills=(),
            decision_history=(),
            observations=(),
            progress_summary="x" * 4_001,
        )


@pytest.mark.asyncio
async def test_executor_sends_bounded_context_and_static_cache_key() -> None:
    first_gateway = CacheGateway(_response())
    second_gateway = CacheGateway(_response())
    trace = RecordingTrace()
    executor = _executor(
        cast(ModelGateway, first_gateway),
        prompt_caching_allowed=lambda _context: True,
        trace=trace,
    )

    await executor.execute(
        _run(space_id=UUID(int=63)),
        cast(PinnedSkill, object()),
        {"question": "synthetic one"},
        goal="Synthetic first goal.",
    )
    first_request = first_gateway.requests[0]
    payload = json.loads(first_request.messages[1].content)

    assert payload["model_context"]["schema_version"] == "agent-model-context-v2"
    assert payload["model_context"]["decision_history"] == []
    assert first_request.cache_key is not None
    assert first_request.cache_key.startswith("sha256:")
    assert "Synthetic first goal." not in first_request.cache_key
    assert any(event["event"] == "agent_round" for event in trace.events)
    assert all("Synthetic first goal." not in json.dumps(event) for event in trace.events)

    await _executor(
        cast(ModelGateway, second_gateway),
        prompt_caching_allowed=lambda _context: True,
    ).execute(
        _run(space_id=UUID(int=64)),
        cast(PinnedSkill, object()),
        {"question": "synthetic two"},
        goal="A completely different goal.",
    )

    assert second_gateway.requests[0].cache_key == first_request.cache_key


@pytest.mark.asyncio
async def test_executor_omits_cache_key_when_policy_or_provider_disables_caching() -> None:
    allowed_gateway = CacheGateway(_response())
    allowed = await _executor(
        cast(ModelGateway, allowed_gateway),
        prompt_caching_allowed=lambda _context: False,
    ).execute(
        _run(),
        cast(PinnedSkill, object()),
        {"question": "synthetic"},
        goal="Synthetic goal.",
    )
    assert allowed.run.status.value == "completed"
    assert allowed_gateway.requests[0].cache_key is None

    disabled_gateway = FakeModelGateway()
    disabled = await _executor(
        disabled_gateway,
        prompt_caching_allowed=lambda _context: True,
    ).execute(
        _run(),
        cast(PinnedSkill, object()),
        {"question": "synthetic"},
        goal="Synthetic goal.",
    )
    assert disabled.run.status.value == "completed"
    # FakeModelGateway default advertises no prompt caching.
    assert disabled_gateway.status.supports_prompt_caching(CapabilityAlias.FAST_CHAT) is False


def test_conversation_snapshot_can_carry_only_the_bounded_native_context() -> None:
    context = _context()
    snapshot = ConversationContextSnapshot(
        conversation_id=UUID(int=71),
        space_id=SPACE_ID,
        current_message_id=UUID(int=72),
        current_content="Synthetic user request.",
        summary=None,
        recent_messages=(),
        sensitivity=ConversationSensitivity.PUBLIC_DEMO,
        estimated_input_tokens=1,
        soft_limit_exceeded=False,
        native_model_context=context,
    )

    rendered = snapshot.native_model_context_request()
    assert rendered == context.render_model_context()
    assert "Synthetic decision." in rendered
    assert "prompt" not in rendered
    assert "output_summary" not in rendered
