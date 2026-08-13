"""Cross-session long-term memory use cases (personalization Phase 5).

Distillation (``distill``) turns conversation summaries and Phase 2 usage patterns
into durable ``memory_entries``; retrieval (``retrieval``) selects a bounded,
sensitivity-filtered slice for injection into new Assistant turns.
"""

from .distill import (
    MEMORY_SIMILARITY_THRESHOLD,
    DistillResult,
    MemoryDistillationSource,
    MemoryDistiller,
    PersistedOutcome,
    parse_memory_entries,
)
from .retrieval import (
    MEMORY_RECENCY_HALF_LIFE_DAYS,
    MEMORY_RECENCY_WEIGHT,
    MEMORY_TOP_K,
    MemoryRetrievalPort,
    injectable,
    recency_factor,
    score_memory,
    select_memories,
)

__all__ = [
    "DistillResult",
    "MEMORY_RECENCY_HALF_LIFE_DAYS",
    "MEMORY_RECENCY_WEIGHT",
    "MEMORY_SIMILARITY_THRESHOLD",
    "MEMORY_TOP_K",
    "MemoryDistillationSource",
    "MemoryDistiller",
    "MemoryRetrievalPort",
    "PersistedOutcome",
    "injectable",
    "parse_memory_entries",
    "recency_factor",
    "score_memory",
    "select_memories",
]
