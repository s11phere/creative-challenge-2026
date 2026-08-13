"""PostgreSQL adapters for cross-session long-term memory (personalization Phase 5).

Storage (``PostgresMemoryEntryRepository``), the distillation read source
(``PostgresMemoryDistillationSource``), and the injection retriever
(``GatewayMemoryRetriever``) reuse the module-level ``Database`` pattern from the
worker; the retriever is best-effort so a memory failure never breaks a turn.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from application.ingestion.embedding import TextEmbedder
from application.memory.retrieval import MEMORY_TOP_K, select_memories
from domain.conversation_context import (
    ConversationSensitivity,
    ConversationSummary,
)
from domain.memory_entries import MemoryEntry, MemoryEntryRepository, MemoryEntryType
from domain.usage_traces import UsagePatternSnapshot
from model_gateway import CapabilityAlias, EmbeddingRequest, ModelGateway
from sqlalchemy import select

from .database import Database
from .orm import ConversationSummaryModel, MemoryEntryModel
from .usage_traces import PostgresUsagePatternRepository

logger = logging.getLogger(__name__)


class PostgresMemoryEntryRepository:
    """Content-addressed persistence for distilled memory entries."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def save(self, entry: MemoryEntry) -> MemoryEntry:
        async with self._database.transaction() as session:
            session.add(_memory_model(entry))
            await session.flush()
        return entry

    async def get(self, memory_id: UUID) -> MemoryEntry | None:
        async with self._database.session() as session:
            model = await session.get(MemoryEntryModel, memory_id)
            return _memory(model) if model is not None else None

    async def get_by_hash(self, content_sha256: str) -> MemoryEntry | None:
        async with self._database.session() as session:
            model = (
                await session.execute(
                    select(MemoryEntryModel).where(
                        MemoryEntryModel.content_sha256 == content_sha256
                    )
                )
            ).scalar_one_or_none()
            return _memory(model) if model is not None else None

    async def update(self, entry: MemoryEntry) -> MemoryEntry:
        async with self._database.transaction() as session:
            model = await session.get(MemoryEntryModel, entry.memory_id)
            if model is None:
                raise ValueError("memory entry is unavailable")
            model.entry_type = entry.entry_type.value
            model.content = entry.content
            model.content_sha256 = entry.content_sha256
            model.source_conversation_id = entry.source_conversation_id
            model.source_summary_id = entry.source_summary_id
            model.source_kind = entry.source_kind
            model.embedding = list(entry.embedding) if entry.embedding is not None else None
            model.sensitivity = entry.sensitivity.value
            model.expires_at = entry.expires_at
            model.frequency = entry.frequency
            model.first_seen_at = entry.first_seen_at
            model.updated_at = entry.updated_at
            await session.flush()
        return entry

    async def find_similar(
        self,
        embedding: tuple[float, ...],
        *,
        limit: int,
        entry_type: MemoryEntryType | None = None,
    ) -> tuple[MemoryEntry, ...]:
        if limit < 1:
            raise ValueError("memory similarity limit must be positive")
        async with self._database.session() as session:
            statement = (
                select(MemoryEntryModel)
                .where(MemoryEntryModel.embedding.is_not(None))
                .order_by(MemoryEntryModel.embedding.cosine_distance(list(embedding)))
                .limit(limit)
            )
            if entry_type is not None:
                statement = statement.where(MemoryEntryModel.entry_type == entry_type.value)
            models = (await session.execute(statement)).scalars()
            return tuple(_memory(model) for model in models)

    async def list_all(self, *, limit: int | None = None) -> tuple[MemoryEntry, ...]:
        async with self._database.session() as session:
            statement = select(MemoryEntryModel).order_by(
                MemoryEntryModel.updated_at.desc(), MemoryEntryModel.memory_id
            )
            if limit is not None:
                if limit < 1:
                    raise ValueError("memory entry page limit must be positive")
                statement = statement.limit(limit)
            models = (await session.execute(statement)).scalars()
            return tuple(_memory(model) for model in models)


