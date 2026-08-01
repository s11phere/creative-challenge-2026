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
from .orm import DerivedKnowledgeItemModel, RuntimeApprovalModel


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
            if approval is None or approval.status != "pending":
                return False
            approval.status = "approved" if approved else "rejected"
            approval.decided_at = datetime.now(UTC)
            approval.decided_by = decided_by
            approval.expires_at = expires_at if approved else None
        return True


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
        async with self._database.transaction() as session:
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
    )


__all__ = ["DerivedKnowledgeRecord", "PostgresApprovalPort", "PostgresDerivedKnowledgeStore"]
