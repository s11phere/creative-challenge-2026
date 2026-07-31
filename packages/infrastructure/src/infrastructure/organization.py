"""Database-backed immutable scope validation for organization Skills."""

from __future__ import annotations

from uuid import UUID

from application.skills import KnowledgeOrganizationScopeService
from domain.qa_persistence import QARetrievalScope

from .database import Database
from .repositories import DocumentRepository, DocumentVersionRepository, SourceRepository


class PostgresKnowledgeOrganizationScope:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def document_scope(
        self, *, space_id: UUID, document_id: UUID, version_id: UUID
    ) -> QARetrievalScope:
        async with self._database.session() as session:
            return await KnowledgeOrganizationScopeService(
                sources=SourceRepository(session),
                documents=DocumentRepository(session),
                versions=DocumentVersionRepository(session),
            ).document_scope(
                space_id=space_id,
                document_id=document_id,
                version_id=version_id,
            )

    async def sources_scope(
        self, *, space_id: UUID, source_ids: frozenset[UUID]
    ) -> QARetrievalScope:
        async with self._database.session() as session:
            return await KnowledgeOrganizationScopeService(
                sources=SourceRepository(session),
                documents=DocumentRepository(session),
                versions=DocumentVersionRepository(session),
            ).sources_scope(space_id=space_id, source_ids=source_ids)


__all__ = ["PostgresKnowledgeOrganizationScope"]
