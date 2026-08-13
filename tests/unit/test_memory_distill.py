from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from application.memory import (
    MEMORY_SIMILARITY_THRESHOLD,
    DistillResult,
    MemoryDistiller,
    parse_memory_entries,
)
from domain.conversation_context import (
    ConversationSensitivity,
    ConversationSummary,
)
from domain.memory_entries import (
    MEMORY_CONTENT_LIMIT,
    MemoryEntry,
    MemoryEntryType,
    cosine_similarity,
    normalize_vector,
)
from domain.usage_traces import UsagePatternSnapshot
from model_gateway import (
    CapabilityAlias,
    ChatRequest,
    ChatResponse,
    ModelUsage,
)

_DIMENSIONS = 8


def _summary(
    *,
    index: int,
    content: str = "The user is a researcher working on retrieval.",
    sensitivity: ConversationSensitivity = ConversationSensitivity.PRIVATE_LOCAL,
) -> ConversationSummary:
    return ConversationSummary(
        conversation_id=UUID(int=index),
        space_id=UUID(int=index),
        run_id=uuid4(),
        covered_start_message_id=uuid4(),
        covered_end_message_id=uuid4(),
        covered_message_count=3,
        content=content,
        prompt_version="conversation-summary-prompt-v1",
        model_identity="fake",
        sensitivity=sensitivity,
    )


class InMemoryMemoryDistillationSource:
    def __init__(
        self,
        *,
        summaries: tuple[ConversationSummary, ...] = (),
        patterns: tuple[UsagePatternSnapshot, ...] = (),
    ) -> None:
        self._summaries = summaries
        self._patterns = patterns

    async def list_all_summaries(
        self, *, limit: int | None = None
    ) -> tuple[ConversationSummary, ...]:
        return self._summaries[:limit] if limit is not None else self._summaries

    async def list_usage_patterns(
        self, *, limit: int | None = None
    ) -> tuple[UsagePatternSnapshot, ...]:
        return self._patterns[:limit] if limit is not None else self._patterns


class InMemoryMemoryEntryRepository:
    def __init__(self) -> None:
        self._entries: dict[UUID, MemoryEntry] = {}
        self._by_hash: dict[str, MemoryEntry] = {}

    async def save(self, entry: MemoryEntry) -> MemoryEntry:
        self._entries[entry.memory_id] = entry
        self._by_hash[entry.content_sha256] = entry
        return entry

    async def get(self, memory_id: UUID) -> MemoryEntry | None:
        return self._entries.get(memory_id)

    async def get_by_hash(self, content_sha256: str) -> MemoryEntry | None:
        return self._by_hash.get(content_sha256)

    async def update(self, entry: MemoryEntry) -> MemoryEntry:
        self._entries[entry.memory_id] = entry
        self._by_hash[entry.content_sha256] = entry
        return entry

    async def find_similar(
        self,
        embedding: tuple[float, ...],
        *,
        limit: int,
        entry_type: MemoryEntryType | None = None,
    ) -> tuple[MemoryEntry, ...]:
        candidates = [
            entry
            for entry in self._entries.values()
            if entry.embedding is not None
            and (entry_type is None or entry.entry_type is entry_type)
        ]
        ranked = sorted(
            candidates,
            key=lambda entry: cosine_similarity(embedding, entry.embedding),
            reverse=True,
        )
        return tuple(ranked[:limit])

    async def list_all(self, *, limit: int | None = None) -> tuple[MemoryEntry, ...]:
        ranked = tuple(
            sorted(self._entries.values(), key=lambda entry: entry.updated_at, reverse=True)
        )
        return ranked[:limit] if limit is not None else ranked


class StubChatGateway:
    def __init__(self, *, text: str) -> None:
        self._text = text
        self.requests: list[ChatRequest] = []

    async def chat(
        self,
        request: ChatRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.FAST_CHAT,
    ) -> ChatResponse:
        self.requests.append(request)
        return ChatResponse(
            text=self._text,
            finish_reason="stop",
            usage=ModelUsage(input_tokens=10, output_tokens=5),
            capability=capability,
            latency_ms=1.0,
        )


