"""Resolve citations that were atomically published with a QA answer."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from domain.grounded_qa import Citation, CitationResolution, CitationStatus
from domain.qa_persistence import QARunRecord


class QARunReader(Protocol):
    async def get_run(self, run_id: UUID) -> QARunRecord | None: ...


class CitationResolutionPort(Protocol):
    async def resolve(self, citation: Citation) -> CitationResolution: ...


class PublishedCitationApplicationPort(Protocol):
    async def resolve(self, run_id: UUID, evidence_id: UUID) -> CitationResolution | None: ...


class PublishedCitationService:
    """Resolve only a server-published citation belonging to the requested run."""

    def __init__(self, *, runs: QARunReader, resolver: CitationResolutionPort) -> None:
        self._runs = runs
        self._resolver = resolver

    async def resolve(self, run_id: UUID, evidence_id: UUID) -> CitationResolution | None:
        run = await self._runs.get_run(run_id)
        if run is None or run.result is None or run.result.answer is None:
            return None
        citation = next(
            (
                item
                for item in run.result.answer.citations
                if item.evidence_id == evidence_id
                and item.space_id == run.space_id
                and item.status is CitationStatus.VALID
            ),
            None,
        )
        if citation is None:
            return None
        return await self._resolver.resolve(citation)


__all__ = [
    "CitationResolutionPort",
    "PublishedCitationApplicationPort",
    "PublishedCitationService",
    "QARunReader",
]
