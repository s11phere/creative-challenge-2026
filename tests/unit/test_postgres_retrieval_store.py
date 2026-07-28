"""Unit checks for PostgreSQL retrieval adapter configuration and locators."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock

import pytest
from domain.retrieval import LocatorKind, SearchLocator, analyze_keyword_query
from infrastructure.retrieval.postgres_store import (
    PostgresRetrievalStore,
    _disjunctive_fts_query,
    _locators,
)
from sqlalchemy.ext.asyncio import AsyncSession


def test_ivfflat_probes_must_be_positive() -> None:
    with pytest.raises(ValueError, match="ivfflat_probes must be positive"):
        PostgresRetrievalStore(cast(AsyncSession, object()), ivfflat_probes=0)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("transaction retry deadline", "transaction OR retry OR deadline"),
        ("并发控制，为什么使用互斥锁？", "并发控制 OR 为什么使用互斥锁"),
        ("C++ std::vector", "C OR std OR vector"),
    ],
)
def test_keyword_fts_query_uses_bounded_disjunction(query: str, expected: str) -> None:
    assert _disjunctive_fts_query(analyze_keyword_query(query)) == expected


async def test_ivfflat_setting_loads_pgvector_on_a_fresh_backend() -> None:
    session = AsyncMock(spec=AsyncSession)
    session.scalar.side_effect = [None, "1"]
    store = PostgresRetrievalStore(session)

    async with store._planner_settings({"ivfflat.probes": "10"}):
        pass

    assert session.scalar.await_count == 2
    assert session.execute.await_count == 3
    load_statement = str(session.execute.await_args_list[0].args[0])
    assert "SELECT '[0]'::vector" in load_statement


def test_locators_preserve_valid_line_and_page_ranges() -> None:
    assert _locators(
        {
            "start_line": "2",
            "end_line": 5,
            "start_page": 3,
            "end_page": "4",
        }
    ) == (
        SearchLocator(kind=LocatorKind.LINES, start=2, end=5),
        SearchLocator(kind=LocatorKind.PDF_PAGE, start=3, end=4),
    )


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        [],
        {"start_line": 0, "end_line": 1},
        {"start_line": 3, "end_line": 2},
        {"start_line": True, "end_line": 2},
        {"start_page": "not-an-integer", "end_page": 2},
    ],
)
def test_locators_ignore_invalid_metadata(metadata: object) -> None:
    assert _locators(metadata) == ()
