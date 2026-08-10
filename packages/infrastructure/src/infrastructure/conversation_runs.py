"""PostgreSQL adapter for the shared ConversationRun parent identity."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from domain.conversation_run import (
    AssistantResult,
    AssistantResultKind,
    Clarification,
    ClarificationContinuation,
    ClarificationKind,
    ConversationRun,
    ConversationRunKind,
    ConversationRunSelectionSource,
    ConversationRunStatus,
    ConversationRunUsage,
    FixedSkillIdentity,
    ResourceCandidate,
)
from domain.grounded_qa import QAContractError
from domain.qa_persistence import MessageRecord, MessageRole
from domain.reasoning import reasoning_profile_from_dict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .database import Database
from .orm import ConversationModel, ConversationRunModel, QAMessageModel

_TERMINAL = frozenset(
    {
        ConversationRunStatus.COMPLETED,
        ConversationRunStatus.REFUSED,
        ConversationRunStatus.FAILED,
        ConversationRunStatus.CANCELLED,
        ConversationRunStatus.TIMED_OUT,
    }
)


def _lease_is_active(model: ConversationRunModel, now: datetime) -> bool:
    return model.lease_owner is not None and (
        model.lease_expires_at is None or model.lease_expires_at > now
    )


def _clear_lease(model: ConversationRunModel) -> None:
    model.lease_owner = None
    model.lease_expires_at = None
    model.heartbeat_at = None


class PostgresConversationRunRepository:
    """Atomically persist the user message and shared parent Run."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def create_turn(
        self, run: ConversationRun, user_message: MessageRecord
    ) -> ConversationRun:
        async with self._database.transaction() as session:
            conversation = await session.get(
                ConversationModel, run.conversation_id, with_for_update=True
            )
            if (
                conversation is None
                or conversation.archived_at is not None
                or conversation.space_id != run.space_id
                or conversation.owner_id != run.caller_id
                or user_message.role is not MessageRole.USER
                or user_message.run_id is not None
                or user_message.message_id != run.user_message_id
                or user_message.conversation_id != run.conversation_id
                or user_message.space_id != run.space_id
            ):
                raise QAContractError("Conversation turn ownership is invalid")

            existing = (
                await session.execute(
                    select(ConversationRunModel)
                    .where(
                        ConversationRunModel.space_id == run.space_id,
                        ConversationRunModel.caller_id == run.caller_id,
                        ConversationRunModel.idempotency_key == run.idempotency_key,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if existing is not None:
                existing_message = await session.get(QAMessageModel, existing.user_message_id)
                if existing_message is not None and _same_turn(
                    existing, existing_message, run, user_message
                ):
                    return _run(existing)
                raise QAContractError("ConversationRun idempotency key has conflicting content")

            duplicate_message = (
                await session.execute(
                    select(QAMessageModel).where(
                        QAMessageModel.conversation_id == user_message.conversation_id,
                        QAMessageModel.idempotency_key == user_message.idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if duplicate_message is not None:
                raise QAContractError("Conversation message idempotency key has conflicting Run")

            session.add(
                QAMessageModel(
                    id=user_message.message_id,
                    conversation_id=user_message.conversation_id,
                    run_id=None,
                    space_id=user_message.space_id,
                    role=user_message.role.value,
                    content=user_message.content,
                    idempotency_key=user_message.idempotency_key,
                    created_at=user_message.created_at,
                )
            )
            # The parent has a direct FK to this append-only user message.
            await session.flush()
            session.add(_model(run))
            if user_message.created_at > conversation.updated_at:
                conversation.updated_at = user_message.created_at
        return run

    async def create_context_compaction_run(self, run: ConversationRun) -> ConversationRun:
        if run.run_kind is not ConversationRunKind.CONTEXT_COMPACTION:
            raise QAContractError("Context compaction Run kind is invalid")
        async with self._database.transaction() as session:
            conversation = await session.get(
                ConversationModel, run.conversation_id, with_for_update=True
            )
            message = await session.get(QAMessageModel, run.user_message_id)
            if (
                conversation is None
                or conversation.archived_at is not None
                or conversation.space_id != run.space_id
                or conversation.owner_id != run.caller_id
                or message is None
                or message.role != MessageRole.USER.value
                or message.conversation_id != run.conversation_id
                or message.space_id != run.space_id
            ):
                raise QAContractError("Context compaction Run ownership is invalid")
            existing = (
                await session.execute(
                    select(ConversationRunModel)
                    .where(
                        ConversationRunModel.space_id == run.space_id,
                        ConversationRunModel.caller_id == run.caller_id,
                        ConversationRunModel.idempotency_key == run.idempotency_key,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if existing is not None:
                stored = _run(existing)
                if (
                    stored.run_kind is ConversationRunKind.CONTEXT_COMPACTION
                    and stored.conversation_id == run.conversation_id
                    and stored.user_message_id == run.user_message_id
                ):
                    return stored
                raise QAContractError("Context compaction idempotency key has conflicting content")
            session.add(_model(run))
        return run

    async def get_conversation_run(self, run_id: UUID) -> ConversationRun | None:
        async with self._database.session() as session:
            model = await session.get(ConversationRunModel, run_id)
            return _run(model) if model is not None else None

    async def list_conversation_runs(self, conversation_id: UUID) -> tuple[ConversationRun, ...]:
        async with self._database.session() as session:
            models = (
                await session.execute(
                    select(ConversationRunModel)
                    .where(ConversationRunModel.conversation_id == conversation_id)
                    .order_by(ConversationRunModel.created_at, ConversationRunModel.id)
                )
            ).scalars()
            return tuple(_run(model) for model in models)

    async def request_conversation_cancel(self, run_id: UUID) -> ConversationRun:
        async with self._database.transaction() as session:
            model = await session.get(ConversationRunModel, run_id, with_for_update=True)
            if model is None:
                raise QAContractError("ConversationRun does not exist")
            current = _run(model)
            if (
                current.status in _TERMINAL
                or current.status is ConversationRunStatus.CANCEL_REQUESTED
            ):
                return current
            model.status = ConversationRunStatus.CANCEL_REQUESTED.value
            model.cancellation_requested = True
            await session.flush()
            return _run(model)

    async def reopen_clarification(self, run_id: UUID, *, clarification_id: str) -> ConversationRun:
        async with self._database.transaction() as session:
            model = await self._locked_assistant_run(session, run_id)
            current = _run(model)
            clarification = current.result.clarification if current.result is not None else None
            if (
                current.status is not ConversationRunStatus.WAITING_CLARIFICATION
                or clarification is None
                or clarification.clarification_id != clarification_id
                or clarification.continuation is None
                or current.cancellation_requested
            ):
                raise QAContractError("Conversation clarification cannot be resumed")
            model.status = ConversationRunStatus.CREATED.value
            model.error_code = None
            model.result = None
            model.updated_at = datetime.now(UTC)
            _clear_lease(model)
            await session.flush()
            return _run(model)

    async def prepare_conversation_recovery(self) -> tuple[UUID, ...]:
        async with self._database.session() as session:
            models = (
                await session.execute(
                    select(ConversationRunModel.id)
                    .where(
                        ConversationRunModel.status.in_(
                            (
                                ConversationRunStatus.CREATED.value,
                                ConversationRunStatus.QUEUED.value,
                                ConversationRunStatus.RUNNING.value,
                                ConversationRunStatus.CANCEL_REQUESTED.value,
                            )
                        )
                    )
                    .order_by(ConversationRunModel.created_at, ConversationRunModel.id)
                )
            ).scalars()
            return tuple(models)

    async def prepare_assistant_recovery(self) -> tuple[UUID, ...]:
        """Return unleased Assistant work and reset expired direct executions."""
        async with self._database.transaction() as session:
            models = (
                await session.execute(
                    select(ConversationRunModel)
                    .where(
                        ConversationRunModel.run_kind == ConversationRunKind.ASSISTANT_TURN.value,
                        ConversationRunModel.status.in_(
                            (
                                ConversationRunStatus.CREATED.value,
                                ConversationRunStatus.QUEUED.value,
                                ConversationRunStatus.RUNNING.value,
                                ConversationRunStatus.CANCEL_REQUESTED.value,
                            )
                        ),
                    )
                    .order_by(ConversationRunModel.created_at, ConversationRunModel.id)
                    .with_for_update()
                )
            ).scalars()
            now = datetime.now(UTC)
            recovered: list[UUID] = []
            for model in models:
                if _lease_is_active(model, now):
                    continue
                if model.status == ConversationRunStatus.RUNNING.value:
                    model.status = ConversationRunStatus.QUEUED.value
                    model.updated_at = now
                _clear_lease(model)
                recovered.append(model.id)
            return tuple(recovered)

    async def prepare_context_compaction_recovery(self) -> tuple[UUID, ...]:
        async with self._database.transaction() as session:
            models = (
                await session.execute(
                    select(ConversationRunModel)
                    .where(
                        ConversationRunModel.run_kind
                        == ConversationRunKind.CONTEXT_COMPACTION.value,
                        ConversationRunModel.status.in_(
                            (
                                ConversationRunStatus.CREATED.value,
                                ConversationRunStatus.QUEUED.value,
                                ConversationRunStatus.RUNNING.value,
                                ConversationRunStatus.CANCEL_REQUESTED.value,
                            )
                        ),
                    )
                    .order_by(ConversationRunModel.created_at, ConversationRunModel.id)
                    .with_for_update()
                )
            ).scalars()
            now = datetime.now(UTC)
            recovered: list[UUID] = []
            for model in models:
                if _lease_is_active(model, now):
                    continue
                if model.status == ConversationRunStatus.RUNNING.value:
                    model.status = ConversationRunStatus.QUEUED.value
                    model.updated_at = now
                _clear_lease(model)
                recovered.append(model.id)
            return tuple(recovered)

    async def claim_conversation_run(
        self, run_id: UUID, *, lease_owner: str, lease_seconds: int
    ) -> ConversationRun | None:
        if not lease_owner.strip() or lease_seconds < 1:
            raise ValueError("ConversationRun lease owner and duration are required")
        async with self._database.transaction() as session:
            model = await session.get(ConversationRunModel, run_id, with_for_update=True)
            if model is None or model.run_kind not in {
                ConversationRunKind.ASSISTANT_TURN.value,
                ConversationRunKind.CONTEXT_COMPACTION.value,
                ConversationRunKind.SKILL.value,
                ConversationRunKind.GROUNDED_QA.value,
            }:
                return None
            current = _run(model)
            if (
                current.status in _TERMINAL
                or current.status is ConversationRunStatus.WAITING_CLARIFICATION
            ):
                return current
            now = datetime.now(UTC)
            if _lease_is_active(model, now) and model.lease_owner != lease_owner:
                return None
            model.lease_owner = lease_owner
            model.lease_expires_at = now + timedelta(seconds=lease_seconds)
            model.heartbeat_at = now
            if current.status in {ConversationRunStatus.CREATED, ConversationRunStatus.QUEUED}:
                model.status = ConversationRunStatus.RUNNING.value
            model.updated_at = now
            await session.flush()
            return _run(model)

    async def renew_conversation_run_lease(
        self, run_id: UUID, *, lease_owner: str, lease_seconds: int
    ) -> bool:
        if not lease_owner.strip() or lease_seconds < 1:
            raise ValueError("ConversationRun lease owner and duration are required")
        async with self._database.transaction() as session:
            model = await session.get(ConversationRunModel, run_id, with_for_update=True)
            if model is None or model.lease_owner != lease_owner:
                return False
            current = _run(model)
            if (
                current.status in _TERMINAL
                or current.status is ConversationRunStatus.WAITING_CLARIFICATION
            ):
                return False
            now = datetime.now(UTC)
            model.heartbeat_at = now
            model.lease_expires_at = now + timedelta(seconds=lease_seconds)
            model.updated_at = now
            return True

    async def release_conversation_run_lease(self, run_id: UUID, *, lease_owner: str) -> None:
        async with self._database.transaction() as session:
            model = await session.get(ConversationRunModel, run_id, with_for_update=True)
            if model is not None and model.lease_owner == lease_owner:
                _clear_lease(model)

    async def promote_to_skill(
        self,
        run_id: UUID,
        *,
        run_kind: ConversationRunKind,
        selection_source: ConversationRunSelectionSource,
        skill: FixedSkillIdentity,
        core_prompt_version: str,
    ) -> ConversationRun:
        async with self._database.transaction() as session:
            model = await session.get(ConversationRunModel, run_id, with_for_update=True)
            if model is None:
                raise QAContractError("Assistant ConversationRun cannot be promoted")
            current = _run(model)
            if current.run_kind is not ConversationRunKind.ASSISTANT_TURN:
                if current.skill == skill and current.run_kind is run_kind:
                    return current
                raise QAContractError("ConversationRun already has another execution identity")
            if current.status in _TERMINAL:
                return current
            model.run_kind = run_kind.value
            model.selection_source = selection_source.value
            model.router_version = "assistant-router-decision-v1"
            model.skill_name = skill.name
            model.skill_version = skill.version
            model.skill_content_sha256 = skill.content_sha256
            model.core_prompt_version = core_prompt_version
            model.updated_at = datetime.now(UTC)
            await session.flush()
            return _run(model)

    async def publish_direct_message(
        self,
        *,
        run_id: UUID,
        message: MessageRecord,
        usage: ConversationRunUsage,
        model_identity: str,
        refused: bool = False,
    ) -> ConversationRun:
        async with self._database.transaction() as session:
            model = await self._locked_executable_run(session, run_id)
            current = _run(model)
            if current.status in _TERMINAL:
                return current
            if current.cancellation_requested:
                return self._cancel_locked(model)
            self._validate_assistant_message(current, message)
            existing = await session.get(QAMessageModel, message.message_id)
            if existing is not None:
                if (
                    existing.conversation_id == message.conversation_id
                    and existing.space_id == message.space_id
                    and existing.role == MessageRole.ASSISTANT.value
                    and existing.content == message.content
                    and existing.run_id == message.run_id
                ):
                    return current
                raise QAContractError("Assistant message identity already exists")
            session.add(
                QAMessageModel(
                    id=message.message_id,
                    conversation_id=message.conversation_id,
                    run_id=message.run_id,
                    space_id=message.space_id,
                    role=message.role.value,
                    content=message.content,
                    idempotency_key=message.idempotency_key,
                    created_at=message.created_at,
                )
            )
            model.status = (
                ConversationRunStatus.REFUSED.value
                if refused
                else ConversationRunStatus.COMPLETED.value
            )
            model.error_code = None
            model.model_identity = model_identity
            model.usage = _usage_value(usage)
            model.result = _result_value(
                AssistantResult(AssistantResultKind.DIRECT_MESSAGE, message_id=message.message_id)
            )
            model.updated_at = message.created_at
            _clear_lease(model)
            conversation = await session.get(
                ConversationModel, model.conversation_id, with_for_update=True
            )
            if conversation is not None and message.created_at > conversation.updated_at:
                conversation.updated_at = message.created_at
            await session.flush()
            return _run(model)

    async def publish_existing_skill_result(
        self,
        *,
        run_id: UUID,
        message_id: UUID,
        usage: ConversationRunUsage,
        model_identity: str,
        refused: bool = False,
    ) -> ConversationRun:
        async with self._database.transaction() as session:
            model = await self._locked_executable_run(session, run_id)
            current = _run(model)
            if current.status in _TERMINAL:
                return current
            if current.cancellation_requested:
                return self._cancel_locked(model)
            message = await session.get(QAMessageModel, message_id)
            if (
                message is None
                or message.role != MessageRole.ASSISTANT.value
                or message.run_id != run_id
                or message.conversation_id != model.conversation_id
                or message.space_id != model.space_id
            ):
                raise QAContractError(
                    "Existing Skill message does not belong to the ConversationRun"
                )
            model.status = (
                ConversationRunStatus.REFUSED.value
                if refused
                else ConversationRunStatus.COMPLETED.value
            )
            model.error_code = None
            model.model_identity = model_identity
            model.usage = _usage_value(usage)
            model.result = _result_value(
                AssistantResult(AssistantResultKind.SKILL_RESULT, message_id=message_id)
            )
            model.updated_at = datetime.now(UTC)
            _clear_lease(model)
            await session.flush()
            return _run(model)

    async def publish_clarification(
        self,
        *,
        run_id: UUID,
        clarification: Clarification,
        usage: ConversationRunUsage,
        model_identity: str,
    ) -> ConversationRun:
        async with self._database.transaction() as session:
            model = await self._locked_assistant_run(session, run_id)
            current = _run(model)
            if (
                current.status in _TERMINAL
                or current.status is ConversationRunStatus.WAITING_CLARIFICATION
            ):
                return current
            if current.cancellation_requested:
                return self._cancel_locked(model)
            now = datetime.now(UTC)
            model.status = ConversationRunStatus.WAITING_CLARIFICATION.value
            model.error_code = None
            model.model_identity = model_identity
            model.usage = _usage_value(usage)
            model.result = _result_value(
                AssistantResult(AssistantResultKind.CLARIFICATION, clarification=clarification)
            )
            model.updated_at = now
            _clear_lease(model)
            await session.flush()
            return _run(model)

    async def wait_for_approval(self, run_id: UUID) -> ConversationRun:
        async with self._database.transaction() as session:
            model = await self._locked_assistant_run(session, run_id)
            current = _run(model)
            if (
                current.status in _TERMINAL
                or current.status is ConversationRunStatus.WAITING_APPROVAL
            ):
                return current
            if current.cancellation_requested:
                return self._cancel_locked(model)
            model.status = ConversationRunStatus.WAITING_APPROVAL.value
            model.error_code = None
            model.updated_at = datetime.now(UTC)
            _clear_lease(model)
            await session.flush()
            return _run(model)

    async def fail_conversation_run(self, run_id: UUID, *, error_code: str) -> ConversationRun:
        if not error_code.strip():
            raise ValueError("ConversationRun failure requires an error code")
        async with self._database.transaction() as session:
            model = await self._locked_executable_run(session, run_id)
            current = _run(model)
            if current.status in _TERMINAL:
                return current
            if current.cancellation_requested:
                return self._cancel_locked(model)
            model.status = ConversationRunStatus.FAILED.value
            model.error_code = error_code
            model.result = None
            model.updated_at = datetime.now(UTC)
            _clear_lease(model)
            await session.flush()
            return _run(model)

    async def cancel_conversation_run(self, run_id: UUID) -> ConversationRun:
        async with self._database.transaction() as session:
            model = await self._locked_executable_run(session, run_id)
            current = _run(model)
            if current.status in _TERMINAL:
                return current
            return self._cancel_locked(model)

    async def complete_context_compaction(
        self,
        run_id: UUID,
        *,
        usage: ConversationRunUsage,
        model_identity: str,
    ) -> ConversationRun:
        async with self._database.transaction() as session:
            model = await session.get(ConversationRunModel, run_id, with_for_update=True)
            if model is None or model.run_kind != ConversationRunKind.CONTEXT_COMPACTION.value:
                raise QAContractError("Context compaction Run does not exist")
            current = _run(model)
            if current.status in _TERMINAL:
                return current
            if current.cancellation_requested:
                return self._cancel_locked(model)
            model.status = ConversationRunStatus.COMPLETED.value
            model.error_code = None
            model.model_identity = model_identity
            model.usage = _usage_value(usage)
            model.result = None
            model.updated_at = datetime.now(UTC)
            _clear_lease(model)
            await session.flush()
            return _run(model)

    @staticmethod
    async def _locked_assistant_run(session: AsyncSession, run_id: UUID) -> ConversationRunModel:
        model = await session.get(ConversationRunModel, run_id, with_for_update=True)
        if model is None or model.run_kind != ConversationRunKind.ASSISTANT_TURN.value:
            raise QAContractError("Assistant ConversationRun does not exist")
        return model

    @staticmethod
    async def _locked_executable_run(session: AsyncSession, run_id: UUID) -> ConversationRunModel:
        model = await session.get(ConversationRunModel, run_id, with_for_update=True)
        if model is None or model.run_kind not in {
            ConversationRunKind.ASSISTANT_TURN.value,
            ConversationRunKind.CONTEXT_COMPACTION.value,
            ConversationRunKind.SKILL.value,
            ConversationRunKind.GROUNDED_QA.value,
        }:
            raise QAContractError("ConversationRun does not exist")
        return model

    @staticmethod
    def _validate_assistant_message(run: ConversationRun, message: MessageRecord) -> None:
        if (
            message.role is not MessageRole.ASSISTANT
            or message.run_id != run.run_id
            or message.conversation_id != run.conversation_id
            or message.space_id != run.space_id
            or message.idempotency_key is not None
        ):
            raise QAContractError("Assistant message does not belong to the ConversationRun")

    @staticmethod
    def _cancel_locked(model: ConversationRunModel) -> ConversationRun:
        model.status = ConversationRunStatus.CANCELLED.value
        model.cancellation_requested = True
        model.error_code = None
        model.result = None
        model.updated_at = datetime.now(UTC)
        _clear_lease(model)
        return _run(model)


def _model(run: ConversationRun) -> ConversationRunModel:
    return ConversationRunModel(
        id=run.run_id,
        conversation_id=run.conversation_id,
        space_id=run.space_id,
        caller_id=run.caller_id,
        user_message_id=run.user_message_id,
        idempotency_key=run.idempotency_key,
        run_kind=run.run_kind.value,
        selection_source=run.selection_source.value,
        status=run.status.value,
        cancellation_requested=run.cancellation_requested,
        error_code=run.error_code,
        router_version=run.router_version,
        core_prompt_version=run.core_prompt_version,
        model_identity=run.model_identity,
        reasoning_profile=run.reasoning_profile.as_dict(),
        skill_name=run.skill.name if run.skill else None,
        skill_version=run.skill.version if run.skill else None,
        skill_content_sha256=run.skill.content_sha256 if run.skill else None,
        usage=_usage_value(run.usage),
        result=_result_value(run.result),
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _run(model: ConversationRunModel) -> ConversationRun:
    skill = (
        FixedSkillIdentity(
            name=model.skill_name,
            version=model.skill_version,
            content_sha256=model.skill_content_sha256,
        )
        if (
            model.skill_name is not None
            and model.skill_version is not None
            and model.skill_content_sha256 is not None
        )
        else None
    )
    return ConversationRun(
        run_id=model.id,
        conversation_id=model.conversation_id,
        space_id=model.space_id,
        caller_id=model.caller_id,
        user_message_id=model.user_message_id,
        idempotency_key=model.idempotency_key,
        run_kind=ConversationRunKind(model.run_kind),
        selection_source=ConversationRunSelectionSource(model.selection_source),
        status=ConversationRunStatus(model.status),
        cancellation_requested=model.cancellation_requested,
        error_code=model.error_code,
        router_version=model.router_version,
        core_prompt_version=model.core_prompt_version,
        model_identity=model.model_identity,
        reasoning_profile=reasoning_profile_from_dict(model.reasoning_profile),
        skill=skill,
        usage=_usage(model.usage),
        result=_result(model.result),
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


def _same_turn(
    stored: ConversationRunModel,
    stored_message: QAMessageModel,
    requested_run: ConversationRun,
    requested_message: MessageRecord,
) -> bool:
    return (
        stored.conversation_id,
        stored.space_id,
        stored.caller_id,
        stored.idempotency_key,
        _idempotent_run_kind(stored.run_kind),
        stored.selection_source,
        stored_message.content,
    ) == (
        requested_run.conversation_id,
        requested_run.space_id,
        requested_run.caller_id,
        requested_run.idempotency_key,
        _idempotent_run_kind(requested_run.run_kind.value),
        requested_run.selection_source.value,
        requested_message.content,
    )


def _idempotent_run_kind(value: str) -> str:
    if value in {
        ConversationRunKind.ASSISTANT_TURN.value,
        ConversationRunKind.GROUNDED_QA.value,
        ConversationRunKind.SKILL.value,
    }:
        return "assistant-or-promoted"
    return value


def _usage_value(value: ConversationRunUsage) -> dict[str, Any]:
    return {
        "input_tokens": value.input_tokens,
        "output_tokens": value.output_tokens,
        "model_latency_ms": value.model_latency_ms,
    }


def _usage(value: dict[str, Any]) -> ConversationRunUsage:
    return ConversationRunUsage(
        input_tokens=int(value.get("input_tokens", 0)),
        output_tokens=int(value.get("output_tokens", 0)),
        model_latency_ms=float(value.get("model_latency_ms", 0.0)),
    )


def _result_value(value: AssistantResult | None) -> dict[str, Any] | None:
    if value is None:
        return None
    clarification = value.clarification
    return {
        "kind": value.kind.value,
        "message_id": str(value.message_id) if value.message_id is not None else None,
        "clarification": (
            {
                "clarification_id": clarification.clarification_id,
                "kind": clarification.kind.value,
                "message": clarification.message,
                "resource_candidates": [
                    {
                        "candidate_id": candidate.candidate_id,
                        "resource_type": candidate.resource_type,
                        "label": candidate.label,
                        "source_label": candidate.source_label,
                        "version_label": candidate.version_label,
                    }
                    for candidate in clarification.resource_candidates
                ],
                "continuation": (
                    {
                        "skill": {
                            "name": clarification.continuation.skill.name,
                            "version": clarification.continuation.skill.version,
                            "content_sha256": clarification.continuation.skill.content_sha256,
                        },
                        "selection_source": clarification.continuation.selection_source.value,
                        "question": clarification.continuation.question,
                        "resource_type": clarification.continuation.resource_type,
                    }
                    if clarification.continuation is not None
                    else None
                ),
            }
            if clarification is not None
            else None
        ),
    }


def _result(value: dict[str, Any] | None) -> AssistantResult | None:
    if value is None:
        return None
    raw_clarification = value.get("clarification")
    clarification = None
    if raw_clarification is not None:
        candidates = tuple(
            ResourceCandidate(
                candidate_id=item["candidate_id"],
                resource_type=item["resource_type"],
                label=item["label"],
                source_label=item.get("source_label"),
                version_label=item.get("version_label"),
            )
            for item in raw_clarification.get("resource_candidates", [])
        )
        raw_continuation = raw_clarification.get("continuation")
        continuation = None
        if raw_continuation is not None:
            raw_skill = raw_continuation["skill"]
            continuation = ClarificationContinuation(
                skill=FixedSkillIdentity(
                    name=raw_skill["name"],
                    version=raw_skill["version"],
                    content_sha256=raw_skill["content_sha256"],
                ),
                selection_source=ConversationRunSelectionSource(
                    raw_continuation["selection_source"]
                ),
                question=raw_continuation["question"],
                resource_type=raw_continuation["resource_type"],
            )
        clarification = Clarification(
            clarification_id=raw_clarification["clarification_id"],
            kind=ClarificationKind(raw_clarification["kind"]),
            message=raw_clarification["message"],
            resource_candidates=candidates,
            continuation=continuation,
        )
    raw_message_id = value.get("message_id")
    return AssistantResult(
        kind=AssistantResultKind(value["kind"]),
        message_id=UUID(raw_message_id) if raw_message_id is not None else None,
        clarification=clarification,
    )


__all__ = ["PostgresConversationRunRepository"]