class StubTextEmbedder:
    """Deterministic embedder mapping exact content to a fixed vector."""

    def __init__(
        self,
        *,
        vectors: dict[str, tuple[float, ...]] | None = None,
        default: tuple[float, ...] = (1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    ) -> None:
        self._vectors = vectors or {}
        self._default = default

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple(self._vectors.get(text, self._default) for text in texts)


def _gateway_with_entries(text: str) -> tuple[StubChatGateway, MemoryDistiller]:
    gateway = StubChatGateway(text=text)
    embedder = StubTextEmbedder()
    return gateway, MemoryDistiller(
        source=InMemoryMemoryDistillationSource(summaries=(_summary(index=1),)),
        entries=InMemoryMemoryEntryRepository(),
        gateway=gateway,  # type: ignore[arg-type]
        embedder=embedder,
    )


class TestParseMemoryEntries:
    def test_parses_json_array(self) -> None:
        text = (
            '[{"type": "fact", "content": "User works on retrieval."}, '
            '{"type": "preference", "content": "Prefers concise answers."}]'
        )
        entries = parse_memory_entries(text)
        assert len(entries) == 2
        assert entries[0].entry_type is MemoryEntryType.FACT
        assert entries[1].entry_type is MemoryEntryType.PREFERENCE
        assert entries[0].content == "User works on retrieval."
        assert entries[0].source_kind == "summary"

    def test_extracts_array_from_surrounding_prose(self) -> None:
        text = 'Here you go:\n[{"type": "pattern", "content": "Reads papers daily."}]\nDone.'
        entries = parse_memory_entries(text)
        assert len(entries) == 1
        assert entries[0].entry_type is MemoryEntryType.PATTERN

    def test_drops_malformed_items(self) -> None:
        text = (
            '[{"type": "fact", "content": "ok"}, '
            '{"type": "bogus", "content": "x"}, '
            '{"content": "no type"}, {"type": "fact"}]'
        )
        entries = parse_memory_entries(text)
        assert len(entries) == 1
        assert entries[0].content == "ok"

    def test_caps_at_eight_entries(self) -> None:
        items = ",".join(f'{{"type": "fact", "content": "fact {index}"}}' for index in range(12))
        entries = parse_memory_entries(f"[{items}]")
        assert len(entries) == 8

    def test_system_assigned_sensitivity_wins(self) -> None:
        text = '[{"type": "fact", "content": "sensitive detail"}]'
        entries = parse_memory_entries(text, sensitivity=ConversationSensitivity.RESTRICTED)
        assert entries[0].sensitivity is ConversationSensitivity.RESTRICTED

    def test_truncates_oversized_content(self) -> None:
        text = f'[{{"type": "fact", "content": "{"x" * (MEMORY_CONTENT_LIMIT + 100)}"}}]'
        entries = parse_memory_entries(text)
        assert len(entries) == 1
        assert len(entries[0].content) == MEMORY_CONTENT_LIMIT

    def test_pattern_source_kind(self) -> None:
        text = '[{"type": "pattern", "source": "pattern", "content": "Daily review habit."}]'
        entries = parse_memory_entries(text)
        assert entries[0].source_kind == "pattern"

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "no json here",
            '{"not": "an array"}',
            "[not valid json",
        ],
    )
    def test_rejects_invalid_input(self, text: str) -> None:
        assert parse_memory_entries(text) == ()


