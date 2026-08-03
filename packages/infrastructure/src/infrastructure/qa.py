"""PostgreSQL adapters needed by the provisional in-process Grounded QA runtime."""

from __future__ import annotations

from pathlib import Path

from application.retrieval import (
    GatewayQueryTextEmbedder,
    GatewayReranker,
    QueryEmbeddingService,
    SearchService,
    query_embedding_config,
)
from domain.fingerprinting import compute_storage_key
from domain.grounded_qa import (
    CitationContentKind,
    CitationTargetQuery,
    CitationTargetSnapshot,
)
from domain.parsing import ParseMetadata
from domain.retrieval import RetrievalProfileV1, SearchRequest, SearchResult
from model_gateway import ModelGateway
from sqlalchemy import select

from .config import settings
from .database import Database
from .orm import ChunkModel, DocumentModel, DocumentVersionModel, SourceModel
from .repositories import DocumentRepository, SourceRepository, SpaceRepository
from .retrieval import PostgresRetrievalStore, locators_from_meta


class DatabaseSearchService:
    """Open one bounded database transaction for every SearchService call."""

    def __init__(self, database: Database, gateway: ModelGateway) -> None:
        self._database = database
        self._gateway = gateway
        self._identity = settings.active_embedding_identity()

    async def search(self, request: SearchRequest, profile: RetrievalProfileV1) -> SearchResult:
        async with self._database.session() as session:
            service = SearchService(
                space_repo=SpaceRepository(session),
                source_repo=SourceRepository(session),
                document_repo=DocumentRepository(session),
                retrieval_store=PostgresRetrievalStore(session),
                query_embedder=QueryEmbeddingService(
                    GatewayQueryTextEmbedder(self._gateway),
                    config=query_embedding_config(
                        self._identity,
                        timeout_seconds=settings.retrieval_timeout_seconds,
                    ),
                ),
                reranker=GatewayReranker(self._gateway),
            )
            return await service.search(request, profile)


class PostgresCitationTargetPort:
    """Revalidate a citation identity against the currently published database state."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def get_target(self, query: CitationTargetQuery) -> CitationTargetSnapshot | None:
        async with self._database.session() as session:
            result = await session.execute(
                select(ChunkModel, DocumentVersionModel, DocumentModel, SourceModel)
                .join(DocumentVersionModel, ChunkModel.version_id == DocumentVersionModel.id)
                .join(DocumentModel, DocumentVersionModel.document_id == DocumentModel.id)
                .join(SourceModel, DocumentModel.source_id == SourceModel.id)
                .where(
                    ChunkModel.id == query.chunk_id,
                    DocumentVersionModel.id == query.version_id,
                    DocumentModel.id == query.document_id,
                    SourceModel.id == query.source_id,
                    SourceModel.space_id == query.space_id,
                )
            )
            row = result.one_or_none()
            if row is None:
                return None
            chunk, version, document, source = row
            locators = locators_from_meta(chunk.meta)
            if not locators:
                return None
            file_name = Path(version.file_path or document.stable_key or "document.txt").name
            suffix = Path(file_name).suffix.casefold()
            content_kind = CitationContentKind.PDF if suffix == ".pdf" else CitationContentKind.TEXT
            mime_type = {
                ".md": "text/markdown",
                ".markdown": "text/markdown",
                ".pdf": "application/pdf",
            }.get(suffix, "text/plain")
            return CitationTargetSnapshot(
                query=query,
                current_version_id=document.current_version_id,
                locators=locators,
                blob_hash=version.blob_hash,
                storage_key=compute_storage_key(source.id, version.blob_hash),
                content_kind=content_kind,
                metadata=ParseMetadata(
                    file_name=file_name,
                    file_size=0,
                    mime_type=mime_type,
                    encoding="utf-8",
                ),
                chunk_text=chunk.text,
                document_deleted=document.deleted_at is not None,
                chunk_available=version.status == "published",
            )


__all__ = ["DatabaseSearchService", "PostgresCitationTargetPort"]
