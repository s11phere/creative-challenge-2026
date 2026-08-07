"""Read-only natural-language resource resolution within one conversation Space."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Protocol
from uuid import UUID

from domain.conversation_run import ResourceCandidate
from domain.models import Document, DocumentStatus, DocumentVersion, Source
from domain.qa_persistence import QARetrievalScope

from .context import ConversationContextSnapshot


class ResourceResolutionErrorCode(StrEnum):
    NOT_FOUND = "RESOURCE_NOT_FOUND"
    CONFLICT = "RESOURCE_CONFLICT"


class ResourceResolutionError(ValueError):
    def __init__(
        self,
        code: ResourceResolutionErrorCode,
        message: str,
        candidates: tuple[ResourceCandidate, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.candidates = candidates


class SpaceSourceReader(Protocol):
    async def get_by_space(self, space_id: UUID) -> list[Source]: ...


class SpaceDocumentReader(Protocol):
    async def get_by_source(self, source_id: UUID) -> list[Document]: ...


class SpaceVersionReader(Protocol):
    async def get(self, version_id: UUID) -> DocumentVersion | None: ...


@dataclass(frozen=True)
class ResolvedResource:
    candidate: ResourceCandidate
    scope: QARetrievalScope


class ResourceResolutionPort(Protocol):
    async def resolve(
        self,
        *,
        space_id: UUID,
        resource_type: str,
        reference: str,
        context: ConversationContextSnapshot | None = None,
    ) -> ResolvedResource: ...

    async def select_candidate(
        self, *, space_id: UUID, resource_type: str, candidate_id: str
    ) -> ResolvedResource: ...


_INTERNAL_ID = re.compile(r"(?:[0-9a-f]{32,64}|[0-9a-f]{8}-[0-9a-f-]{27,})", re.IGNORECASE)


@dataclass
class NaturalLanguageResourceResolver(ResourceResolutionPort):
    sources: SpaceSourceReader
    documents: SpaceDocumentReader
    versions: SpaceVersionReader

    async def resolve(
        self,
        *,
        space_id: UUID,
        resource_type: str,
        reference: str,
        context: ConversationContextSnapshot | None = None,
    ) -> ResolvedResource:
        _ = context
        if resource_type not in {"source", "document"} or not reference.strip():
            raise ResourceResolutionError(
                ResourceResolutionErrorCode.NOT_FOUND,
                "No matching resource is available in the current Space.",
            )
        if _INTERNAL_ID.search(reference):
            raise ResourceResolutionError(
                ResourceResolutionErrorCode.CONFLICT,
                "Resource references must be natural language.",
            )
        needle = _normalize(reference)
        matches: list[ResolvedResource] = []
        for source in await self.sources.get_by_space(space_id):
            documents = await self.documents.get_by_source(source.id)
            current: list[tuple[Document, DocumentVersion]] = []
            for document in documents:
                if document.deleted_at is not None or document.current_version_id is None:
                    continue
                version = await self.versions.get(document.current_version_id)
                if version is not None and version.status is DocumentStatus.PUBLISHED:
                    current.append((document, version))
            if resource_type == "source" and _matches(needle, _source_label(source)):
                if current:
                    matches.append(self._source_result(source, current))
            elif resource_type == "document":
                for document, version in current:
                    if _matches(needle, _document_label(document)):
                        assert version is not None
                        matches.append(self._document_result(source, document, version))
        if not matches:
            raise ResourceResolutionError(
                ResourceResolutionErrorCode.NOT_FOUND,
                "No matching resource is available in the current Space.",
            )
        if len(matches) > 1:
            raise ResourceResolutionError(
                ResourceResolutionErrorCode.CONFLICT,
                "More than one current resource matches the reference.",
                tuple(match.candidate for match in matches[:20]),
            )
        return matches[0]

    async def select_candidate(
        self, *, space_id: UUID, resource_type: str, candidate_id: str
    ) -> ResolvedResource:
        if resource_type not in {"source", "document"} or not candidate_id.startswith("candidate:"):
            raise ResourceResolutionError(
                ResourceResolutionErrorCode.NOT_FOUND,
                "The selected resource is no longer available in the current Space.",
            )
        for match in await self._current_resources(space_id=space_id, resource_type=resource_type):
            if match.candidate.candidate_id == candidate_id:
                return match
        raise ResourceResolutionError(
            ResourceResolutionErrorCode.NOT_FOUND,
            "The selected resource is no longer available in the current Space.",
        )

    async def _current_resources(
        self, *, space_id: UUID, resource_type: str
    ) -> tuple[ResolvedResource, ...]:
        matches: list[ResolvedResource] = []
        for source in await self.sources.get_by_space(space_id):
            current: list[tuple[Document, DocumentVersion]] = []
            for document in await self.documents.get_by_source(source.id):
                if document.deleted_at is not None or document.current_version_id is None:
                    continue
                version = await self.versions.get(document.current_version_id)
                if version is not None and version.status is DocumentStatus.PUBLISHED:
                    current.append((document, version))
            if resource_type == "source" and current:
                matches.append(self._source_result(source, current))
            elif resource_type == "document":
                matches.extend(
                    self._document_result(source, document, version)
                    for document, version in current
                )
        return tuple(matches)

    def _source_result(
        self, source: Source, current: list[tuple[Document, DocumentVersion]]
    ) -> ResolvedResource:
        documents = frozenset(document.id for document, _version in current)
        versions = frozenset(version.id for _document, version in current)
        return ResolvedResource(
            candidate=_candidate("source", source.id, _source_label(source)),
            scope=QARetrievalScope(
                source_ids=frozenset({source.id}), document_ids=documents, version_ids=versions
            ),
        )

    def _document_result(
        self, source: Source, document: Document, version: DocumentVersion
    ) -> ResolvedResource:
        return ResolvedResource(
            candidate=_candidate(
                "document",
                document.id,
                _document_label(document),
                _source_label(source),
                "published",
            ),
            scope=QARetrievalScope(
                source_ids=frozenset({source.id}),
                document_ids=frozenset({document.id}),
                version_ids=frozenset({version.id}),
            ),
        )


def _candidate(
    resource_type: str,
    identity: UUID,
    label: str,
    source_label: str | None = None,
    version_label: str | None = None,
) -> ResourceCandidate:
    digest = hashlib.sha256(f"{resource_type}:{identity}".encode()).hexdigest()[:24]
    return ResourceCandidate(
        candidate_id=f"candidate:{digest}",
        resource_type=resource_type,
        label=_safe_label(label),
        source_label=_safe_label(source_label) if source_label else None,
        version_label=version_label,
    )


def _source_label(source: Source) -> str:
    value = PurePosixPath(source.uri.replace("\\", "/")).name or "Untitled source"
    return value[:280]


def _document_label(document: Document) -> str:
    return (document.stable_key or "Untitled document")[:280]


def _safe_label(value: str) -> str:
    return " ".join(value.split())[:280]


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def _matches(needle: str, label: str) -> bool:
    normalized = _normalize(label)
    return needle == normalized or needle in normalized


__all__ = [
    "NaturalLanguageResourceResolver",
    "ResourceResolutionError",
    "ResourceResolutionErrorCode",
    "ResourceResolutionPort",
    "ResolvedResource",
]