class TestMemoryDistiller:
    async def test_distill_inserts_entries(self) -> None:
        gateway, distiller = _gateway_with_entries(
            '[{"type": "fact", "content": "User is an NLP researcher."}, '
            '{"type": "preference", "content": "Prefers Markdown output."}]'
        )
        result = await distiller.distill()
        assert result == DistillResult(candidates=2, inserted=2, updated=0, merged=0, persisted=2)
        assert len(gateway.requests) == 1

    async def test_distill_exact_duplicate_updates(self) -> None:
        source = InMemoryMemoryDistillationSource(summaries=(_summary(index=1),))
        entries = InMemoryMemoryEntryRepository()
        gateway = StubChatGateway(text='[{"type": "fact", "content": "User likes tea."}]')
        distiller = MemoryDistiller(
            source=source,
            entries=entries,
            gateway=gateway,  # type: ignore[arg-type]
            embedder=StubTextEmbedder(),
        )
        first = await distiller.distill()
        second = await distiller.distill()

        assert first.persisted == 1 and first.updated == 0
        assert second.persisted == 1 and second.updated == 1
        stored = await entries.list_all()
        assert len(stored) == 1
        assert stored[0].frequency == 2

    async def test_distill_semantic_duplicate_merges(self) -> None:
        candidate_text = "User prefers brief answers."
        vector = tuple(float(index + 1) for index in range(_DIMENSIONS))
        entries = InMemoryMemoryEntryRepository()
        seed = MemoryEntry(
            entry_type=MemoryEntryType.PREFERENCE,
            content="User likes short replies.",
            source_conversation_id=None,
            sensitivity=ConversationSensitivity.PRIVATE_LOCAL,
            content_sha256="",
            embedding=normalize_vector(vector),
            source_kind="summary",
        )
        await entries.save(seed)
        distiller = MemoryDistiller(
            source=InMemoryMemoryDistillationSource(summaries=(_summary(index=1),)),
            entries=entries,
            gateway=StubChatGateway(  # type: ignore[arg-type]
                text=f'[{{"type": "preference", "content": "{candidate_text}"}}]'
            ),
            embedder=StubTextEmbedder(vectors={candidate_text: vector}),
        )

        result = await distiller.distill()

        assert result.merged == 1 and result.inserted == 0
        stored = await entries.list_all()
        assert len(stored) == 1
        assert stored[0].frequency == 2
        assert stored[0].content == "User likes short replies."  # kept the original

    async def test_distill_unparseable_chat_is_noop(self) -> None:
        gateway, distiller = _gateway_with_entries("I cannot extract anything.")
        result = await distiller.distill()
        assert result.candidates == 0
        assert result.persisted == 0

    async def test_distill_stores_normalized_embedding(self) -> None:
        content = "User is a systems engineer."
        vector = tuple(float(index + 1) for index in range(_DIMENSIONS))
        distiller = MemoryDistiller(
            source=InMemoryMemoryDistillationSource(summaries=(_summary(index=1),)),
            entries=InMemoryMemoryEntryRepository(),
            gateway=StubChatGateway(  # type: ignore[arg-type]
                text=f'[{{"type": "fact", "content": "{content}"}}]'
            ),
            embedder=StubTextEmbedder(vectors={content: vector}),
        )
        await distiller.distill()
        stored = await distiller._entries.list_all()
        assert len(stored) == 1
        norm = sum(value * value for value in stored[0].embedding or ())
        assert abs(norm - 1.0) < 1e-6

    async def test_distill_inherits_most_restrictive_sensitivity(self) -> None:
        source = InMemoryMemoryDistillationSource(
            summaries=(
                _summary(index=1, sensitivity=ConversationSensitivity.PUBLIC_DEMO),
                _summary(index=2, sensitivity=ConversationSensitivity.RESTRICTED),
            )
        )
        entries = InMemoryMemoryEntryRepository()
        distiller = MemoryDistiller(
            source=source,
            entries=entries,
            gateway=StubChatGateway(  # type: ignore[arg-type]
                text='[{"type": "fact", "content": "private detail"}]'
            ),
            embedder=StubTextEmbedder(),
        )
        await distiller.distill()
        stored = await entries.list_all()
        assert stored[0].sensitivity is ConversationSensitivity.RESTRICTED

    async def test_embedding_failure_still_inserts_without_embedding(self) -> None:
        class FailingEmbedder:
            async def embed(self, _texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
                raise RuntimeError("embedder unavailable")

        entries = InMemoryMemoryEntryRepository()
        distiller = MemoryDistiller(
            source=InMemoryMemoryDistillationSource(summaries=(_summary(index=1),)),
            entries=entries,
            gateway=StubChatGateway(  # type: ignore[arg-type]
                text='[{"type": "fact", "content": "durable fact"}]'
            ),
            embedder=FailingEmbedder(),
        )
        result = await distiller.distill()
        assert result.persisted == 1
        stored = await entries.list_all()
        assert stored[0].embedding is None


def test_similarity_threshold_is_reachable_for_identical_vectors() -> None:
    vector = tuple(float(index) for index in range(_DIMENSIONS))
    assert cosine_similarity(vector, vector) >= MEMORY_SIMILARITY_THRESHOLD
