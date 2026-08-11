"""In-memory provisional Grounded QA repository for ownership and atomicity tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from domain.conversation_context import ConversationSummary
from domain.conversation_run import (
    AssistantResult,
    AssistantResultKind,
    Clarification,
    ConversationRun,
    ConversationRunKind,
    ConversationRunSelectionSource,
    ConversationRunStatus,
    ConversationRunUsage,
    FixedSkillIdentity,
)
from domain.grounded_qa import (
    Citation,
    CitationStatus,
    QAContractError,
    QAErrorCode,
    QAEvent,
    QAOutcome,
    QAResult,
    QAStatus,
    transition_qa_status,
    validate_answer_citations,
)
from domain.qa_persistence import (
    CitationRecord,
    ConversationRecord,
    EvidenceRecord,
    FeedbackRecord,
    FeedbackReviewRecord,
    FeedbackReviewStatus,
    MessageRecord,
    MessageRole,
    QARunRecord,
    QARunUsage,
    terminal_status_for_result,
)
from domain.reasoning import ReasoningEffort

_RUNTIME_TERMINAL = frozenset({QAStatus.FAILED, QAStatus.CANCELLED, QAStatus.TIMED_OUT})
_BUSINESS_TERMINAL = frozenset({QAStatus.COMPLETED, QAStatus.REFUSED})


class InMemoryGroundedQARepository:
    """Serialize writes to model the future PostgreSQL transaction boundary."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._conversations: dict[UUID, ConversationRecord] = {}
        self._messages: dict[UUID, MessageRecord] = {}
        self._message_keys: dict[tuple[UUID, str], UUID] = {}
        self._attempts: dict[UUID, QARunRecord] = {}
        self._latest_attempts: dict[UUID, UUID] = {}
        self._run_keys: dict[tuple[UUID, str, str], UUID] = {}
        self._conversation_runs: dict[UUID, ConversationRun] = {}
        self._conversation_run_keys: dict[tuple[UUID, str, str], UUID] = {}
        self._conversation_run_leases: dict[UUID, tuple[str, datetime]] = {}
        self._summaries: dict[UUID, ConversationSummary] = {}
        self._evidence: dict[tuple[UUID, UUID], EvidenceRecord] = {}
        self._citations: dict[tuple[UUID, UUID], CitationRecord] = {}
        self._feedback: dict[UUID, FeedbackRecord] = {}
        self._feedback_keys: dict[tuple[UUID, str, str], UUID] = {}

    async def create_conversation(self, conversation: ConversationRecord) -> ConversationRecord:
        async with self._lock:
            existing = self._conversations.get(conversation.conversation_id)
            if existing is not None:
                if (
                    existing.space_id == conversation.space_id
                    and existing.owner_id == conversation.owner_id
                ):
                    return existing
                raise QAContractError("Conversation identity already exists with another owner")
            self._conversations[conversation.conversation_id] = conversation
            return conversation

    async def get_conversation(self, conversation_id: UUID) -> ConversationRecord | None:
        async with self._lock:
            return self._conversations.get(conversation_id)

    async def set_reasoning_effort(
        self, conversation_id: UUID, effort: ReasoningEffort
    ) -> ConversationRecord:
        async with self._lock:
            conversation = self._require_conversation(conversation_id)
            if conversation.archived_at is not None:
                raise QAContractError("Conversation does not exist")
            updated = replace(
                conversation,
                reasoning_effort=effort,
                updated_at=max(
                    datetime.now(UTC), conversation.updated_at + timedelta(microseconds=1)
                ),
            )
            self._conversations[conversation_id] = updated
            return updated

    async def set_workspace_path(
        self, conversation_id: UUID, workspace_path: str | None
    ) -> ConversationRecord:
        async with self._lock:
            conversation = self._require_conversation(conversation_id)
            if conversation.archived_at is not None:
                raise QAContractError("Conversation does not exist")
            updated = replace(
                conversation,
                workspace_path=workspace_path,
                updated_at=max(
                    datetime.now(UTC), conversation.updated_at + timedelta(microseconds=1)
                ),
            )
            self._conversations[conversation_id] = updated
            return updated

    async def list_conversations(
        self, space_id: UUID, owner_id: str
    ) -> tuple[ConversationRecord, ...]:
        async with self._lock:
            return tuple(
                sorted(
                    (
                        conversation
                        for conversation in self._conversations.values()
                        if conversation.space_id == space_id
                        and conversation.owner_id == owner_id
                        and conversation.archived_at is None
                    ),
                    key=lambda conversation: (
                        conversation.updated_at,
                        str(conversation.conversation_id),
                    ),
                    reverse=True,
                )
            )

    async def archive_conversation(self, conversation_id: UUID) -> ConversationRecord | None:
        async with self._lock:
            conversation = self._conversations.get(conversation_id)
            if conversation is None:
                return None
            if conversation.archived_at is not None:
                return conversation
            archived = replace(conversation, archived_at=datetime.now(UTC))
            self._conversations[conversation_id] = archived
            return archived

    async def append_message(self, message: MessageRecord) -> MessageRecord:
        async with self._lock:
            if message.role is not MessageRole.USER:
                raise QAContractError("Assistant messages require atomic terminal publication")
            self._validate_message_owner(message)
            if message.idempotency_key is not None:
                key = (message.conversation_id, message.idempotency_key)
                existing_id = self._message_keys.get(key)
                if existing_id is not None:
                    existing = self._messages[existing_id]
                    if _same_message_command(existing, message):
                        return existing
                    raise QAContractError("Message idempotency key has conflicting content")
            if message.message_id in self._messages:
                raise QAContractError("Message identity already exists")
            self._messages[message.message_id] = message
            if message.idempotency_key is not None:
                self._message_keys[(message.conversation_id, message.idempotency_key)] = (
                    message.message_id
                )
            conversation = self._conversations[message.conversation_id]
            if message.created_at > conversation.updated_at:
                self._conversations[message.conversation_id] = replace(
                    conversation, updated_at=message.created_at
                )
            return message

    async def get_message(self, message_id: UUID) -> MessageRecord | None:
        async with self._lock:
            return self._messages.get(message_id)

    async def list_messages(self, conversation_id: UUID) -> tuple[MessageRecord, ...]:
        async with self._lock:
            self._require_conversation(conversation_id)
            return tuple(
                sorted(
                    (
                        message
                        for message in self._messages.values()
                        if message.conversation_id == conversation_id
                    ),
                    key=lambda message: (message.created_at, str(message.message_id)),
                )
            )

    async def list_conversation_summaries(
        self, conversation_id: UUID
    ) -> tuple[ConversationSummary, ...]:
        async with self._lock:
            self._require_conversation(conversation_id)
            return tuple(
                sorted(
                    (
                        item
                        for item in self._summaries.values()
                        if item.conversation_id == conversation_id
                    ),
                    key=lambda item: (item.created_at, str(item.summary_id)),
                )
            )

    async def create_conversation_summary(
        self, summary: ConversationSummary
    ) -> ConversationSummary:
        async with self._lock:
            conversation = self._require_conversation(summary.conversation_id)
            if conversation.space_id != summary.space_id:
                raise QAContractError("Conversation summary crosses Space boundary")
            for existing in self._summaries.values():
                if (
                    existing.conversation_id == summary.conversation_id
                    and existing.covered_end_message_id == summary.covered_end_message_id
                    and existing.prompt_version == summary.prompt_version
                ):
                    return existing
            self._summaries[summary.summary_id] = summary
            return summary

    async def create_turn(
        self, run: ConversationRun, user_message: MessageRecord
    ) -> ConversationRun:
        """Persist a user message and parent Run in the same in-memory critical section."""
        async with self._lock:
            conversation = self._require_conversation(run.conversation_id)
            if (
                conversation.archived_at is not None
                or conversation.space_id != run.space_id
                or conversation.owner_id != run.caller_id
                or user_message.role is not MessageRole.USER
                or user_message.run_id is not None
                or user_message.message_id != run.user_message_id
                or user_message.conversation_id != run.conversation_id
                or user_message.space_id != run.space_id
            ):
                raise QAContractError("Conversation turn ownership is invalid")
            key = (run.space_id, run.caller_id, run.idempotency_key)
            existing_id = self._conversation_run_keys.get(key)
            if existing_id is not None:
                existing = self._conversation_runs[existing_id]
                existing_message = self._messages[existing.user_message_id]
                if _same_conversation_turn(existing, existing_message, run, user_message):
                    return existing
                raise QAContractError("ConversationRun idempotency key has conflicting content")
            if run.run_id in self._conversation_runs or run.user_message_id in self._messages:
                raise QAContractError("Conversation turn identity already exists")
            self._messages[user_message.message_id] = user_message
            self._message_keys[(user_message.conversation_id, run.idempotency_key)] = (
                user_message.message_id
            )
            self._conversation_runs[run.run_id] = run
            self._conversation_run_keys[key] = run.run_id
            if user_message.created_at > conversation.updated_at:
                self._conversations[conversation.conversation_id] = replace(
                    conversation, updated_at=user_message.created_at
                )
            return run

    async def create_context_compaction_run(self, run: ConversationRun) -> ConversationRun:
        async with self._lock:
            if run.run_kind is not ConversationRunKind.CONTEXT_COMPACTION:
                raise QAContractError("Context compaction Run kind is invalid")
            conversation = self._require_conversation(run.conversation_id)
            message = self._messages.get(run.user_message_id)
            if (
                message is None
                or message.role is not MessageRole.USER
                or message.conversation_id != run.conversation_id
                or message.space_id != run.space_id
                or conversation.owner_id != run.caller_id
                or conversation.space_id != run.space_id
            ):
                raise QAContractError("Context compaction Run ownership is invalid")
            key = (run.space_id, run.caller_id, run.idempotency_key)
            existing_id = self._conversation_run_keys.get(key)
            if existing_id is not None:
                existing = self._conversation_runs[existing_id]
                if (
                    existing.conversation_id == run.conversation_id
                    and existing.user_message_id == run.user_message_id
                ):
                    return existing
                raise QAContractError("Context compaction idempotency key has conflicting content")
            self._conversation_runs[run.run_id] = run
            self._conversation_run_keys[key] = run.run_id
            return run

    async def get_conversation_run(self, run_id: UUID) -> ConversationRun | None:
        async with self._lock:
            return self._conversation_runs.get(run_id)

    async def list_conversation_runs(self, conversation_id: UUID) -> tuple[ConversationRun, ...]:
        async with self._lock:
            self._require_conversation(conversation_id)
            return tuple(
                sorted(
                    (
                        run
                        for run in self._conversation_runs.values()
                        if run.conversation_id == conversation_id
                    ),
                    key=lambda run: (run.created_at, str(run.run_id)),
                )
            )

    async def request_conversation_cancel(self, run_id: UUID) -> ConversationRun:
        async with self._lock:
            run = self._conversation_runs.get(run_id)
            if run is None:
                raise QAContractError("ConversationRun does not exist")
            if run.status in {
                ConversationRunStatus.CANCEL_REQUESTED,
                ConversationRunStatus.COMPLETED,
                ConversationRunStatus.REFUSED,
                ConversationRunStatus.FAILED,
                ConversationRunStatus.CANCELLED,
                ConversationRunStatus.TIMED_OUT,
            }:
                return run
            updated = replace(
                run,
                status=ConversationRunStatus.CANCEL_REQUESTED,
                cancellation_requested=True,
                updated_at=datetime.now(UTC),
            )
            self._conversation_runs[run_id] = updated
            return updated

    async def reopen_clarification(self, run_id: UUID, *, clarification_id: str) -> ConversationRun:
        async with self._lock:
            run = self._require_assistant_conversation_run(run_id)
            clarification = run.result.clarification if run.result is not None else None
            if (
                run.status is not ConversationRunStatus.WAITING_CLARIFICATION
                or clarification is None
                or clarification.clarification_id != clarification_id
                or clarification.continuation is None
                or run.cancellation_requested
            ):
                raise QAContractError("Conversation clarification cannot be resumed")
            reopened = replace(
                run,
                status=ConversationRunStatus.CREATED,
                result=None,
                updated_at=datetime.now(UTC),
            )
            self._conversation_runs[run_id] = reopened
            return reopened

    async def prepare_conversation_recovery(self) -> tuple[UUID, ...]:
        async with self._lock:
            return tuple(
                run.run_id
                for run in self._conversation_runs.values()
                if run.status
                in {
                    ConversationRunStatus.CREATED,
                    ConversationRunStatus.QUEUED,
                    ConversationRunStatus.RUNNING,
                    ConversationRunStatus.CANCEL_REQUESTED,
                }
            )

    async def prepare_assistant_recovery(self) -> tuple[UUID, ...]:
        async with self._lock:
            now = datetime.now(UTC)
            recovered: list[UUID] = []
            for run_id, run in self._conversation_runs.items():
                if run.run_kind is not ConversationRunKind.ASSISTANT_TURN:
                    continue
                if run.status not in {
                    ConversationRunStatus.CREATED,
                    ConversationRunStatus.QUEUED,
                    ConversationRunStatus.RUNNING,
                    ConversationRunStatus.CANCEL_REQUESTED,
                }:
                    continue
                lease = self._conversation_run_leases.get(run_id)
                if lease is not None and lease[1] > now:
                    continue
                self._conversation_run_leases.pop(run_id, None)
                if run.status is ConversationRunStatus.RUNNING:
                    run = replace(
                        run,
                        status=ConversationRunStatus.QUEUED,
                        updated_at=now,
                    )
                    self._conversation_runs[run_id] = run
                recovered.append(run_id)
            return tuple(recovered)

    async def prepare_context_compaction_recovery(self) -> tuple[UUID, ...]:
        async with self._lock:
            now = datetime.now(UTC)
            recovered: list[UUID] = []
            for run_id, run in self._conversation_runs.items():
                if run.run_kind is not ConversationRunKind.CONTEXT_COMPACTION:
                    continue
                if run.status not in {
                    ConversationRunStatus.CREATED,
                    ConversationRunStatus.QUEUED,
                    ConversationRunStatus.RUNNING,
                    ConversationRunStatus.CANCEL_REQUESTED,
                }:
                    continue
                lease = self._conversation_run_leases.get(run_id)
                if lease is not None and lease[1] > now:
                    continue
                self._conversation_run_leases.pop(run_id, None)
                if run.status is ConversationRunStatus.RUNNING:
                    self._conversation_runs[run_id] = replace(
                        run, status=ConversationRunStatus.QUEUED, updated_at=now
                    )
                recovered.append(run_id)
            return tuple(recovered)

    async def claim_conversation_run(
        self, run_id: UUID, *, lease_owner: str, lease_seconds: int
    ) -> ConversationRun | None:
        if not lease_owner.strip() or lease_seconds < 1:
            raise ValueError("ConversationRun lease owner and duration are required")
        async with self._lock:
            run = self._conversation_runs.get(run_id)
            if run is None or run.run_kind not in {
                ConversationRunKind.ASSISTANT_TURN,
                ConversationRunKind.CONTEXT_COMPACTION,
                ConversationRunKind.SKILL,
                ConversationRunKind.GROUNDED_QA,
            }:
                return None
            if run.status in {
                ConversationRunStatus.COMPLETED,
                ConversationRunStatus.REFUSED,
                ConversationRunStatus.FAILED,
                ConversationRunStatus.CANCELLED,
                ConversationRunStatus.TIMED_OUT,
                ConversationRunStatus.WAITING_CLARIFICATION,
            }:
                return run
            now = datetime.now(UTC)
            lease = self._conversation_run_leases.get(run_id)
            if lease is not None and lease[0] != lease_owner and lease[1] > now:
                return None
            self._conversation_run_leases[run_id] = (
                lease_owner,
                now + timedelta(seconds=lease_seconds),
            )
            if run.status in {ConversationRunStatus.CREATED, ConversationRunStatus.QUEUED}:
                run = replace(run, status=ConversationRunStatus.RUNNING, updated_at=now)
                self._conversation_runs[run_id] = run
            return run

    async def renew_conversation_run_lease(
        self, run_id: UUID, *, lease_owner: str, lease_seconds: int
    ) -> bool:
        if not lease_owner.strip() or lease_seconds < 1:
            raise ValueError("ConversationRun lease owner and duration are required")
        async with self._lock:
            run = self._conversation_runs.get(run_id)
            lease = self._conversation_run_leases.get(run_id)
            if (
                run is None
                or lease is None
                or lease[0] != lease_owner
                or run.status
                in {
                    ConversationRunStatus.COMPLETED,
                    ConversationRunStatus.REFUSED,
                    ConversationRunStatus.FAILED,
                    ConversationRunStatus.CANCELLED,
                    ConversationRunStatus.TIMED_OUT,
                    ConversationRunStatus.WAITING_CLARIFICATION,
                }
            ):
                return False
            self._conversation_run_leases[run_id] = (
                lease_owner,
                datetime.now(UTC) + timedelta(seconds=lease_seconds),
            )
            return True

    async def release_conversation_run_lease(self, run_id: UUID, *, lease_owner: str) -> None:
        async with self._lock:
            lease = self._conversation_run_leases.get(run_id)
            if lease is not None and lease[0] == lease_owner:
                self._conversation_run_leases.pop(run_id, None)

    async def promote_to_skill(
        self,
        run_id: UUID,
        *,
        run_kind: ConversationRunKind,
        selection_source: ConversationRunSelectionSource,
        skill: FixedSkillIdentity,
        core_prompt_version: str,
    ) -> ConversationRun:
        async with self._lock:
            run = self._conversation_runs.get(run_id)
            if run is None or run.run_kind is not ConversationRunKind.ASSISTANT_TURN:
                raise QAContractError("Assistant ConversationRun cannot be promoted")
            if run.status in {
                ConversationRunStatus.COMPLETED,
                ConversationRunStatus.REFUSED,
                ConversationRunStatus.FAILED,
                ConversationRunStatus.CANCELLED,
                ConversationRunStatus.TIMED_OUT,
            }:
                return run
            promoted = replace(
                run,
                run_kind=run_kind,
                selection_source=selection_source,
                router_version="assistant-router-decision-v1",
                skill=skill,
                core_prompt_version=core_prompt_version,
                updated_at=datetime.now(UTC),
            )
            self._conversation_runs[run_id] = promoted
            return promoted

    async def publish_direct_message(
        self,
        *,
        run_id: UUID,
        message: MessageRecord,
        usage: ConversationRunUsage,
        model_identity: str,
        refused: bool = False,
    ) -> ConversationRun:
        async with self._lock:
            run = self._require_executable_conversation_run(run_id)
            if run.status in {
                ConversationRunStatus.COMPLETED,
                ConversationRunStatus.REFUSED,
                ConversationRunStatus.FAILED,
                ConversationRunStatus.CANCELLED,
                ConversationRunStatus.TIMED_OUT,
            }:
                return run
            if run.cancellation_requested:
                return self._cancel_assistant_conversation_run(run)
            self._validate_direct_message(run, message)
            if message.message_id in self._messages:
                raise QAContractError("Assistant message identity already exists")
            self._messages[message.message_id] = message
            now = message.created_at
            completed = replace(
                run,
                status=(
                    ConversationRunStatus.REFUSED if refused else ConversationRunStatus.COMPLETED
                ),
                error_code=None,
                model_identity=model_identity,
                usage=usage,
                result=AssistantResult(
                    AssistantResultKind.DIRECT_MESSAGE, message_id=message.message_id
                ),
                updated_at=now,
            )
            self._conversation_runs[run_id] = completed
            self._conversation_run_leases.pop(run_id, None)
            conversation = self._conversations[run.conversation_id]
            if now > conversation.updated_at:
                self._conversations[run.conversation_id] = replace(conversation, updated_at=now)
            return completed

    async def publish_existing_skill_result(
        self,
        *,
        run_id: UUID,
        message_id: UUID,
        usage: ConversationRunUsage,
        model_identity: str,
        refused: bool = False,
    ) -> ConversationRun:
        async with self._lock:
            run = self._require_executable_conversation_run(run_id)
            if run.status in {
                ConversationRunStatus.COMPLETED,
                ConversationRunStatus.REFUSED,
                ConversationRunStatus.FAILED,
                ConversationRunStatus.CANCELLED,
                ConversationRunStatus.TIMED_OUT,
            }:
                return run
            if run.cancellation_requested:
                return self._cancel_assistant_conversation_run(run)
            message = self._messages.get(message_id)
            if (
                message is None
                or message.role is not MessageRole.ASSISTANT
                or message.run_id != run.run_id
                or message.conversation_id != run.conversation_id
                or message.space_id != run.space_id
            ):
                raise QAContractError(
                    "Existing Skill message does not belong to the ConversationRun"
                )
            completed = replace(
                run,
                status=(
                    ConversationRunStatus.REFUSED if refused else ConversationRunStatus.COMPLETED
                ),
                error_code=None,
                model_identity=model_identity,
                usage=usage,
                result=AssistantResult(AssistantResultKind.SKILL_RESULT, message_id=message_id),
                updated_at=datetime.now(UTC),
            )
            self._conversation_runs[run_id] = completed
            self._conversation_run_leases.pop(run_id, None)
            return completed

    async def publish_clarification(
        self,
        *,
        run_id: UUID,
        clarification: Clarification,
        usage: ConversationRunUsage,
        model_identity: str,
    ) -> ConversationRun:
        async with self._lock:
            run = self._require_executable_conversation_run(run_id)
            if run.status in {
                ConversationRunStatus.COMPLETED,
                ConversationRunStatus.REFUSED,
                ConversationRunStatus.FAILED,
                ConversationRunStatus.CANCELLED,
                ConversationRunStatus.TIMED_OUT,
                ConversationRunStatus.WAITING_CLARIFICATION,
            }:
                return run
            if run.cancellation_requested:
                return self._cancel_assistant_conversation_run(run)
            clarified = replace(
                run,
                status=ConversationRunStatus.WAITING_CLARIFICATION,
                error_code=None,
                model_identity=model_identity,
                usage=usage,
                result=AssistantResult(
                    AssistantResultKind.CLARIFICATION,
                    clarification=clarification,
                ),
                updated_at=datetime.now(UTC),
            )
            self._conversation_runs[run_id] = clarified
            self._conversation_run_leases.pop(run_id, None)
            return clarified

    async def wait_for_approval(self, run_id: UUID) -> ConversationRun:
        async with self._lock:
            run = self._require_assistant_conversation_run(run_id)
            if run.status in {
                ConversationRunStatus.COMPLETED,
                ConversationRunStatus.REFUSED,
                ConversationRunStatus.FAILED,
                ConversationRunStatus.CANCELLED,
                ConversationRunStatus.TIMED_OUT,
                ConversationRunStatus.WAITING_APPROVAL,
            }:
                return run
            if run.cancellation_requested:
                return self._cancel_assistant_conversation_run(run)
            waiting = replace(
                run,
                status=ConversationRunStatus.WAITING_APPROVAL,
                error_code=None,
                updated_at=datetime.now(UTC),
            )
            self._conversation_runs[run_id] = waiting
            self._conversation_run_leases.pop(run_id, None)
            return waiting

    async def fail_conversation_run(self, run_id: UUID, *, error_code: str) -> ConversationRun:
        if not error_code.strip():
            raise ValueError("ConversationRun failure requires an error code")
        async with self._lock:
            run = self._require_executable_conversation_run(run_id)
            if run.status in {
                ConversationRunStatus.COMPLETED,
                ConversationRunStatus.REFUSED,
                ConversationRunStatus.FAILED,
                ConversationRunStatus.CANCELLED,
                ConversationRunStatus.TIMED_OUT,
            }:
                return run
            if run.cancellation_requested:
                return self._cancel_assistant_conversation_run(run)
            failed = replace(
                run,
                status=ConversationRunStatus.FAILED,
                error_code=error_code,
                result=None,
                updated_at=datetime.now(UTC),
            )
            self._conversation_runs[run_id] = failed
            self._conversation_run_leases.pop(run_id, None)
            return failed

    async def cancel_conversation_run(self, run_id: UUID) -> ConversationRun:
        async with self._lock:
            run = self._require_executable_conversation_run(run_id)
            if run.status in {
                ConversationRunStatus.COMPLETED,
                ConversationRunStatus.REFUSED,
                ConversationRunStatus.FAILED,
                ConversationRunStatus.CANCELLED,
                ConversationRunStatus.TIMED_OUT,
            }:
                return run
            return self._cancel_assistant_conversation_run(run)

    def _require_assistant_conversation_run(self, run_id: UUID) -> ConversationRun:
        run = self._conversation_runs.get(run_id)
        if run is None or run.run_kind is not ConversationRunKind.ASSISTANT_TURN:
            raise QAContractError("Assistant ConversationRun does not exist")
        return run

    def _require_executable_conversation_run(self, run_id: UUID) -> ConversationRun:
        run = self._conversation_runs.get(run_id)
        if run is None or run.run_kind not in {
            ConversationRunKind.ASSISTANT_TURN,
            ConversationRunKind.CONTEXT_COMPACTION,
            ConversationRunKind.SKILL,
            ConversationRunKind.GROUNDED_QA,
        }:
            raise QAContractError("ConversationRun does not exist")
        return run

    async def complete_context_compaction(
        self,
        run_id: UUID,
        *,
        usage: ConversationRunUsage,
        model_identity: str,
    ) -> ConversationRun:
        async with self._lock:
            run = self._conversation_runs.get(run_id)
            if run is None or run.run_kind is not ConversationRunKind.CONTEXT_COMPACTION:
                raise QAContractError("Context compaction Run does not exist")
            if run.status in {
                ConversationRunStatus.COMPLETED,
                ConversationRunStatus.FAILED,
                ConversationRunStatus.CANCELLED,
                ConversationRunStatus.TIMED_OUT,
            }:
                return run
            completed = replace(
                run,
                status=ConversationRunStatus.COMPLETED,
                usage=usage,
                model_identity=model_identity,
                updated_at=datetime.now(UTC),
            )
            self._conversation_runs[run_id] = completed
            self._conversation_run_leases.pop(run_id, None)
            return completed

    @staticmethod
    def _validate_direct_message(run: ConversationRun, message: MessageRecord) -> None:
        if (
            message.role is not MessageRole.ASSISTANT
            or message.run_id != run.run_id
            or message.conversation_id != run.conversation_id
            or message.space_id != run.space_id
            or message.idempotency_key is not None
        ):
            raise QAContractError("Assistant message does not belong to the ConversationRun")

    def _cancel_assistant_conversation_run(self, run: ConversationRun) -> ConversationRun:
        cancelled = replace(
            run,
            status=ConversationRunStatus.CANCELLED,
            cancellation_requested=True,
            error_code=None,
            result=None,
            updated_at=datetime.now(UTC),
        )
        self._conversation_runs[run.run_id] = cancelled
        self._conversation_run_leases.pop(run.run_id, None)
        return cancelled

    async def create_run(self, run: QARunRecord) -> QARunRecord:
        async with self._lock:
            conversation = self._require_conversation(run.conversation_id)
            if conversation.space_id != run.space_id or conversation.owner_id != run.caller_id:
                raise QAContractError("QA run does not belong to the conversation owner and Space")
            question = self._messages.get(run.question_message_id)
            if question is None or question.role is not MessageRole.USER:
                raise QAContractError("QA run must reference an existing user question")
            if question.conversation_id != run.conversation_id or question.space_id != run.space_id:
                raise QAContractError("QA run question does not belong to its conversation")
            if run.status is not QAStatus.CREATED or run.result is not None:
                raise QAContractError("New QA attempts must start without a result")

            key = (run.space_id, run.caller_id, run.idempotency_key)
            existing_attempt_id = self._run_keys.get(key)
            if existing_attempt_id is not None:
                existing = self._attempts[existing_attempt_id]
                if _same_run_command(existing, run):
                    return existing
                raise QAContractError("QA run idempotency key has conflicting ownership")

            self._validate_new_attempt(run)
            if run.attempt.attempt_id in self._attempts:
                raise QAContractError("QA attempt identity already exists")
            self._attempts[run.attempt.attempt_id] = run
            self._latest_attempts[run.run_id] = run.attempt.attempt_id
            self._run_keys[key] = run.attempt.attempt_id
            self._create_legacy_conversation_run(run)
            return run

    async def get_run(self, run_id: UUID) -> QARunRecord | None:
        async with self._lock:
            attempt_id = self._latest_attempts.get(run_id)
            return self._attempts.get(attempt_id) if attempt_id is not None else None

    async def list_runs(self, conversation_id: UUID) -> tuple[QARunRecord, ...]:
        async with self._lock:
            self._require_conversation(conversation_id)
            runs = (
                self._attempts[attempt_id]
                for attempt_id in self._latest_attempts.values()
                if self._attempts[attempt_id].conversation_id == conversation_id
            )
            return tuple(sorted(runs, key=lambda run: (run.created_at, str(run.run_id))))

    async def transition_run(
        self,
        run_id: UUID,
        event: QAEvent,
        *,
        error_code: str | None = None,
    ) -> QARunRecord:
        if event in {QAEvent.COMPLETE, QAEvent.REFUSE, QAEvent.CONFLICT}:
            raise QAContractError("Business terminal transitions require atomic publication")
        async with self._lock:
            run = self._require_latest_run(run_id)
            status = transition_qa_status(run.status, event)
            expected_error = {
                QAStatus.FAILED: error_code,
                QAStatus.TIMED_OUT: QAErrorCode.TIMED_OUT.value,
                QAStatus.CANCELLED: QAErrorCode.CANCELLED.value,
            }.get(status)
            if status is QAStatus.FAILED and not expected_error:
                raise QAContractError("Failed QA runs require a safe error code")
            updated = replace(
                run,
                status=status,
                cancellation_requested=(
                    run.cancellation_requested or status is QAStatus.CANCEL_REQUESTED
                ),
                error_code=expected_error,
                updated_at=datetime.now(UTC),
            )
            self._attempts[run.attempt.attempt_id] = updated
            self._project_conversation_run(updated)
            return updated

    async def request_cancel(self, run_id: UUID) -> QARunRecord:
        async with self._lock:
            run = self._require_latest_run(run_id)
            if run.status is QAStatus.CANCEL_REQUESTED or run.status in (
                _RUNTIME_TERMINAL | _BUSINESS_TERMINAL
            ):
                return run
            updated = replace(
                run,
                status=transition_qa_status(run.status, QAEvent.REQUEST_CANCEL),
                cancellation_requested=True,
                updated_at=datetime.now(UTC),
            )
            self._attempts[run.attempt.attempt_id] = updated
            self._project_conversation_run(updated)
            return updated

    async def save_usage(self, run_id: UUID, usage: QARunUsage) -> QARunRecord:
        async with self._lock:
            run = self._require_latest_run(run_id)
            if run.usage == usage:
                return run
            if run.status in _RUNTIME_TERMINAL | _BUSINESS_TERMINAL:
                raise QAContractError("QA usage cannot change after terminal publication")
            if not _usage_is_monotonic(run.usage, usage):
                raise QAContractError("QA usage updates must be monotonic")
            updated = replace(run, usage=usage, updated_at=datetime.now(UTC))
            self._attempts[run.attempt.attempt_id] = updated
            self._project_conversation_run(updated)
            return updated

    async def save_evidence(self, evidence: EvidenceRecord) -> EvidenceRecord:
        async with self._lock:
            run = self._require_latest_run(evidence.run_id)
            if run.attempt.attempt_id != evidence.attempt_id:
                raise QAContractError("Evidence does not belong to the current QA attempt")
            if evidence.candidate.space_id != run.space_id:
                raise QAContractError("Evidence does not belong to the QA run Space")
            if run.status in _RUNTIME_TERMINAL | _BUSINESS_TERMINAL:
                raise QAContractError("Evidence cannot be added to a terminal QA run")
            key = (evidence.attempt_id, evidence.candidate.evidence_id)
            existing = self._evidence.get(key)
            if existing is not None:
                if existing == evidence:
                    return existing
                raise QAContractError("Evidence identity has conflicting immutable data")
            self._evidence[key] = evidence
            return evidence

    async def list_evidence(self, attempt_id: UUID) -> tuple[EvidenceRecord, ...]:
        async with self._lock:
            return tuple(
                value
                for (stored_attempt_id, _evidence_id), value in self._evidence.items()
                if stored_attempt_id == attempt_id
            )

    async def publish_terminal(
        self,
        *,
        run_id: UUID,
        result: QAResult,
        answer_message: MessageRecord,
        citations: tuple[CitationRecord, ...] = (),
    ) -> QARunRecord:
        async with self._lock:
            run = self._require_latest_run(run_id)
            if run.status in _BUSINESS_TERMINAL:
                if _same_publication(
                    run,
                    result,
                    answer_message,
                    citations,
                    self._citations,
                    self._messages,
                ):
                    return run
                raise QAContractError("QA run already has a different terminal result")
            if run.status is not QAStatus.VERIFYING or run.cancellation_requested:
                raise QAContractError("QA run is not eligible for terminal publication")
            if answer_message.role is not MessageRole.ASSISTANT:
                raise QAContractError("Terminal publication requires an assistant message")
            if (
                answer_message.run_id != run.run_id
                or answer_message.conversation_id != run.conversation_id
                or answer_message.space_id != run.space_id
            ):
                raise QAContractError("Terminal message does not belong to the QA run")
            if answer_message.content != _result_content(result):
                raise QAContractError("Terminal message content does not match the QA result")
            if answer_message.message_id in self._messages:
                raise QAContractError("Terminal message identity already exists")

            evidence = tuple(
                item
                for (attempt_id, _evidence_id), item in self._evidence.items()
                if attempt_id == run.attempt.attempt_id
            )
            self._validate_result_evidence(
                run=run,
                result=result,
                message=answer_message,
                evidence=evidence,
                citations=citations,
            )

            status = terminal_status_for_result(result)
            updated = replace(
                run,
                status=status,
                result=result,
                answer_message_id=answer_message.message_id,
                updated_at=datetime.now(UTC),
            )
            self._messages[answer_message.message_id] = answer_message
            for citation in citations:
                self._citations[(citation.attempt_id, citation.citation.evidence_id)] = citation
            self._attempts[run.attempt.attempt_id] = updated
            self._project_conversation_run(updated)
            return updated

    async def list_citations(self, attempt_id: UUID) -> tuple[CitationRecord, ...]:
        async with self._lock:
            return tuple(
                value
                for (stored_attempt_id, _evidence_id), value in self._citations.items()
                if stored_attempt_id == attempt_id
            )

    async def submit_feedback(self, feedback: FeedbackRecord) -> FeedbackRecord:
        async with self._lock:
            conversation = self._require_conversation(feedback.conversation_id)
            if (
                conversation.space_id != feedback.space_id
                or conversation.owner_id != feedback.caller_id
            ):
                raise QAContractError(
                    "Feedback does not belong to the conversation owner and Space"
                )
            run = self._attempts.get(feedback.attempt_id)
            message = self._messages.get(feedback.message_id)
            if (
                run is None
                or run.run_id != feedback.run_id
                or run.conversation_id != feedback.conversation_id
                or run.space_id != feedback.space_id
                or run.answer_message_id != feedback.message_id
                or message is None
                or message.role is not MessageRole.ASSISTANT
            ):
                raise QAContractError("Feedback target does not belong to the published QA result")
            key = (feedback.run_id, feedback.caller_id, feedback.idempotency_key)
            existing_id = self._feedback_keys.get(key)
            if existing_id is not None:
                existing = self._feedback[existing_id]
                if _same_feedback_command(existing, feedback):
                    return existing
                raise QAContractError("Feedback idempotency key has conflicting content")
            if feedback.feedback_id in self._feedback:
                raise QAContractError("Feedback identity already exists")
            self._feedback[feedback.feedback_id] = feedback
            self._feedback_keys[key] = feedback.feedback_id
            return feedback

    async def get_feedback(self, feedback_id: UUID) -> FeedbackRecord | None:
        async with self._lock:
            return self._feedback.get(feedback_id)

    async def list_feedback(
        self, space_id: UUID, review_status: FeedbackReviewStatus | None = None
    ) -> tuple[FeedbackRecord, ...]:
        async with self._lock:
            records = [
                record
                for record in self._feedback.values()
                if record.space_id == space_id
                and (review_status is None or record.review_status is review_status)
            ]
            return tuple(
                sorted(records, key=lambda record: (record.created_at, str(record.feedback_id)))
            )

    async def review_feedback(self, review: FeedbackReviewRecord) -> FeedbackRecord:
        async with self._lock:
            current = self._feedback.get(review.feedback_id)
            if current is None:
                raise QAContractError("Feedback not found")
            if current.space_id != review.space_id:
                raise QAContractError("Feedback does not belong to the requested Space")
            evidence_ids = {
                evidence.candidate.evidence_id
                for (attempt_id, _evidence_id), evidence in self._evidence.items()
                if attempt_id == current.attempt_id
            }
            if not set(review.approved_evidence_ids).issubset(evidence_ids):
                raise QAContractError("Feedback review Evidence does not belong to the QA attempt")
            if current.review_status is not FeedbackReviewStatus.PENDING_REVIEW:
                if (
                    current.review_status is review.review_status
                    and current.reviewer_id == review.reviewer_id
                    and current.authorization_confirmed == review.authorization_confirmed
                    and current.redaction_complete == review.redaction_complete
                    and current.expected_behavior == review.expected_behavior
                    and current.approved_evidence_ids == review.approved_evidence_ids
                    and current.gold_answer_sha256 == review.gold_answer_sha256
                    and current.rejection_reason == review.rejection_reason
                ):
                    return current
                raise QAContractError("Feedback has already been reviewed")
            updated = replace(
                current,
                review_status=review.review_status,
                reviewer_id=review.reviewer_id,
                reviewed_at=review.reviewed_at,
                authorization_confirmed=review.authorization_confirmed,
                redaction_complete=review.redaction_complete,
                expected_behavior=review.expected_behavior,
                approved_evidence_ids=review.approved_evidence_ids,
                gold_answer_sha256=review.gold_answer_sha256,
                rejection_reason=review.rejection_reason,
            )
            self._feedback[review.feedback_id] = updated
            return updated

    def _validate_message_owner(self, message: MessageRecord) -> None:
        conversation = self._require_conversation(message.conversation_id)
        if conversation.space_id != message.space_id:
            raise QAContractError("Message does not belong to the conversation Space")

    def _validate_new_attempt(self, run: QARunRecord) -> None:
        latest_id = self._latest_attempts.get(run.run_id)
        if run.attempt.number == 1:
            if latest_id is not None:
                raise QAContractError("Initial QA run identity already exists")
            return
        previous_attempt_id = run.attempt.previous_attempt_id
        assert previous_attempt_id is not None
        if latest_id != previous_attempt_id:
            raise QAContractError("Retry does not follow the latest QA attempt")
        previous = self._attempts.get(previous_attempt_id)
        if previous is None or previous.status not in {QAStatus.FAILED, QAStatus.TIMED_OUT}:
            raise QAContractError("Retry predecessor is not an eligible terminal attempt")
        if (
            previous.conversation_id != run.conversation_id
            or previous.question_message_id != run.question_message_id
            or previous.space_id != run.space_id
            or previous.caller_id != run.caller_id
        ):
            raise QAContractError("Retry changes immutable QA run ownership")

    def _validate_result_evidence(
        self,
        *,
        run: QARunRecord,
        result: QAResult,
        message: MessageRecord,
        evidence: tuple[EvidenceRecord, ...],
        citations: tuple[CitationRecord, ...],
    ) -> None:
        if any(item.resolution_status is not CitationStatus.VALID for item in evidence):
            raise QAContractError("Terminal publication contains unavailable Evidence")
        evidence_by_id = {item.candidate.evidence_id: item for item in evidence}
        citation_ids = tuple(item.citation.evidence_id for item in citations)
        if len(citation_ids) != len(set(citation_ids)):
            raise QAContractError("Terminal publication contains duplicate Citation Evidence IDs")
        if any(item.citation.status is not CitationStatus.VALID for item in citations):
            raise QAContractError("Terminal publication contains a non-publishable Citation")
        if result.outcome is QAOutcome.ANSWER:
            assert result.answer is not None
            if len(citations) != len(result.answer.citations):
                raise QAContractError("Published answer citations are incomplete")
            if {item.citation for item in citations} != set(result.answer.citations):
                raise QAContractError("Citation records do not match the published answer")
            validate_answer_citations(
                result.answer,
                tuple(item.candidate for item in evidence),
                space_id=run.space_id,
            )
        elif citations:
            raise QAContractError("Only grounded answers may publish Citation records")
        if result.outcome is QAOutcome.CONFLICT:
            assert result.conflict is not None
            if any(
                evidence_id not in evidence_by_id for evidence_id in result.conflict.evidence_ids
            ):
                raise QAContractError("Conflict references Evidence outside the QA attempt")
            if (
                len(
                    {
                        evidence_by_id[evidence_id].candidate.source_id
                        for evidence_id in result.conflict.evidence_ids
                    }
                )
                < 2
            ):
                raise QAContractError("Conflict must preserve Evidence from at least two sources")
        for citation in citations:
            if (
                citation.run_id != run.run_id
                or citation.attempt_id != run.attempt.attempt_id
                or citation.message_id != message.message_id
                or citation.citation.evidence_id not in evidence_by_id
            ):
                raise QAContractError("Citation record does not belong to the terminal publication")

    def _require_conversation(self, conversation_id: UUID) -> ConversationRecord:
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            raise QAContractError("Conversation does not exist")
        return conversation

    def _require_latest_run(self, run_id: UUID) -> QARunRecord:
        attempt_id = self._latest_attempts.get(run_id)
        if attempt_id is None:
            raise QAContractError("QA run does not exist")
        return self._attempts[attempt_id]

    def _create_legacy_conversation_run(self, run: QARunRecord) -> None:
        existing = self._conversation_runs.get(run.run_id)
        parent = _conversation_run_from_qa(run)
        if existing is not None:
            if not _same_qa_parent_identity(existing, run):
                raise QAContractError("QA Run conflicts with its ConversationRun parent")
            self._conversation_runs[run.run_id] = _conversation_run_from_qa(run, existing=existing)
            return
        key = (run.space_id, run.caller_id, run.idempotency_key)
        conflicting_id = self._conversation_run_keys.get(key)
        if conflicting_id is not None and conflicting_id != run.run_id:
            raise QAContractError("QA Run idempotency key conflicts with a ConversationRun")
        self._conversation_runs[run.run_id] = parent
        self._conversation_run_keys[key] = run.run_id

    def _project_conversation_run(self, run: QARunRecord) -> None:
        parent = self._conversation_runs.get(run.run_id)
        if parent is None:
            self._create_legacy_conversation_run(run)
            return
        projected = _conversation_run_from_qa(run, existing=parent)
        if (
            (
                parent.run_kind in {ConversationRunKind.SKILL, ConversationRunKind.GROUNDED_QA}
                and parent.core_prompt_version
                in {
                    "assistant-base-prompt-v2",
                    "assistant-base-prompt-v3",
                    "assistant-base-prompt-v4",
                }
            )
            or (
                parent.run_kind is ConversationRunKind.ASSISTANT_TURN
                and parent.router_version == "assistant-agent-loop-v1"
                and parent.core_prompt_version
                in {
                    "assistant-base-prompt-v5",
                    "assistant-base-prompt-v6",
                    "assistant-base-prompt-v7",
                }
            )
            and parent.result is None
            and run.status in {QAStatus.COMPLETED, QAStatus.REFUSED}
        ):
            projected = replace(
                projected,
                status=ConversationRunStatus.RUNNING,
                result=None,
            )
        self._conversation_runs[run.run_id] = projected


