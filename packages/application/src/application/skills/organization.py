"""Validate immutable retrieval scopes for knowledge-organization Skills."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from domain.models import Document, DocumentStatus, DocumentVersion, Source
from domain.qa_persistence import QARetrievalScope


class SourceReader(Protocol):
    async def get(self, source_id: UUID) -> Source | None: ...


class DocumentReader(Protocol):
    async def get(self, document_id: UUID) -> Document | None: ...

    async def get_by_source(self, source_id: UUID) -> list[Document]: ...


class VersionReader(Protocol):
    async def get(self, version_id: UUID) -> DocumentVersion | None: ...


class OrganizationScopeErrorCode(StrEnum):
    SOURCE_INVALID = "SKILL_SOURCE_INVALID"
    DOCUMENT_INVALID = "SKILL_DOCUMENT_INVALID"
    VERSION_INVALID = "SKILL_VERSION_INVALID"
    EVIDENCE_INSUFFICIENT = "SKILL_EVIDENCE_INSUFFICIENT"


class OrganizationScopeError(ValueError):
    def __init__(self, code: OrganizationScopeErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class KnowledgeOrganizationScopeService:
    sources: SourceReader
    documents: DocumentReader
    versions: VersionReader

    async def document_scope(
        self, *, space_id: UUID, document_id: UUID, version_id: UUID
    ) -> QARetrievalScope:
        document = await self.documents.get(document_id)
        if document is None or document.deleted_at is not None:
            raise OrganizationScopeError(
                OrganizationScopeErrorCode.DOCUMENT_INVALID,
                "Selected document is unavailable.",
            )
        source = await self.sources.get(document.source_id)
        if source is None or source.space_id != space_id:
            raise OrganizationScopeError(
                OrganizationScopeErrorCode.DOCUMENT_INVALID,
                "Selected document does not belong to the conversation Space.",
            )
        version = await self.versions.get(version_id)
        if (
            version is None
            or version.document_id != document_id
            or document.current_version_id != version_id
            or version.status is not DocumentStatus.PUBLISHED
        ):
            raise OrganizationScopeError(
                OrganizationScopeErrorCode.VERSION_INVALID,
                "Selected version is not the current published document version.",
            )
        return QARetrievalScope(
            source_ids=frozenset({source.id}),
            document_ids=frozenset({document.id}),
            version_ids=frozenset({version.id}),
        )

    async def sources_scope(
        self, *, space_id: UUID, source_ids: frozenset[UUID]
    ) -> QARetrievalScope:
        if len(source_ids) < 2:
            raise OrganizationScopeError(
                OrganizationScopeErrorCode.SOURCE_INVALID,
                "Source comparison requires at least two sources.",
            )
        document_ids: set[UUID] = set()
        version_ids: set[UUID] = set()
        for source_id in sorted(source_ids, key=str):
            source = await self.sources.get(source_id)
            if source is None or source.space_id != space_id:
                raise OrganizationScopeError(
                    OrganizationScopeErrorCode.SOURCE_INVALID,
                    "Selected source does not belong to the conversation Space.",
                )
            source_document_count = 0
            for document in await self.documents.get_by_source(source_id):
                if document.deleted_at is not None or document.current_version_id is None:
                    continue
                version = await self.versions.get(document.current_version_id)
                if version is None or version.status is not DocumentStatus.PUBLISHED:
                    continue
                document_ids.add(document.id)
                version_ids.add(version.id)
                source_document_count += 1
            if source_document_count == 0:
                raise OrganizationScopeError(
                    OrganizationScopeErrorCode.EVIDENCE_INSUFFICIENT,
                    "Each selected source must contain a current published document.",
                )
        return QARetrievalScope(
            source_ids=source_ids,
            document_ids=frozenset(document_ids),
            version_ids=frozenset(version_ids),
        )


__all__ = [
    "KnowledgeOrganizationScopeService",
    "OrganizationScopeError",
    "OrganizationScopeErrorCode",
]
