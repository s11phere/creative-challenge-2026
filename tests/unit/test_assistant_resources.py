from __future__ import annotations

from uuid import UUID

import pytest
from application.assistant.resources import NaturalLanguageResourceResolver
from domain.models import Document, DocumentStatus, DocumentVersion, Source


class Sources:
    async def get_by_space(self, _space_id: UUID) -> list[Source]:
        return [Source(id=UUID(int=1), space_id=UUID(int=9), uri="synthetic/source")]


class Documents:
    async def get_by_source(self, _source_id: UUID) -> list[Document]:
        return [
            Document(
                id=UUID(int=2),
                source_id=UUID(int=1),
                stable_key="paper-00000000-0000-4000-8000-000000000003",
                current_version_id=UUID(int=3),
            )
        ]


class Versions:
    async def get(self, _version_id: UUID) -> DocumentVersion:
        return DocumentVersion(
            id=UUID(int=3),
            document_id=UUID(int=2),
            blob_hash="b" * 64,
            content_hash="a" * 64,
            status=DocumentStatus.PUBLISHED,
        )


@pytest.mark.asyncio
async def test_document_description_redacts_internal_identifiers_from_safe_labels() -> None:
    resolver = NaturalLanguageResourceResolver(Sources(), Documents(), Versions())

    described = await resolver.describe_documents(
        space_id=UUID(int=9), document_ids=(UUID(int=2),), limit=8
    )

    assert described[0].candidate.label == "Untitled resource"
    assert "00000000" not in described[0].candidate.label
