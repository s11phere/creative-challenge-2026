from __future__ import annotations

from datetime import datetime
from uuid import UUID

import pytest
from application.assistant import AssistantTurnSubmission, ConversationRunService
from application.qa import InMemoryGroundedQARepository
from application.usage_traces import (
    UsageTraceRecorder,
    build_usage_trace,
    classify_outcome,
    sanitize_input_summary,
)
from domain.agent_sse import AgentRunEventLog, AgentRunEventType
from domain.conversation_context import ConversationSensitivity, ConversationSummary
from domain.conversation_run import (
    AssistantResult,
    AssistantResultKind,
    ConversationRun,
    ConversationRunKind,
    ConversationRunStatus,
    ConversationRunUsage,
    FixedSkillIdentity,
)
from domain.qa_persistence import (
    ConversationRecord,
    MessageRecord,
    MessageRole,
    QAAttempt,
    QARunRecord,
    QARunVersions,
)
from domain.usage_traces import UsageOutcome, UsageTrace

_RUN_ID = UUID(int=100)
_SPACE_ID = UUID(int=902)


class InMemoryUsageTraceRepository:
    def __init__(self) -> None:
        self._traces: dict[UUID, UsageTrace] = {}

    async def save(self, trace: UsageTrace) -> UsageTrace:
        self._traces[trace.run_id] = trace
        return trace

    async def get(self, run_id: UUID) -> UsageTrace | None:
        return self._traces.get(run_id)

    async def list(
        self, *, limit: int | None = None, since: datetime | None = None
    ) -> tuple[UsageTrace, ...]:
        values = (
            trace for trace in self._traces.values() if since is None or trace.created_at >= since
        )
        selected = tuple(values)
        return selected[:limit] if limit is not None else selected


def _versions() -> QARunVersions:
    return QARunVersions(
        skill_version="1.0.0",
        profile_version="grounded-qa-provisional-v1",
        retrieval_profile_version="stage3-default-pending-formal-freeze",
        model_identity="fake-fast-chat-v1",
        prompt_version="grounded-qa-v1-provisional",
        output_schema_version="grounded-answer-v1",
        corpus_version="v0-provisional",
        dataset_version="knowledge-qa-v0-provisional",
        skill_content_sha256="a" * 64,
    )


def _result(message_id: int = 7) -> AssistantResult:
    return AssistantResult(kind=AssistantResultKind.DIRECT_MESSAGE, message_id=UUID(int=message_id))


def _plain_run(*, status: ConversationRunStatus) -> ConversationRun:
    return ConversationRun(
        run_id=_RUN_ID,
        conversation_id=UUID(int=2),
        space_id=_SPACE_ID,
        caller_id="user",
        user_message_id=UUID(int=4),
        idempotency_key="key",
        run_kind=ConversationRunKind.ASSISTANT_TURN,
        status=status,
        model_identity="fake",
        result=_result() if status is ConversationRunStatus.COMPLETED else None,
    )


class TestClassifyOutcome:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (ConversationRunStatus.COMPLETED, UsageOutcome.COMPLETED),
            (ConversationRunStatus.REFUSED, UsageOutcome.REFUSED),
            (ConversationRunStatus.WAITING_CLARIFICATION, UsageOutcome.CLARIFIED),
            (ConversationRunStatus.FAILED, UsageOutcome.FAILED),
            (ConversationRunStatus.CANCELLED, UsageOutcome.FAILED),
            (ConversationRunStatus.TIMED_OUT, UsageOutcome.FAILED),
        ],
    )
    def test_finished_statuses(self, status: ConversationRunStatus, expected: UsageOutcome) -> None:
        assert classify_outcome(status) is expected

    @pytest.mark.parametrize(
        "status",
        [
            ConversationRunStatus.CREATED,
            ConversationRunStatus.QUEUED,
            ConversationRunStatus.RUNNING,
            ConversationRunStatus.CANCEL_REQUESTED,
            ConversationRunStatus.WAITING_APPROVAL,
        ],
    )
    def test_unfinished_statuses_yield_none(self, status: ConversationRunStatus) -> None:
        assert classify_outcome(status) is None