def _result_content(result: QAResult) -> str:
    if result.answer is not None:
        return result.answer.text
    if result.refusal is not None:
        return result.refusal.message
    if result.conflict is not None:
        return result.conflict.message
    raise QAContractError("Infrastructure failures cannot create assistant messages")


def _same_message_command(existing: MessageRecord, requested: MessageRecord) -> bool:
    return (
        existing.conversation_id,
        existing.space_id,
        existing.role,
        existing.content,
        existing.run_id,
    ) == (
        requested.conversation_id,
        requested.space_id,
        requested.role,
        requested.content,
        requested.run_id,
    )


def _same_conversation_turn(
    existing_run: ConversationRun,
    existing_message: MessageRecord,
    requested_run: ConversationRun,
    requested_message: MessageRecord,
) -> bool:
    return (
        existing_run.conversation_id,
        existing_run.space_id,
        existing_run.caller_id,
        existing_run.idempotency_key,
        existing_run.selection_source,
        _idempotent_turn_kind(existing_run),
        existing_message.content,
    ) == (
        requested_run.conversation_id,
        requested_run.space_id,
        requested_run.caller_id,
        requested_run.idempotency_key,
        requested_run.selection_source,
        _idempotent_turn_kind(requested_run),
        requested_message.content,
    )


def _same_legacy_conversation_run_identity(
    existing: ConversationRun, requested: ConversationRun
) -> bool:
    return (
        existing.run_id,
        existing.conversation_id,
        existing.space_id,
        existing.caller_id,
        existing.user_message_id,
        existing.run_kind,
        existing.selection_source,
        existing.router_version,
        existing.core_prompt_version,
        existing.model_identity,
        existing.skill,
    ) == (
        requested.run_id,
        requested.conversation_id,
        requested.space_id,
        requested.caller_id,
        requested.user_message_id,
        requested.run_kind,
        requested.selection_source,
        requested.router_version,
        requested.core_prompt_version,
        requested.model_identity,
        requested.skill,
    )


