"""PostgreSQL adapter for the Assistant resource-resolution Application Port."""

from __future__ import annotations

from uuid import UUID

from application.assistant import NaturalLanguageResourceResolver, ResolvedResource

from .database import Database
from .repositories import DocumentRepository, DocumentVersionRepository, SourceRepository


class PostgresAssistantResourceResolver(NaturalLanguageResourceResolver):
    def __init__(self, database: Database) -> None:
        self._database = database

    async def resolve(
        self, *, space_id: UUID, resource_type: str, reference: str
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
            )


__all__ = ["PostgresAssistantResourceResolver"]