class TestSanitizeInputSummary:
    def test_truncates_to_bound(self) -> None:
        assert len(sanitize_input_summary("x" * 10_000, max_chars=64)) == 64

    def test_redacts_long_hex_secrets(self) -> None:
        secret = "a" * 40
        result = sanitize_input_summary(f"token is {secret} rest")
        assert secret not in result
        assert "[redacted]" in result

    def test_strips_control_characters_and_collapses_whitespace(self) -> None:
        result = sanitize_input_summary("a\x00b\n\t  c")
        assert "\x00" not in result
        assert "  " not in result
        assert "b c" in result

    def test_empty_content_becomes_placeholder(self) -> None:
        assert sanitize_input_summary("   ") == "(empty)"


class TestBuildUsageTrace:
    def test_skill_trace(self) -> None:
        run = ConversationRun(
            run_id=_RUN_ID,
            conversation_id=UUID(int=2),
            space_id=_SPACE_ID,
            caller_id="user",
            user_message_id=UUID(int=4),
            idempotency_key="key",
            run_kind=ConversationRunKind.SKILL,
            status=ConversationRunStatus.COMPLETED,
            model_identity="fake",
            skill=FixedSkillIdentity("knowledge_agent", "1.0.0", "a" * 64),
            result=_result(),
        )
        trace = build_usage_trace(
            run,
            input_summary="Summarize this document",
            tools_used=("knowledge_search", "grounded_answer"),
            sensitivity=ConversationSensitivity.PRIVATE_LOCAL,
        )
        assert trace.skill_name == "knowledge_agent"
        assert trace.command is None
        assert trace.outcome is UsageOutcome.COMPLETED
        assert trace.model == "fake"
        assert trace.input_summary == "Summarize this document"

    def test_command_trace_uses_run_kind(self) -> None:
        trace = build_usage_trace(
            _plain_run(status=ConversationRunStatus.COMPLETED),
            input_summary="Hello",
            tools_used=(),
            sensitivity=ConversationSensitivity.PRIVATE_LOCAL,
        )
        assert trace.skill_name is None
        assert trace.command == "assistant_turn"

    def test_unfinished_run_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="finished"):
            build_usage_trace(
                _plain_run(status=ConversationRunStatus.RUNNING),
                input_summary="Hello",
                tools_used=(),
                sensitivity=ConversationSensitivity.PRIVATE_LOCAL,
            )


