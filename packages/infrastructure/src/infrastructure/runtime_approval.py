"""Durable approval and derived-knowledge adapters for Runtime write Tools."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

from domain.agent_runtime import AgentRunContext, ApprovalPort, ToolCallRecord
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from .database import Database
from .orm import (
    ConversationRunModel,
    DerivedKnowledgeItemModel,
    QACitationModel,
    RuntimeApprovalModel,
)


@dataclass(frozen=True)
class DerivedKnowledgeRecord:
    id: UUID
    run_id: UUID
    space_id: UUID
    kind: str
    idempotency_key: str
    content: Mapping[str, Any]
    citation_ids: tuple[str, ...]
    created_by: str
    created_at: datetime
    status: str = "active"
    revoked_at: datetime | None = None
    revoked_by: str | None = None


@dataclass(frozen=True)
class ApprovalRecord:
    approval_id: UUID
    run_id: UUID
    space_id: UUID
    caller_id: str
    action: str
    tool_name: str
    tool_version: str
    idempotency_key: str
    status: str
    requested_at: datetime
    decided_at: datetime | None
    decided_by: str | None
    expires_at: datetime | None
    revoked_at: datetime | None
    revoked_by: str | None


class PostgresApprovalPort(ApprovalPort):
    """Persist approval requests and expose only approved, unexpired grants."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def request(self, context: AgentRunContext, tool: ToolCallRecord) -> str:
        approval_id = uuid4()
        async with self._database.transaction() as session:
            existing = await session.scalar(
                select(RuntimeApprovalModel).where(
                    RuntimeApprovalModel.run_id == context.run_id,
                    RuntimeApprovalModel.action == tool.tool_name,
                    RuntimeApprovalModel.idempotency_key == tool.idempotency_key,
                )
            )
            if existing is not None:
                if (
                    existing.space_id != context.space_id
                    or existing.caller_id != context.caller_id
                    or existing.tool_version != tool.tool_version
                ):
                    raise ValueError("approval identity conflicts with the existing request")
                return str(existing.id)
            session.add(
                RuntimeApprovalModel(
                    id=approval_id,
                    run_id=context.run_id,
                    space_id=context.space_id,
                    caller_id=context.caller_id,
                    action=tool.tool_name,
                    tool_name=tool.tool_name,
                    tool_version=tool.tool_version,
                    idempotency_key=tool.idempotency_key,
                    status="pending",
                )
            )
        return str(approval_id)

    async def is_approved(self, approval_id: str, context: AgentRunContext) -> bool:
        return await self.is_approved_for_tool(approval_id, context)

    async def is_approved_for_tool(
        self,
        approval_id: str,
        context: AgentRunContext,
        *,
        tool_name: str | None = None,
        tool_version: str | None = None,
    ) -> bool:
        try:
            approval_uuid = UUID(approval_id)
        except ValueError:
            return False
        async with self._database.session() as session:
            approval = await session.get(RuntimeApprovalModel, approval_uuid)
        if approval is None:
            return False
        now = datetime.now(UTC)
        return (
            approval.run_id == context.run_id
            and approval.space_id == context.space_id
            and approval.caller_id == context.caller_id
            and approval.status == "approved"
            and (tool_name is None or approval.tool_name == tool_name)
            and (tool_version is None or approval.tool_version == tool_version)
            and (approval.expires_at is None or approval.expires_at > now)
        )

    async def decide(
        self,
        approval_id: str,
        *,
        approved: bool,
        decided_by: str,
        expires_at: datetime | None = None,
    ) -> bool:
        try:
            approval_uuid = UUID(approval_id)
        except ValueError:
            return False
        async with self._database.transaction() as session:
            approval = await session.get(RuntimeApprovalModel, approval_uuid, with_for_update=True)
            if approval is None:
                return False
            target_status = "approved" if approved else "rejected"
            if approval.status == target_status:
                return True
            if approval.status != "pending":
                return False
            approval.status = target_status
            approval.decided_at = datetime.now(UTC)
            approval.decided_by = decided_by
            approval.expires_at = expires_at if approved else None
        return True

    async def revoke(self, approval_id: str, *, revoked_by: str) -> bool:
        try:
            approval_uuid = UUID(approval_id)
        except ValueError:
            return False
        async with self._database.transaction() as session:
            approval = await session.get(RuntimeApprovalModel, approval_uuid, with_for_update=True)
            if approval is None or approval.status in {"rejected", "revoked"}:
                return False
            approval.status = "revoked"
            approval.revoked_at = datetime.now(UTC)
            approval.revoked_by = revoked_by
        return True

    async def status(self, approval_id: str) -> str | None:
        try:
            approval_uuid = UUID(approval_id)
        except ValueError:
            return None
        async with self._database.session() as session:
            approval = await session.get(RuntimeApprovalModel, approval_uuid)
            return approval.status if approval is not None else None

    async def get(self, approval_id: str, *, run_id: UUID | None = None) -> ApprovalRecord | None:
        try:
            approval_uuid = UUID(approval_id)
        except ValueError:
            return None
        async with self._database.session() as session:
            approval = await session.get(RuntimeApprovalModel, approval_uuid)
            if approval is None or (run_id is not None and approval.run_id != run_id):
                return None
            return _approval_record(approval)

    async def list_for_run(self, run_id: UUID) -> tuple[ApprovalRecord, ...]:
        async with self._database.session() as session:
            models = (
                await session.execute(
                    select(RuntimeApprovalModel)
                    .where(RuntimeApprovalModel.run_id == run_id)
                    .order_by(RuntimeApprovalModel.requested_at, RuntimeApprovalModel.id)
                )
            ).scalars()
            return tuple(_approval_record(model) for model in models)


