"""Pure retrieval contracts, value objects, errors, and provider-neutral ports."""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from uuid import UUID

MAX_SEARCH_QUERY_CHARS = 512
RETRIEVAL_EMBEDDING_DIMENSIONS = 768
_TECHNICAL_TERM_MARKERS = ("++", "::", "#", "_")
_TERM_BOUNDARY_PUNCTUATION = ".,;:!?\uff0c\u3002\uff1b\uff1a\uff01\uff1f()[]{}<>\"'`"
_CHINESE_CHARACTER = re.compile(r"[\u3400-\u9fff]")
_LATIN_CHARACTER = re.compile(r"[A-Za-z]")


class RetrievalMode(StrEnum):
    KEYWORD = "keyword"
    DENSE = "dense"
    HYBRID = "hybrid"
    HYBRID_RERANK = "hybrid_rerank"


class SearchExecutionContext(StrEnum):
    ONLINE = "online"
    OFFLINE_EVALUATION = "offline_evaluation"


class KeywordLanguageSlice(StrEnum):
    CHINESE = "chinese"
    ENGLISH = "english"
    MIXED = "mixed"
    OTHER = "other"


class KeywordQueryKind(StrEnum):
    CODE = "code"
    NATURAL_LANGUAGE = "natural_language"


class CandidateChannel(StrEnum):
    KEYWORD = "keyword"
    DENSE = "dense"


class LocatorKind(StrEnum):
    LINES = "lines"
    PDF_PAGE = "pdf_page"


class HybridEmbeddingFailurePolicy(StrEnum):
    ERROR = "error"
    KEYWORD_FALLBACK = "keyword_fallback"


class RerankerFailurePolicy(StrEnum):
    ERROR = "error"
    FUSED_FALLBACK = "fused_fallback"


class RetrievalErrorCode(StrEnum):
    SPACE_NOT_FOUND = "RETRIEVAL_SPACE_NOT_FOUND"
    INVALID_FILTER = "RETRIEVAL_INVALID_FILTER"
    EMBEDDING_UNAVAILABLE = "RETRIEVAL_EMBEDDING_UNAVAILABLE"
    EMBEDDING_DIMENSION_MISMATCH = "RETRIEVAL_EMBEDDING_DIMENSION_MISMATCH"
    RETRIEVAL_TIMEOUT = "RETRIEVAL_TIMEOUT"
    RERANKER_UNAVAILABLE = "RETRIEVAL_RERANKER_UNAVAILABLE"
    PROFILE_INCOMPATIBLE = "RETRIEVAL_PROFILE_INCOMPATIBLE"
    PROVIDER_POLICY_DENIED = "RETRIEVAL_PROVIDER_POLICY_DENIED"


