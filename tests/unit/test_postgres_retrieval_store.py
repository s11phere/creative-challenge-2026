"""Unit checks for PostgreSQL retrieval adapter configuration and locators."""

from __future__ import annotations

from typing import cast

import pytest
from domain.retrieval import LocatorKind, SearchLocator
from infrastructure.retrieval.postgres_store import PostgresRetrievalStore, _locators
from sqlalchemy.ext.asyncio import AsyncSession


def test_ivfflat_probes_must_be_positive() -> None:
    with pytest.raises(ValueError, match="ivfflat_probes must be positive"):
        PostgresRetrievalStore(cast(AsyncSession, object()), ivfflat_probes=0)


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
