"""Distillation of durable long-term memory from summaries and usage patterns.

Phase 5 reuses the Phase 2 pipeline shape: a worker task reads ``conversation_summaries``
and ``usage_patterns``, an LLM extracts stable facts/preferences/patterns, and the
results are written into ``memory_entries`` with an update-not-duplicate strategy.
Privacy gate: the distilled ``content`` is a bounded standalone statement, never a
full prompt or private body, and sensitivity is inherited from the source summaries.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from importlib.resources import files
from typing import Protocol

from domain.conversation_context import (
    ConversationSensitivity,
    ConversationSummary,
    most_restrictive_sensitivity,
)
from domain.memory_entries import (
    MEMORY_CONTENT_LIMIT,
    MemoryEntry,
    MemoryEntryRepository,
    MemoryEntryType,
    cosine_similarity,
    normalize_vector,
)
from domain.usage_traces import UsagePatternSnapshot
from model_gateway import CapabilityAlias, ChatMessage, ChatRequest, ChatRole, ModelGateway

from application.ingestion.embedding import TextEmbedder

_DISTILL_PROMPT = (
    files("application.memory")
    .joinpath("contracts", "memory-distill-prompt-v1.txt")
    .read_text(encoding="utf-8")
)

MEMORY_SIMILARITY_THRESHOLD = 0.92
_MEMORY_DISTILL_MAX_ENTRIES = 8
_SUMMARY_WINDOW = 10
_PATTERN_WINDOW = 10
_SUMMARY_CHUNK_LIMIT = 4_000
logger = logging.getLogger(__name__)


class MemoryDistillationSource(Protocol):
    """Cross-conversation read port consumed by the distillation worker."""

    async def list_all_summaries(
        self, *, limit: int | None = None
    ) -> tuple[ConversationSummary, ...]: ...

    async def list_usage_patterns(
        self, *, limit: int | None = None
    ) -> tuple[UsagePatternSnapshot, ...]: ...


class PersistedOutcome(StrEnum):
    """How one candidate entry was persisted (update-not-duplicate)."""

    INSERTED = "inserted"
    UPDATED = "updated"
    MERGED = "merged"


@dataclass(frozen=True)
class DistillResult:
    """Bounded outcome counters for one memory-distillation pass."""

    candidates: int
    inserted: int
    updated: int
    merged: int
    persisted: int


def parse_memory_entries(
    text: str, *, sensitivity: ConversationSensitivity = ConversationSensitivity.PRIVATE_LOCAL
) -> tuple[MemoryEntry, ...]:
    """Parse a bounded JSON array of ``{type, content}`` into candidate entries.

    Sensitivity is system-assigned (inherited from the source summaries), never
    taken from the model output. Malformed or oversized entries are dropped.
    """
    if not text.strip():
        return ()
    extracted = _extract_json_array(text)
    if extracted is None:
        return ()
    try:
        payload = json.loads(extracted)
    except json.JSONDecodeError:
        return ()
    if not isinstance(payload, list):
        return ()
    entries: list[MemoryEntry] = []
    for item in payload[:_MEMORY_DISTILL_MAX_ENTRIES]:
        if not isinstance(item, dict):
            continue
        entry_type = _parse_entry_type(item.get("type"))
        content = item.get("content")
        if entry_type is None or not isinstance(content, str):
            continue
        content = content.strip()
        if not content:
            continue
        if len(content) > MEMORY_CONTENT_LIMIT:
            content = content[:MEMORY_CONTENT_LIMIT]
        entries.append(
            MemoryEntry(
                entry_type=entry_type,
                content=content,
                source_conversation_id=None,
                sensitivity=sensitivity,
                content_sha256="",
                source_kind="pattern" if item.get("source") == "pattern" else "summary",
            )
        )
    return tuple(entries)


class MemoryDistiller:
    """Extract durable memory entries and persist them update-not-duplicate."""

    def __init__(
        self,
        *,
        source: MemoryDistillationSource,
        entries: MemoryEntryRepository,
        gateway: ModelGateway,
        embedder: TextEmbedder,
        summary_window: int = _SUMMARY_WINDOW,
        pattern_window: int = _PATTERN_WINDOW,
    ) -> None:
        self._source = source
        self._entries = entries
        self._gateway = gateway
        self._embedder = embedder
        self._summary_window = summary_window
        self._pattern_window = pattern_window

    async def distill(self) -> DistillResult:
        summaries = await self._source.list_all_summaries(limit=self._summary_window)
        patterns = await self._source.list_usage_patterns(limit=self._pattern_window)
        sensitivity = most_restrictive_sensitivity(
            tuple(item.sensitivity for item in summaries)
            or (ConversationSensitivity.PRIVATE_LOCAL,)
        )
        response = await self._gateway.chat(
            ChatRequest(
                messages=(
                    ChatMessage(role=ChatRole.SYSTEM, content=_DISTILL_PROMPT),
                    ChatMessage(role=ChatRole.USER, content=_render_input(summaries, patterns)),
                ),
                temperature=0.0,
                max_tokens=1_500,
            ),
            capability=CapabilityAlias.FAST_CHAT,
        )
        candidates = parse_memory_entries(response.text, sensitivity=sensitivity)
        inserted = updated = merged = 0
        for candidate in candidates:
            outcome = await self._persist(candidate)
            if outcome is PersistedOutcome.INSERTED:
                inserted += 1
            elif outcome is PersistedOutcome.UPDATED:
                updated += 1
            else:
                merged += 1
        return DistillResult(
            candidates=len(candidates),
            inserted=inserted,
            updated=updated,
            merged=merged,
            persisted=inserted + updated + merged,
        )

    async def _persist(self, candidate: MemoryEntry) -> PersistedOutcome:
        existing = await self._entries.get_by_hash(candidate.content_sha256)
        if existing is not None:
            await self._entries.update(
                _refreshed(existing, updated_at=candidate.updated_at, embedding=candidate.embedding)
            )
            return PersistedOutcome.UPDATED
        embedding = await self._embed_candidate(candidate)
        if embedding:
            similar = await self._entries.find_similar(
                embedding, limit=1, entry_type=candidate.entry_type
            )
            if similar and (
                cosine_similarity(embedding, similar[0].embedding) >= MEMORY_SIMILARITY_THRESHOLD
            ):
                await self._entries.update(
                    _refreshed(similar[0], updated_at=candidate.updated_at, embedding=embedding)
                )
                return PersistedOutcome.MERGED
        await self._entries.save(replace(candidate, embedding=embedding or None))
        return PersistedOutcome.INSERTED

    async def _embed_candidate(self, candidate: MemoryEntry) -> tuple[float, ...]:
        try:
            vector = (await self._embedder.embed((candidate.content,)))[0]
            return normalize_vector(vector)
        except Exception:
            logger.exception("memory_candidate_embedding_failed")
            return ()


def _refreshed(
    entry: MemoryEntry,
    *,
    updated_at: datetime,
    embedding: tuple[float, ...] | None = None,
) -> MemoryEntry:
    return replace(
        entry,
        frequency=entry.frequency + 1,
        updated_at=updated_at,
        embedding=embedding or entry.embedding,
    )


def _render_input(
    summaries: tuple[ConversationSummary, ...],
    patterns: tuple[UsagePatternSnapshot, ...],
) -> str:
    parts = ['<distillation-input trust="untrusted_user">']
    if summaries:
        parts.append("<conversation-summaries>")
        for index, summary in enumerate(summaries):
            parts.extend(
                [
                    f'<summary index="{index}" sensitivity="{summary.sensitivity.value}">',
                    summary.content[:_SUMMARY_CHUNK_LIMIT],
                    "</summary>",
                ]
            )
        parts.append("</conversation-summaries>")
    if patterns:
        parts.append("<usage-patterns>")
        for pattern in patterns:
            skill = pattern.skill_name or "assistant"
            parts.append(
                f'<pattern skill="{skill}" category="{pattern.task_category}" '
                f'frequency="{pattern.frequency}" input_type="{pattern.input_type}" '
                f'tools="{pattern.tool_sequence}" />'
            )
        parts.append("</usage-patterns>")
    parts.append("</distillation-input>")
    return "\n".join(parts)


def _extract_json_array(text: str) -> str | None:
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end <= start:
        return None
    return text[start : end + 1]


def _parse_entry_type(value: object) -> MemoryEntryType | None:
    if not isinstance(value, str):
        return None
    try:
        return MemoryEntryType(value)
    except ValueError:
        return None


__all__ = [
    "DistillResult",
    "MEMORY_SIMILARITY_THRESHOLD",
    "MemoryDistillationSource",
    "MemoryDistiller",
    "PersistedOutcome",
    "parse_memory_entries",
]