class RetrievalError(Exception):
    """Stable retrieval failure safe to map at transport boundaries."""

    def __init__(
        self,
        code: RetrievalErrorCode,
        message: str,
        *,
        retryable: bool = False,
        details: tuple[tuple[str, str], ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details


@dataclass(frozen=True)
class SearchFilters:
    """Allowlisted filters that can only narrow a Space-scoped query."""

    source_ids: frozenset[UUID] = frozenset()
    document_ids: frozenset[UUID] = frozenset()


@dataclass(frozen=True)
class KeywordQueryAnalysis:
    normalized_query: str
    language_slice: KeywordLanguageSlice
    query_kind: KeywordQueryKind
    literal_terms: tuple[str, ...] = ()


def normalize_search_query(query: str) -> str:
    """Normalize presentation whitespace while preserving retrieval syntax."""
    if len(query) > MAX_SEARCH_QUERY_CHARS:
        raise ValueError(f"Search query must not exceed {MAX_SEARCH_QUERY_CHARS} characters")
    normalized = " ".join(unicodedata.normalize("NFKC", query).split())
    if not normalized:
        raise ValueError("Search query must not be blank")
    if len(normalized) > MAX_SEARCH_QUERY_CHARS:
        raise ValueError(f"Search query must not exceed {MAX_SEARCH_QUERY_CHARS} characters")
    return normalized


def analyze_keyword_query(query: str) -> KeywordQueryAnalysis:
    normalized = normalize_search_query(query)
    has_chinese = _CHINESE_CHARACTER.search(normalized) is not None
    has_latin = _LATIN_CHARACTER.search(normalized) is not None
    if has_chinese and has_latin:
        language_slice = KeywordLanguageSlice.MIXED
    elif has_chinese:
        language_slice = KeywordLanguageSlice.CHINESE
    elif has_latin:
        language_slice = KeywordLanguageSlice.ENGLISH
    else:
        language_slice = KeywordLanguageSlice.OTHER

    literal_terms: list[str] = []
    for raw_term in normalized.split():
        term = raw_term.strip(_TERM_BOUNDARY_PUNCTUATION)
        if (
            term
            and any(marker in term for marker in _TECHNICAL_TERM_MARKERS)
            and any(character.isalnum() for character in term)
            and term not in literal_terms
        ):
            literal_terms.append(term)
    query_kind = KeywordQueryKind.CODE if literal_terms else KeywordQueryKind.NATURAL_LANGUAGE
    return KeywordQueryAnalysis(
        normalized_query=normalized,
        language_slice=language_slice,
        query_kind=query_kind,
        literal_terms=tuple(literal_terms),
    )


@dataclass(frozen=True)
class SearchRequest:
    query: str
    space_id: UUID
    mode: RetrievalMode = RetrievalMode.HYBRID
    filters: SearchFilters = SearchFilters()
    execution_context: SearchExecutionContext = SearchExecutionContext.ONLINE

    def __post_init__(self) -> None:
        object.__setattr__(self, "query", normalize_search_query(self.query))


@dataclass(frozen=True)
class RetrievalProfileV1:
    profile_version: str = "retrieval-profile-v1"
    keyword_candidate_k: int = 30
    dense_candidate_k: int = 30
    fusion_candidate_k: int = 30
    rrf_k: int = 60
    fusion_alpha: float = 0.5
    reranker_enabled: bool = False
    rerank_k: int = 10
    final_k: int = 5
    adjacent_window: int = 1
    max_chunks_per_document: int = 3
    hybrid_embedding_failure_policy: HybridEmbeddingFailurePolicy = (
        HybridEmbeddingFailurePolicy.ERROR
    )
    reranker_failure_policy: RerankerFailurePolicy = RerankerFailurePolicy.ERROR
    embedding_version: str = "embedding-unset"
    expected_embedding_dimensions: int = 768
    dense_timeout_seconds: float = 15.0

    def __post_init__(self) -> None:
        if self.profile_version != "retrieval-profile-v1":
            raise ValueError("Unsupported retrieval profile version")
        positive_values = (
            self.keyword_candidate_k,
            self.dense_candidate_k,
            self.fusion_candidate_k,
            self.rrf_k,
            self.rerank_k,
            self.final_k,
            self.max_chunks_per_document,
            self.expected_embedding_dimensions,
        )
        if any(value < 1 for value in positive_values):
            raise ValueError("Retrieval profile counts and dimensions must be positive")
        if not 0.0 <= self.fusion_alpha <= 1.0:
            raise ValueError("fusion_alpha must be between 0 and 1")
        if self.adjacent_window < 0:
            raise ValueError("adjacent_window cannot be negative")
        if self.final_k > self.fusion_candidate_k:
            raise ValueError("final_k cannot exceed fusion_candidate_k")
        if self.reranker_enabled and self.final_k > self.rerank_k:
            raise ValueError("final_k cannot exceed rerank_k when reranking is enabled")
        if self.rerank_k > self.fusion_candidate_k:
            raise ValueError("rerank_k cannot exceed fusion_candidate_k")
        if not self.embedding_version:
            raise ValueError("embedding_version must not be empty")
        if self.expected_embedding_dimensions != RETRIEVAL_EMBEDDING_DIMENSIONS:
            raise ValueError("Retrieval embeddings are fixed at 768 dimensions")
        if self.dense_timeout_seconds <= 0 or not math.isfinite(self.dense_timeout_seconds):
            raise ValueError("dense_timeout_seconds must be finite and positive")


@dataclass(frozen=True)
class SearchLocator:
    kind: LocatorKind
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 1 or self.end < self.start:
            raise ValueError("Search locator must be a one-based inclusive interval")

    def overlaps(self, other: SearchLocator) -> bool:
        return self.kind is other.kind and self.start <= other.end and other.start <= self.end


@dataclass(frozen=True)
class RetrievalCandidate:
    chunk_id: UUID
    version_id: UUID
    document_id: UUID
    source_id: UUID
    source_key: str
    text: str
    chunk_hash: str
    locators: tuple[SearchLocator, ...]
    channel: CandidateChannel
    rank: int
    score: float

    def __post_init__(self) -> None:
        if self.rank < 1:
            raise ValueError("Candidate rank must be one-based")
        if not math.isfinite(self.score):
            raise ValueError("Candidate score must be finite")
        if not self.source_key or not self.chunk_hash:
            raise ValueError("Candidate source_key and chunk_hash must not be empty")


@dataclass(frozen=True)
class CandidateBatch:
    channel: CandidateChannel
    candidates: tuple[RetrievalCandidate, ...]
    index_version: str
    latency_ms: float

    def __post_init__(self) -> None:
        if not self.index_version:
            raise ValueError("Candidate batch index_version must not be empty")
        if self.latency_ms < 0 or not math.isfinite(self.latency_ms):
            raise ValueError("Candidate batch latency must be finite and non-negative")
        if any(candidate.channel is not self.channel for candidate in self.candidates):
            raise ValueError("Candidate channel does not match its batch")
        ranks = tuple(candidate.rank for candidate in self.candidates)
        chunk_ids = tuple(candidate.chunk_id for candidate in self.candidates)
        if len(ranks) != len(set(ranks)):
            raise ValueError("Candidate batch ranks must be unique")
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("Candidate batch chunk IDs must be unique")


@dataclass(frozen=True)
class KeywordCandidateQuery:
    query: str
    space_id: UUID
    filters: SearchFilters
    limit: int
    analysis: KeywordQueryAnalysis = field(init=False)

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("Keyword candidate limit must be positive")
        analysis = analyze_keyword_query(self.query)
        object.__setattr__(self, "query", analysis.normalized_query)
        object.__setattr__(self, "analysis", analysis)


@dataclass(frozen=True)
class DenseCandidateQuery:
    query_vector: tuple[float, ...]
    space_id: UUID
    filters: SearchFilters
    limit: int
    embedding_version: str

    def __post_init__(self) -> None:
        if self.limit < 1 or not self.embedding_version:
            raise ValueError("Dense candidate limit and embedding version must be valid")
        if not self.query_vector or any(not math.isfinite(value) for value in self.query_vector):
            raise ValueError("Dense candidate query vector must contain finite values")


@dataclass(frozen=True)
class QueryEmbedding:
    vector: tuple[float, ...]
    model_version: str
    latency_ms: float

    def __post_init__(self) -> None:
        if not self.model_version:
            raise ValueError("Query embedding model_version must not be empty")
        if self.latency_ms < 0 or not math.isfinite(self.latency_ms):
            raise ValueError("Query embedding latency must be finite and non-negative")
        if not self.vector or any(not math.isfinite(value) for value in self.vector):
            raise ValueError("Query embedding must contain finite values")


@dataclass(frozen=True)
class RerankDocument:
    index: int
    chunk_id: UUID
    text: str


@dataclass(frozen=True)
class RerankRequest:
    query: str
    documents: tuple[RerankDocument, ...]

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValueError("Rerank query must not be blank")
        indices = tuple(document.index for document in self.documents)
        chunk_ids = tuple(document.chunk_id for document in self.documents)
        if indices != tuple(range(len(self.documents))):
            raise ValueError("Rerank document indices must be contiguous and zero-based")
        if len(chunk_ids) != len(set(chunk_ids)):
            raise ValueError("Rerank document chunk IDs must be unique")


@dataclass(frozen=True)
class RerankScore:
    index: int
    score: float

    def __post_init__(self) -> None:
        if self.index < 0 or not math.isfinite(self.score):
            raise ValueError("Rerank result index and score must be valid")


@dataclass(frozen=True)
class RerankResponse:
    scores: tuple[RerankScore, ...]
    model_version: str
    latency_ms: float

    def __post_init__(self) -> None:
        if not self.model_version:
            raise ValueError("Reranker model_version must not be empty")
        if self.latency_ms < 0 or not math.isfinite(self.latency_ms):
            raise ValueError("Reranker latency must be finite and non-negative")


@dataclass(frozen=True)
class SearchHitSummary:
    """Log-safe hit metadata that never contains source text."""

    chunk_hash: str
    text_length: int
    locator_count: int

    def __post_init__(self) -> None:
        if not self.chunk_hash or self.text_length < 0 or self.locator_count < 0:
            raise ValueError("Search hit summary values must be valid")


@dataclass(frozen=True)
class SearchHit:
    chunk_id: UUID
    version_id: UUID
    document_id: UUID
    source_id: UUID
    source_key: str
    text: str
    chunk_hash: str
    safe_summary: SearchHitSummary
    locators: tuple[SearchLocator, ...]
    final_rank: int
    keyword_rank: int | None = None
    keyword_score: float | None = None
    dense_rank: int | None = None
    dense_score: float | None = None
    fused_rank: int | None = None
    fused_score: float | None = None
    rerank_rank: int | None = None
    rerank_score: float | None = None
    context_only: bool = False


@dataclass(frozen=True)
class CandidateCounts:
    keyword: int = 0
    dense: int = 0
    fused: int = 0
    reranked: int = 0
    final: int = 0


@dataclass(frozen=True)
class StageTiming:
    stage: str
    latency_ms: float

    def __post_init__(self) -> None:
        if self.latency_ms < 0 or not math.isfinite(self.latency_ms):
            raise ValueError("Stage latency must be finite and non-negative")


@dataclass(frozen=True)
class SearchDiagnostics:
    requested_mode: RetrievalMode
    executed_mode: RetrievalMode
    profile_version: str
    embedding_version: str | None
    reranker_version: str | None
    keyword_index_version: str | None
    dense_index_version: str | None
    candidate_counts: CandidateCounts
    stage_timings: tuple[StageTiming, ...]
    keyword_language_slice: KeywordLanguageSlice | None = None
    keyword_query_kind: KeywordQueryKind | None = None
    keyword_literal_term_count: int = 0
    dense_language_slice: KeywordLanguageSlice | None = None
    dense_query_kind: KeywordQueryKind | None = None
    degraded: bool = False
    degradation_reasons: tuple[RetrievalErrorCode, ...] = ()
    filter_reasons: tuple[str, ...] = ()
    source_filter_count: int = 0
    document_filter_count: int = 0


@dataclass(frozen=True)
class SearchResult:
    hits: tuple[SearchHit, ...]
    diagnostics: SearchDiagnostics


class RetrievalStore(Protocol):
    """Raw candidate access constrained to a Space and narrowing filters."""

    async def keyword_candidates(self, query: KeywordCandidateQuery) -> CandidateBatch: ...

    async def dense_candidates(self, query: DenseCandidateQuery) -> CandidateBatch: ...


class QueryEmbedder(Protocol):
    async def embed_query(self, query: str) -> QueryEmbedding: ...


class Reranker(Protocol):
    async def rerank(self, request: RerankRequest) -> RerankResponse: ...