class TestUsageTraceRecorder:
    @staticmethod
    async def _recorder(
        repository: InMemoryGroundedQARepository,
        events: AgentRunEventLog,
        traces: InMemoryUsageTraceRepository,
    ) -> UsageTraceRecorder:
        return UsageTraceRecorder(
            runs=repository,
            data=repository,
            qa=repository,
            agent_events=events,
            traces=traces,
        )

    @staticmethod
    async def _skill_run(repository: InMemoryGroundedQARepository) -> ConversationRun:
        conversation = await repository.create_conversation(
            ConversationRecord(
                conversation_id=UUID(int=901),
                space_id=_SPACE_ID,
                owner_id="usage-trace-user",
            )
        )
        run = await ConversationRunService(conversations=repository, runs=repository).submit(
            AssistantTurnSubmission(
                conversation_id=conversation.conversation_id,
                content="Summarize this document",
                idempotency_key="usage-trace-turn",
            )
        )
        await repository.claim_conversation_run(run.run_id, lease_owner="worker", lease_seconds=60)
        return await repository.promote_to_skill(
            run.run_id,
            run_kind=ConversationRunKind.SKILL,
            selection_source=run.selection_source,
            skill=FixedSkillIdentity("knowledge_agent", "1.0.0", "a" * 64),
            core_prompt_version="assistant-base-prompt-v8",
        )

    @staticmethod
    async def _finish(run: ConversationRun, repository: InMemoryGroundedQARepository) -> None:
        await repository.publish_direct_message(
            run_id=run.run_id,
            message=MessageRecord(
                message_id=UUID(int=905),
                conversation_id=run.conversation_id,
                space_id=run.space_id,
                role=MessageRole.ASSISTANT,
                content="answer",
                run_id=run.run_id,
            ),
            usage=ConversationRunUsage(),
            model_identity="fake",
        )

    async def test_records_completed_skill_run(self) -> None:
        repository = InMemoryGroundedQARepository()
        events = AgentRunEventLog()
        traces = InMemoryUsageTraceRepository()
        run = await self._skill_run(repository)
        await events.append(
            run.run_id,
            AgentRunEventType.TOOL_STARTED,
            {"iteration": 1, "tool_name": "knowledge_search", "tool_version": "1.0.0"},
            event_key="tool-1",
        )
        await self._finish(run, repository)
        recorder = await self._recorder(repository, events, traces)

        recorded = await recorder.record_run(run.run_id)

        assert recorded is not None
        assert recorded.skill_name == "knowledge_agent"
        assert recorded.outcome is UsageOutcome.COMPLETED
        assert recorded.tools_used == ("knowledge_search",)
        assert "Summarize this document" in recorded.input_summary
        assert recorded.model == "fake"
        assert recorded.sensitivity is ConversationSensitivity.PRIVATE_LOCAL

    async def test_recording_is_idempotent(self) -> None:
        repository = InMemoryGroundedQARepository()
        traces = InMemoryUsageTraceRepository()
        run = await self._skill_run(repository)
        await self._finish(run, repository)
        recorder = await self._recorder(repository, AgentRunEventLog(), traces)

        first = await recorder.record_run(run.run_id)
        second = await recorder.record_run(run.run_id)

        assert first == second
        assert len(await traces.list()) == 1

    async def test_unfinished_run_is_skipped(self) -> None:
        repository = InMemoryGroundedQARepository()
        traces = InMemoryUsageTraceRepository()
        run = await self._skill_run(repository)
        recorder = await self._recorder(repository, AgentRunEventLog(), traces)

        assert await recorder.record_run(run.run_id) is None
        assert len(await traces.list()) == 0

    async def test_missing_run_is_skipped(self) -> None:
        repository = InMemoryGroundedQARepository()
        recorder = await self._recorder(
            repository, AgentRunEventLog(), InMemoryUsageTraceRepository()
        )
        assert await recorder.record_run(_RUN_ID) is None

    async def test_sensitivity_from_qa_run(self) -> None:
        repository = InMemoryGroundedQARepository()
        traces = InMemoryUsageTraceRepository()
        run = await self._skill_run(repository)
        await repository.create_run(
            QARunRecord(
                run_id=run.run_id,
                attempt=QAAttempt(run_id=run.run_id),
                conversation_id=run.conversation_id,
                question_message_id=run.user_message_id,
                space_id=run.space_id,
                caller_id=run.caller_id,
                idempotency_key=run.idempotency_key,
                versions=_versions(),
                context_sensitivity="restricted",
            )
        )
        await self._finish(run, repository)
        recorder = await self._recorder(repository, AgentRunEventLog(), traces)

        recorded = await recorder.record_run(run.run_id)

        assert recorded is not None
        assert recorded.sensitivity is ConversationSensitivity.RESTRICTED

    async def test_sensitivity_falls_back_to_summaries(self) -> None:
        repository = InMemoryGroundedQARepository()
        traces = InMemoryUsageTraceRepository()
        run = await self._skill_run(repository)
        await repository.create_conversation_summary(
            ConversationSummary(
                conversation_id=run.conversation_id,
                space_id=run.space_id,
                run_id=run.run_id,
                covered_start_message_id=run.user_message_id,
                covered_end_message_id=run.user_message_id,
                covered_message_count=1,
                content="rolling summary",
                prompt_version="conversation-summary-prompt-v1",
                model_identity="fake",
                sensitivity=ConversationSensitivity.RESTRICTED,
            )
        )
        await self._finish(run, repository)
        recorder = await self._recorder(repository, AgentRunEventLog(), traces)

        recorded = await recorder.record_run(run.run_id)

        assert recorded is not None
        assert recorded.sensitivity is ConversationSensitivity.RESTRICTED
