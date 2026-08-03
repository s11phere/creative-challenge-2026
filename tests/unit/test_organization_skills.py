from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import cast
from uuid import UUID

import pytest
from application.skills import (
    KnowledgeOrganizationScopeService,
    OrganizationScopeError,
    OrganizationScopeErrorCode,
)
from domain.models import Document, DocumentStatus, DocumentVersion, Source


@dataclass
class FakeSources:
    values: dict[UUID, Source]

    async def get(self, source_id: UUID) -> Source | None:
        return self.values.get(source_id)


@dataclass
class FakeDocuments:
    values: dict[UUID, Document]
    by_source: dict[UUID, list[Document]] = field(default_factory=dict)

    async def get(self, document_id: UUID) -> Document | None:
        return self.values.get(document_id)

    async def get_by_source(self, source_id: UUID) -> list[Document]:
        return self.by_source.get(source_id, [])


@dataclass
class FakeVersions:
    values: dict[UUID, DocumentVersion]

    async def get(self, version_id: UUID) -> DocumentVersion | None:
        return self.values.get(version_id)


def service() -> tuple[KnowledgeOrganizationScopeService, dict[str, UUID]]:
    ids = {
        name: UUID(int=index)
        for index, name in enumerate(
            ("space", "source1", "source2", "document1", "document2", "version1", "version2"),
            1,
        )
    }
    source1 = Source(id=ids["source1"], space_id=ids["space"])
    source2 = Source(id=ids["source2"], space_id=ids["space"])
    document1 = Document(
        id=ids["document1"], source_id=source1.id, current_version_id=ids["version1"]
    )
    document2 = Document(
        id=ids["document2"], source_id=source2.id, current_version_id=ids["version2"]
    )
    version1 = DocumentVersion(
        id=ids["version1"], document_id=document1.id, status=DocumentStatus.PUBLISHED
    )
    version2 = DocumentVersion(
        id=ids["version2"], document_id=document2.id, status=DocumentStatus.PUBLISHED
    )
    return (
        KnowledgeOrganizationScopeService(
            sources=FakeSources({source1.id: source1, source2.id: source2}),
            documents=FakeDocuments(
                {document1.id: document1, document2.id: document2},
                {source1.id: [document1], source2.id: [document2]},
            ),
            versions=FakeVersions({version1.id: version1, version2.id: version2}),
        ),
        ids,
    )


@pytest.mark.asyncio
async def test_document_and_source_scopes_fix_current_published_versions() -> None:
    scope_service, ids = service()
    document_scope = await scope_service.document_scope(
        space_id=ids["space"],
        document_id=ids["document1"],
        version_id=ids["version1"],
    )
    compare_scope = await scope_service.sources_scope(
        space_id=ids["space"],
        source_ids=frozenset({ids["source1"], ids["source2"]}),
    )

    assert document_scope.version_ids == frozenset({ids["version1"]})
    assert compare_scope.document_ids == frozenset({ids["document1"], ids["document2"]})
    assert compare_scope.version_ids == frozenset({ids["version1"], ids["version2"]})


@pytest.mark.asyncio
async def test_document_scope_rejects_cross_space_or_non_current_version() -> None:
    scope_service, ids = service()
    with pytest.raises(OrganizationScopeError) as cross_space:
        await scope_service.document_scope(
            space_id=UUID(int=99),
            document_id=ids["document1"],
            version_id=ids["version1"],
        )
    assert cross_space.value.code is OrganizationScopeErrorCode.DOCUMENT_INVALID

    with pytest.raises(OrganizationScopeError) as old_version:
        await scope_service.document_scope(
            space_id=ids["space"],
            document_id=ids["document1"],
            version_id=UUID(int=98),
        )
    assert old_version.value.code is OrganizationScopeErrorCode.VERSION_INVALID


@pytest.mark.asyncio
async def test_comparison_scope_rejects_undersized_cross_space_or_unpublished_sources() -> None:
    scope_service, ids = service()

    with pytest.raises(OrganizationScopeError) as undersized:
        await scope_service.sources_scope(
            space_id=ids["space"], source_ids=frozenset({ids["source1"]})
        )
    assert undersized.value.code is OrganizationScopeErrorCode.SOURCE_INVALID

    sources = cast(FakeSources, scope_service.sources)
    sources.values[ids["source2"]] = replace(sources.values[ids["source2"]], space_id=UUID(int=99))
    with pytest.raises(OrganizationScopeError) as cross_space:
        await scope_service.sources_scope(
            space_id=ids["space"],
            source_ids=frozenset({ids["source1"], ids["source2"]}),
        )
    assert cross_space.value.code is OrganizationScopeErrorCode.SOURCE_INVALID

    sources.values[ids["source2"]] = replace(sources.values[ids["source2"]], space_id=ids["space"])
    documents = cast(FakeDocuments, scope_service.documents)
    documents.by_source[ids["source2"]] = []
    with pytest.raises(OrganizationScopeError) as unpublished:
        await scope_service.sources_scope(
            space_id=ids["space"],
            source_ids=frozenset({ids["source1"], ids["source2"]}),
        )
    assert unpublished.value.code is OrganizationScopeErrorCode.EVIDENCE_INSUFFICIENT
