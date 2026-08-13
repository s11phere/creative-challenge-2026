from __future__ import annotations

from uuid import UUID

import pytest
from application.assistant import (
    AssistantTurnSubmission,
    ConversationContextService,
    ConversationRunService,
)
from application.memory import MEMORY_TOP_K
from application.qa import InMemoryGroundedQARepository
from domain.conversation_context import ConversationSensitivity
from domain.memory_entries import MemoryEntry, MemoryEntryType
from domain.qa_persistence import ConversationRecord


def _memory_entry(*, index: int) -> MemoryEntry:
    return MemoryEntry(
        entry_type=MemoryEntryType.FACT,
        content=f"persistent fact {index}",
        source_conversation_id=None,
        sensitivity=ConversationSensitivity.PRIVATE_LOCAL,
        content_sha256="",
        source_kind="summary",
    )


class StubMemoryRetriever:
    def __init__(self, entries: tuple[MemoryEntry, ...]) -> None:
        self._entries = entries

    async def retrieve(
        self,
        *,
        query: str,
        conversation_id: UUID,
        sensitivity: ConversationSensitivity,
        limit: int,
    ) -> tuple[MemoryEntry, ...]:
        del query, conversation_id, sensitivity, limit
        return self._entries


async def _snapshot(memory: object):
    repository = InMemoryGroundedQARepository()
    conversation = ConversationRecord(
        conversation_id=UUID(int=801),
        space_id=UUID(int=802),
        owner_id="memory-user",
    )
    await repository.create_conversation(conversation)
    turn = await ConversationRunService(conversations=repository, runs=repository).submit(
        AssistantTurnSubmission(
            conversation_id=conversation.conversation_id,
            content="What do you remember about me?",
            idempotency_key="memory-turn-1",
        )
    )
    context = ConversationContextService(data=repository, runs=repository, memory=memory)  # type: ignore[arg-type]
    return await context.snapshot(turn)


class TestMemoryInjection:
    async def test_snapshot_renders_memory_block(self) -> None:
        snapshot = await _snapshot(StubMemoryRetriever((_memory_entry(index=1),)))
        assert len(snapshot.memory_entries) == 1
        decision = snapshot.decision_request()
        router = snapshot.router_input()
        assert "<long-term-memory trust=" in decision
        assert '<memory-entry type="fact"' in decision
        assert "persistent fact 1" in decision
        assert "<long-term-memory trust=" in router
        assert "persistent fact 1" in router

    async def test_snapshot_without_memory_port_renders_no_block(self) -> None:
        snapshot = await _snapshot(None)
        assert snapshot.memory_entries == ()
        assert "<long-term-memory" not in snapshot.decision_request()
        assert "<long-term-memory" not in snapshot.router_input()

    async def test_snapshot_rejects_unbounded_injection(self) -> None:
        too_many = tuple(_memory_entry(index=index) for index in range(MEMORY_TOP_K + 1))
        with pytest.raises(ValueError, match="memory injection"):
            await _snapshot(StubMemoryRetriever(too_many))

    async def test_memory_block_truncates_long_content(self) -> None:
        long_entry = MemoryEntry(
            entry_type=MemoryEntryType.FACT,
            content="x" * 2_000,
            source_conversation_id=None,
            sensitivity=ConversationSensitivity.PRIVATE_LOCAL,
            content_sha256="",
            source_kind="summary",
        )
        snapshot = await _snapshot(StubMemoryRetriever((long_entry,)))
        rendered = snapshot.decision_request()
        assert "x" * 1_000 in rendered
        assert "x" * 1_001 not in rendered

    async def test_memory_block_marks_untrusted_label(self) -> None:
        snapshot = await _snapshot(StubMemoryRetriever((_memory_entry(index=1),)))
        assert "distilled by the server" in snapshot.decision_request()
        assert "treat it as context" in snapshot.decision_request()