def _idempotent_turn_kind(run: ConversationRun) -> str:
    if run.run_kind in {
        ConversationRunKind.ASSISTANT_TURN,
        ConversationRunKind.GROUNDED_QA,
        ConversationRunKind.SKILL,
    }:
        return "assistant-or-promoted"
    return run.run_kind.value


def _same_qa_parent_identity(existing: ConversationRun, run: QARunRecord) -> bool:
    if (
        existing.conversation_id != run.conversation_id
        or existing.space_id != run.space_id
        or existing.caller_id != run.caller_id
        or existing.user_message_id != run.question_message_id
    ):
        return False
    if existing.skill is not None:
        if run.versions.skill_content_sha256 is None:
            return False
        if existing.skill != FixedSkillIdentity(
            name=run.versions.skill_name,
            version=run.versions.skill_version,
            content_sha256=run.versions.skill_content_sha256,
        ):
            return False
    if existing.router_version == "legacy-v1":
        return _same_legacy_conversation_run_identity(existing, _conversation_run_from_qa(run))
    if existing.router_version == "assistant-agent-loop-v1":
        return (
            existing.run_kind is ConversationRunKind.ASSISTANT_TURN
            and existing.skill is None
            and existing.core_prompt_version
            in {"assistant-base-prompt-v5", "assistant-base-prompt-v6", "assistant-base-prompt-v7"}
            and run.versions.skill_name == "knowledge_agent"
        )
    return (
        existing.run_kind
        == (
            ConversationRunKind.GROUNDED_QA
            if run.versions.skill_name == "knowledge_qa"
            else ConversationRunKind.SKILL
        )
        and existing.selection_source
        in {
            ConversationRunSelectionSource.AUTO,
            ConversationRunSelectionSource.COMMAND,
        }
        and existing.core_prompt_version
        in {"assistant-base-prompt-v2", "assistant-base-prompt-v3", "assistant-base-prompt-v4"}
    )


