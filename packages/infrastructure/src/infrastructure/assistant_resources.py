"""PostgreSQL adapter for the Assistant resource-resolution Application Port."""

from __future__ import annotations

from uuid import UUID

from application.assistant import (
    ConversationContextSnapshot,
    NaturalLanguageResourceResolver,
    ResolvedResource,
)

from .database import Database
from .repositories import DocumentRepository, DocumentVersionRepository, SourceRepository


class PostgresAssistantResourceResolver(NaturalLanguageResourceResolver):
    def __init__(self, database: Database) -> None:
        self._database = database

    async def resolve(
        self,
        *,
        space_id: UUID,
        resource_type: str,
        reference: str,
        context: ConversationContextSnapshot | None = None,
    ) -> ResolvedResource:
        async with self._database.session() as session:
            resolver = NaturalLanguageResourceResolver(
                sources=SourceRepository(session),
                documents=DocumentRepository(session),
                versions=DocumentVersionRepository(session),
            )
            return await resolver.resolve(
                space_id=space_id,
                resource_type=resource_type,
                reference=reference,
                context=context,
            )

    async def select_candidate(
        self, *, space_id: UUID, resource_type: str, candidate_id: str
    ) -> ResolvedResource:
        async with self._database.session() as session:
            resolver = NaturalLanguageResourceResolver(
                sources=SourceRepository(session),
                documents=DocumentRepository(session),
                versions=DocumentVersionRepository(session),
            )
            return await resolver.select_candidate(
                space_id=space_id,
                resource_type=resource_type,
                candidate_id=candidate_id,
            )


__all__ = ["PostgresAssistantResourceResolver"]