class PostgresMemoryDistillationSource:
    """Cross-conversation read source: most recent summaries plus usage patterns."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._patterns = PostgresUsagePatternRepository(database)

    async def list_all_summaries(
        self, *, limit: int | None = None
    ) -> tuple[ConversationSummary, ...]:
        async with self._database.session() as session:
            statement = select(ConversationSummaryModel).order_by(
                ConversationSummaryModel.created_at.desc(), ConversationSummaryModel.id
            )
            if limit is not None:
                if limit < 1:
                    raise ValueError("summary distillation limit must be positive")
                statement = statement.limit(limit)
            models = (await session.execute(statement)).scalars()
            return tuple(_conversation_summary(model) for model in models)

    async def list_usage_patterns(
        self, *, limit: int | None = None
    ) -> tuple[UsagePatternSnapshot, ...]:
        return await self._patterns.list(limit=limit)


class GatewayTextEmbedder:
    """Adapt the ModelGateway embedding capability to the application TextEmbedder."""

    def __init__(self, gateway: ModelGateway, *, dimensions: int = 768) -> None:
        self._gateway = gateway
        self._dimensions = dimensions

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        response = await self._gateway.embed(
            EmbeddingRequest(texts=texts, dimensions=self._dimensions),
            capability=CapabilityAlias.EMBEDDING_ZH,
        )
        return response.vectors


class GatewayMemoryRetriever:
    """Best-effort ``MemoryRetrievalPort``: embed the query, then rank candidates."""

    def __init__(
        self,
        *,
        repository: MemoryEntryRepository,
        embedder: TextEmbedder,
        top_k: int = MEMORY_TOP_K,
    ) -> None:
        self._repository = repository
        self._embedder = embedder
        self._top_k = top_k

    async def retrieve(
        self,
        *,
        query: str,
        conversation_id: UUID,
        sensitivity: ConversationSensitivity,
        limit: int,
    ) -> tuple[MemoryEntry, ...]:
        del conversation_id
        try:
            embedding = (await self._embedder.embed((query,)))[0]
        except Exception:
            logger.exception("memory_query_embedding_failed")
            embedding = ()
        if embedding:
            try:
                candidates = await self._repository.find_similar(embedding, limit=limit * 4)
            except Exception:
                logger.exception("memory_similar_search_failed")
                return ()
        else:
            try:
                candidates = await self._repository.list_all(limit=limit * 4)
            except Exception:
                logger.exception("memory_list_failed")
                return ()
        return select_memories(
            candidates,
            embedding,
            sensitivity=sensitivity,
            limit=limit,
            now=datetime.now(UTC),
        )


def _memory_model(entry: MemoryEntry) -> MemoryEntryModel:
    return MemoryEntryModel(
        memory_id=entry.memory_id,
        entry_type=entry.entry_type.value,
        content=entry.content,
        content_sha256=entry.content_sha256,
        source_conversation_id=entry.source_conversation_id,
        source_summary_id=entry.source_summary_id,
        source_kind=entry.source_kind,
        embedding=list(entry.embedding) if entry.embedding is not None else None,
        sensitivity=entry.sensitivity.value,
        expires_at=entry.expires_at,
        frequency=entry.frequency,
        first_seen_at=entry.first_seen_at,
        updated_at=entry.updated_at,
    )


def _memory(model: MemoryEntryModel) -> MemoryEntry:
    return MemoryEntry(
        entry_type=MemoryEntryType(model.entry_type),
        content=model.content,
        source_conversation_id=model.source_conversation_id,
        sensitivity=ConversationSensitivity(model.sensitivity),
        content_sha256=model.content_sha256,
        embedding=tuple(model.embedding) if model.embedding is not None else None,
        source_kind=model.source_kind,
        source_summary_id=model.source_summary_id,
        expires_at=model.expires_at,
        frequency=model.frequency,
        first_seen_at=model.first_seen_at,
        updated_at=model.updated_at,
        memory_id=model.memory_id,
    )


def _conversation_summary(model: ConversationSummaryModel) -> ConversationSummary:
    return ConversationSummary(
        conversation_id=model.conversation_id,
        space_id=model.space_id,
        run_id=model.run_id,
        covered_start_message_id=model.covered_start_message_id,
        covered_end_message_id=model.covered_end_message_id,
        covered_message_count=model.covered_message_count,
        content=model.content,
        prompt_version=model.prompt_version,
        model_identity=model.model_identity,
        sensitivity=ConversationSensitivity(model.sensitivity),
        summary_id=model.id,
        content_sha256=model.content_sha256,
        created_at=model.created_at,
    )


__all__ = [
    "GatewayMemoryRetriever",
    "GatewayTextEmbedder",
    "PostgresMemoryDistillationSource",
    "PostgresMemoryEntryRepository",
]