def _conversation_run_from_qa(
    run: QARunRecord, *, existing: ConversationRun | None = None
) -> ConversationRun:
    status = {
        QAStatus.CREATED: ConversationRunStatus.CREATED,
        QAStatus.QUEUED: ConversationRunStatus.QUEUED,
        QAStatus.RUNNING: ConversationRunStatus.RUNNING,
        QAStatus.VERIFYING: ConversationRunStatus.RUNNING,
        QAStatus.COMPLETED: ConversationRunStatus.COMPLETED,
        QAStatus.REFUSED: ConversationRunStatus.REFUSED,
        QAStatus.FAILED: ConversationRunStatus.FAILED,
        QAStatus.CANCEL_REQUESTED: ConversationRunStatus.CANCEL_REQUESTED,
        QAStatus.CANCELLED: ConversationRunStatus.CANCELLED,
        QAStatus.TIMED_OUT: ConversationRunStatus.TIMED_OUT,
    }[run.status]
    skill = (
        FixedSkillIdentity(
            name=run.versions.skill_name,
            version=run.versions.skill_version,
            content_sha256=run.versions.skill_content_sha256,
        )
        if run.versions.skill_content_sha256 is not None
        else None
    )
    result = (
        AssistantResult(AssistantResultKind.SKILL_RESULT, message_id=run.answer_message_id)
        if run.answer_message_id is not None
        else None
    )
    return ConversationRun(
        run_id=run.run_id,
        conversation_id=run.conversation_id,
        space_id=run.space_id,
        caller_id=run.caller_id,
        user_message_id=run.question_message_id,
        idempotency_key=existing.idempotency_key if existing is not None else run.idempotency_key,
        run_kind=(
            existing.run_kind
            if existing is not None
            else (
                ConversationRunKind.GROUNDED_QA
                if run.versions.skill_name == "knowledge_qa"
                else ConversationRunKind.SKILL
            )
        ),
        selection_source=(
            existing.selection_source
            if existing is not None
            else ConversationRunSelectionSource.NONE
        ),
        status=status,
        cancellation_requested=run.cancellation_requested,
        error_code=run.error_code,
        router_version=existing.router_version if existing is not None else "legacy-v1",
        core_prompt_version=existing.core_prompt_version if existing is not None else "legacy-v1",
        model_identity=run.versions.model_identity,
        skill=existing.skill if existing is not None else skill,
        usage=ConversationRunUsage(
            input_tokens=run.usage.input_tokens,
            output_tokens=run.usage.output_tokens,
            model_latency_ms=run.usage.model_latency_ms,
        ),
        result=result,
        created_at=existing.created_at if existing is not None else run.created_at,
        updated_at=run.updated_at,
    )


