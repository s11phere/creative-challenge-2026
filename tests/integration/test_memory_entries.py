from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from domain.conversation_context import ConversationSensitivity
from domain.memory_entries import MemoryEntry, MemoryEntryType
from infrastructure.config import settings
from infrastructure.database import Database
from infrastructure.memory_entries import (
    PostgresMemoryDistillationSource,
    PostgresMemoryEntryRepository,
)
from infrastructure.orm import MemoryEntryModel
from sqlalchemy import delete

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION") != "1",
        reason="set RUN_INTEGRATION=1 with an isolated migrated PostgreSQL database",
    ),
]

_DIMENSIONS = 768
_UNIT_A = tuple(1.0 for _ in range(_DIMENSIONS))
_UNIT_B = tuple(-1.0 for _ in range(_DIMENSIONS))


def _entry(*, content: str, embedding: tuple[float, ...] | None = _UNIT_A) -> MemoryEntry:
    return MemoryEntry(
        entry_type=MemoryEntryType.FACT,
        content=content,
        source_conversation_id=None,
        sensitivity=ConversationSensitivity.PRIVATE_LOCAL,
        content_sha256="",
        embedding=embedding,
        source_kind="summary",
    )


@pytest.mark.asyncio
async def test_memory_entry_repository_roundtrip_similarity_and_update() -> None:
    database = Database(settings.database_url)
    repository = PostgresMemoryEntryRepository(database)
    entry_a = _entry(content="The user prefers concise answers.", embedding=_UNIT_A)
    entry_b = _entry(content="The user studies dense retrieval.", embedding=_UNIT_B)
    memory_ids = [entry_a.memory_id, entry_b.memory_id]
    try:
        saved_a = await repository.save(entry_a)
        await repository.save(entry_b)

        assert await repository.get(saved_a.memory_id) == saved_a
        assert await repository.get_by_hash(saved_a.content_sha256) == saved_a
        assert len(await repository.list_all()) >= 2

        similar = await repository.find_similar(_UNIT_A, limit=1)
        assert similar[0].memory_id == saved_a.memory_id

        refreshed = await repository.update(_bump_frequency(saved_a))
        assert (await repository.get(refreshed.memory_id)) == refreshed

        listed = await repository.list_all(limit=1)
        assert listed[0].memory_id == refreshed.memory_id  # updated_at-desc ordering
    finally:
        await _cleanup(database, memory_ids)
        await database.dispose()


@pytest.mark.asyncio
async def test_memory_distillation_source_is_queryable_when_empty() -> None:
    database = Database(settings.database_url)
    source = PostgresMemoryDistillationSource(database)
    try:
        assert await source.list_all_summaries() == ()
        assert await source.list_usage_patterns() == ()
    finally:
        await database.dispose()


async def _cleanup(database: Database, memory_ids: list[UUID]) -> None:
    async with database.transaction() as session:
        for memory_id in memory_ids:
            await session.execute(
                delete(MemoryEntryModel).where(MemoryEntryModel.memory_id == memory_id)
            )


def _bump_frequency(entry: MemoryEntry) -> MemoryEntry:
    return replace(
        entry,
        frequency=entry.frequency + 1,
        updated_at=datetime.now(UTC) + timedelta(seconds=60),
    )
