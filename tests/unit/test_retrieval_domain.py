from __future__ import annotations

from uuid import UUID

import pytest
from domain.retrieval import (
    MAX_SEARCH_QUERY_CHARS,
    CandidateBatch,
    CandidateChannel,
    DenseCandidateQuery,
    KeywordCandidateQuery,
    KeywordLanguageSlice,
    KeywordQueryKind,
    LocatorKind,
    RetrievalCandidate,
    RetrievalError,
    RetrievalErrorCode,
    RetrievalMode,
    RetrievalProfileV1,
    SearchFilters,
    SearchLocator,
    SearchRequest,
    analyze_keyword_query,
    normalize_search_query,
)


def _candidate(*, rank: int = 1, channel: CandidateChannel = CandidateChannel.KEYWORD):
    return RetrievalCandidate(
        chunk_id=UUID(int=1),
        version_id=UUID(int=2),
        document_id=UUID(int=3),
        source_id=UUID(int=4),
        source_key="space/source",
        text="content",
        chunk_hash="a" * 64,
        locators=(SearchLocator(LocatorKind.LINES, 1, 3),),
        channel=channel,
        rank=rank,
        score=0.5,
    )


def test_retrieval_modes_and_error_codes_are_unique() -> None:
    assert {mode.value for mode in RetrievalMode} == {
        "keyword",
        "dense",
        "hybrid",
        "hybrid_rerank",
    }
    assert len({code.value for code in RetrievalErrorCode}) == len(RetrievalErrorCode)


@pytest.mark.parametrize(
    "overrides",
    [
        {"profile_version": "v2"},
        {"keyword_candidate_k": 0},
        {"fusion_alpha": -0.1},
        {"fusion_alpha": 1.1},
        {"adjacent_window": -1},
        {"fusion_candidate_k": 4, "final_k": 5},
        {"fusion_candidate_k": 5, "rerank_k": 6},
        {"reranker_enabled": True, "rerank_k": 4, "final_k": 5},
        {"embedding_version": ""},
        {"expected_embedding_dimensions": 384},
        {"dense_timeout_seconds": 0},
    ],
)
def test_profile_rejects_incompatible_values(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        RetrievalProfileV1(**overrides)  # type: ignore[arg-type]


def test_search_request_rejects_blank_query() -> None:
    with pytest.raises(ValueError, match="blank"):
        SearchRequest(query="  ", space_id=UUID(int=1))


def test_search_query_normalization_preserves_technical_syntax() -> None:
    raw = "  \uff23\uff0b\uff0b\n std::vector\t snake_case_identifier  "
    assert normalize_search_query(raw) == "C++ std::vector snake_case_identifier"
    request = SearchRequest(query=raw, space_id=UUID(int=1))
    assert request.query == "C++ std::vector snake_case_identifier"


def test_search_query_rejects_overlong_input() -> None:
    with pytest.raises(ValueError, match=str(MAX_SEARCH_QUERY_CHARS)):
        SearchRequest(query="x" * (MAX_SEARCH_QUERY_CHARS + 1), space_id=UUID(int=1))


@pytest.mark.parametrize(
    ("query", "language_slice", "query_kind", "literal_terms"),
    [
        (
            "deadlock prevention",
            KeywordLanguageSlice.ENGLISH,
            KeywordQueryKind.NATURAL_LANGUAGE,
            (),
        ),
        (
            "\u5e76\u53d1\u63a7\u5236",
            KeywordLanguageSlice.CHINESE,
            KeywordQueryKind.NATURAL_LANGUAGE,
            (),
        ),
        (
            "\u4f7f\u7528 std::vector",
            KeywordLanguageSlice.MIXED,
            KeywordQueryKind.CODE,
            ("std::vector",),
        ),
        (
            "C++ snake_case C++",
            KeywordLanguageSlice.ENGLISH,
            KeywordQueryKind.CODE,
            ("C++", "snake_case"),
        ),
    ],
)
def test_keyword_query_analysis_reports_language_and_query_kind(
    query: str,
    language_slice: KeywordLanguageSlice,
    query_kind: KeywordQueryKind,
    literal_terms: tuple[str, ...],
) -> None:
    analysis = analyze_keyword_query(query)
    assert analysis.language_slice is language_slice
    assert analysis.query_kind is query_kind
    assert analysis.literal_terms == literal_terms


def test_keyword_candidate_query_carries_normalized_analysis() -> None:
    query = KeywordCandidateQuery("  \uff23\uff0b\uff0b  ", UUID(int=1), SearchFilters(), 5)
    assert query.query == "C++"
    assert query.analysis.literal_terms == ("C++",)


def test_locator_overlap_requires_same_kind_and_inclusive_intersection() -> None:
    lines = SearchLocator(LocatorKind.LINES, 10, 20)
    assert lines.overlaps(SearchLocator(LocatorKind.LINES, 20, 25))
    assert not lines.overlaps(SearchLocator(LocatorKind.LINES, 21, 25))
    assert not lines.overlaps(SearchLocator(LocatorKind.PDF_PAGE, 10, 20))


def test_candidate_batch_rejects_duplicate_rank_and_channel_mismatch() -> None:
    first = _candidate()
    second = RetrievalCandidate(
        **{
            **first.__dict__,
            "chunk_id": UUID(int=9),
        }
    )
    with pytest.raises(ValueError, match="ranks"):
        CandidateBatch(
            channel=CandidateChannel.KEYWORD,
            candidates=(first, second),
            index_version="fts-v1",
            latency_ms=1.0,
        )
    with pytest.raises(ValueError, match="channel"):
        CandidateBatch(
            channel=CandidateChannel.DENSE,
            candidates=(first,),
            index_version="dense-v1",
            latency_ms=1.0,
        )


def test_retrieval_error_exposes_stable_mapping_fields() -> None:
    error = RetrievalError(
        RetrievalErrorCode.EMBEDDING_UNAVAILABLE,
        "Embedding is offline.",
        retryable=True,
        details=(("capability", "embedding_zh"),),
    )
    assert error.code is RetrievalErrorCode.EMBEDDING_UNAVAILABLE
    assert error.retryable is True
    assert error.details == (("capability", "embedding_zh"),)


def test_candidate_queries_reject_unbounded_or_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        KeywordCandidateQuery("query", UUID(int=1), SearchFilters(), 0)
    with pytest.raises(ValueError):
        DenseCandidateQuery((), UUID(int=1), SearchFilters(), 1, "embedding-v1")
    with pytest.raises(ValueError):
        DenseCandidateQuery((float("nan"),), UUID(int=1), SearchFilters(), 1, "embedding-v1")