def _same_run_command(existing: QARunRecord, requested: QARunRecord) -> bool:
    return (
        existing.conversation_id,
        existing.question_message_id,
        existing.space_id,
        existing.caller_id,
        existing.versions,
        existing.retrieval_scope,
    ) == (
        requested.conversation_id,
        requested.question_message_id,
        requested.space_id,
        requested.caller_id,
        requested.versions,
        requested.retrieval_scope,
    )


def _same_feedback_command(existing: FeedbackRecord, requested: FeedbackRecord) -> bool:
    return (
        existing.conversation_id,
        existing.message_id,
        existing.run_id,
        existing.attempt_id,
        existing.space_id,
        existing.caller_id,
        existing.decision,
        existing.note,
    ) == (
        requested.conversation_id,
        requested.message_id,
        requested.run_id,
        requested.attempt_id,
        requested.space_id,
        requested.caller_id,
        requested.decision,
        requested.note,
    )


def _same_publication(
    run: QARunRecord,
    result: QAResult,
    message: MessageRecord,
    citations: tuple[CitationRecord, ...],
    stored_citations: dict[tuple[UUID, UUID], CitationRecord],
    stored_messages: dict[UUID, MessageRecord],
) -> bool:
    stored_message = stored_messages.get(message.message_id)
    if (
        run.result != result
        or run.answer_message_id != message.message_id
        or stored_message is None
        or not _same_message_command(stored_message, message)
    ):
        return False
    stored = tuple(
        value
        for (attempt_id, _evidence_id), value in stored_citations.items()
        if attempt_id == run.attempt.attempt_id
    )
    return {_citation_identity(value) for value in stored} == {
        _citation_identity(value) for value in citations
    }


def _citation_identity(record: CitationRecord) -> tuple[UUID, UUID, Citation]:
    return (
        record.run_id,
        record.attempt_id,
        record.citation,
    )


def _usage_is_monotonic(previous: QARunUsage, current: QARunUsage) -> bool:
    if (
        current.input_tokens < previous.input_tokens
        or current.output_tokens < previous.output_tokens
        or current.model_calls < previous.model_calls
        or current.repair_attempts < previous.repair_attempts
        or current.model_latency_ms < previous.model_latency_ms
    ):
        return False
    current_phases = {timing.phase: timing.duration_ms for timing in current.phase_timings}
    return all(
        current_phases.get(timing.phase, -1) >= timing.duration_ms
        for timing in previous.phase_timings
    )


__all__ = ["InMemoryGroundedQARepository"]
