"""PostgreSQL adapter for the shared ConversationRun parent identity."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from domain.conversation_run import (
    AssistantResult,
    AssistantResultKind,
    Clarification,
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
from sqlalchemy import select

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
            session.add(_model(run))
            if user_message.created_at > conversation.updated_at:
                conversation.updated_at = user_message.created_at
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
        stored.run_kind,
        stored.selection_source,
        stored_message.content,
    ) == (
        requested_run.conversation_id,
        requested_run.space_id,
        requested_run.caller_id,
        requested_run.idempotency_key,
        requested_run.run_kind.value,
        requested_run.selection_source.value,
        requested_message.content,
    )


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
        clarification = Clarification(
            clarification_id=raw_clarification["clarification_id"],
            kind=ClarificationKind(raw_clarification["kind"]),
            message=raw_clarification["message"],
            resource_candidates=candidates,
        )
    raw_message_id = value.get("message_id")
    return AssistantResult(
        kind=AssistantResultKind(value["kind"]),
        message_id=UUID(raw_message_id) if raw_message_id is not None else None,
        clarification=clarification,
    )


__all__ = ["PostgresConversationRunRepository"]