class PostgresDerivedKnowledgeStore:
    """Write citation-backed derived knowledge exactly once per Runtime action."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def write_review_cards(
        self,
        *,
        run_id: UUID,
        space_id: UUID,
        created_by: str,
        idempotency_key: str,
        content: Mapping[str, Any],
        citation_ids: tuple[str, ...],
    ) -> DerivedKnowledgeRecord:
        if not idempotency_key or not citation_ids:
            raise ValueError("derived knowledge requires an idempotency key and citations")
        if len(set(citation_ids)) != len(citation_ids):
            raise ValueError("derived knowledge citations must be unique")
        try:
            citation_uuids = tuple(UUID(value) for value in citation_ids)
        except ValueError as exc:
            raise ValueError("derived knowledge citations must be UUIDs") from exc
        async with self._database.transaction() as session:
            run_space_id = await session.scalar(
                select(ConversationRunModel.space_id).where(ConversationRunModel.id == run_id)
            )
            if run_space_id != space_id:
                raise ValueError("derived knowledge Space does not match the ConversationRun")
            citation_rows = (
                await session.execute(
                    select(QACitationModel.evidence_id).where(
                        QACitationModel.run_id == run_id,
                        QACitationModel.evidence_id.in_(citation_uuids),
                    )
                )
            ).scalars()
            if set(str(value) for value in citation_rows) != set(citation_ids):
                raise ValueError("derived knowledge citations must belong to the QA Run")
            statement = (
                insert(DerivedKnowledgeItemModel)
                .values(
                    id=uuid4(),
                    run_id=run_id,
                    space_id=space_id,
                    kind="review_cards",
                    idempotency_key=idempotency_key,
                    content=cast(dict[str, Any], dict(content)),
                    citation_ids=list(citation_ids),
                    created_by=created_by,
                )
                .on_conflict_do_nothing(constraint="uq_derived_knowledge_idempotency")
            )
            await session.execute(statement)
            stored = await session.scalar(
                select(DerivedKnowledgeItemModel).where(
                    DerivedKnowledgeItemModel.run_id == run_id,
                    DerivedKnowledgeItemModel.idempotency_key == idempotency_key,
                )
            )
            if stored is None:
                raise RuntimeError("derived knowledge write was not persisted")
            if stored.space_id != space_id or stored.created_by != created_by:
                raise ValueError("derived knowledge identity conflicts with the existing item")
            return _record(stored)

    async def get(
        self, item_id: UUID, *, run_id: UUID | None = None
    ) -> DerivedKnowledgeRecord | None:
        async with self._database.session() as session:
            item = await session.get(DerivedKnowledgeItemModel, item_id)
            if item is None or (run_id is not None and item.run_id != run_id):
                return None
            return _record(item)

    async def list_for_run(self, run_id: UUID) -> tuple[DerivedKnowledgeRecord, ...]:
        async with self._database.session() as session:
            models = (
                await session.execute(
                    select(DerivedKnowledgeItemModel)
                    .where(DerivedKnowledgeItemModel.run_id == run_id)
                    .order_by(DerivedKnowledgeItemModel.created_at, DerivedKnowledgeItemModel.id)
                )
            ).scalars()
            return tuple(_record(model) for model in models)

    async def revoke(
        self, item_id: UUID, *, run_id: UUID, space_id: UUID, revoked_by: str
    ) -> DerivedKnowledgeRecord | None:
        async with self._database.transaction() as session:
            item = await session.get(DerivedKnowledgeItemModel, item_id, with_for_update=True)
            if item is None or item.run_id != run_id or item.space_id != space_id:
                return None
            if item.status == "active":
                item.status = "revoked"
                item.revoked_at = datetime.now(UTC)
                item.revoked_by = revoked_by
                await session.flush()
            return _record(item)


def _record(value: DerivedKnowledgeItemModel) -> DerivedKnowledgeRecord:
    return DerivedKnowledgeRecord(
        id=value.id,
        run_id=value.run_id,
        space_id=value.space_id,
        kind=value.kind,
        idempotency_key=value.idempotency_key,
        content=cast(Mapping[str, Any], value.content),
        citation_ids=tuple(value.citation_ids),
        created_by=value.created_by,
        created_at=value.created_at,
        status=value.status,
        revoked_at=value.revoked_at,
        revoked_by=value.revoked_by,
    )


def _approval_record(value: RuntimeApprovalModel) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=value.id,
        run_id=value.run_id,
        space_id=value.space_id,
        caller_id=value.caller_id,
        action=value.action,
        tool_name=value.tool_name,
        tool_version=value.tool_version,
        idempotency_key=value.idempotency_key,
        status=value.status,
        requested_at=value.requested_at,
        decided_at=value.decided_at,
        decided_by=value.decided_by,
        expires_at=value.expires_at,
        revoked_at=value.revoked_at,
        revoked_by=value.revoked_by,
    )


__all__ = [
    "ApprovalRecord",
    "DerivedKnowledgeRecord",
    "PostgresApprovalPort",
    "PostgresDerivedKnowledgeStore",
]
