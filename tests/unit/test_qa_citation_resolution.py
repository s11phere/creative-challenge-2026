from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest
from application.qa import PublishedCitationService
from domain.grounded_qa import (
    Citation,
    CitationResolution,
    CitationStatus,
    Claim,
    GroundedAnswer,
    QAAttempt,
    QAOutcome,
    QAResult,
    QAStatus,
)
from domain.qa_persistence import QARunRecord, QARunVersions
from domain.retrieval import LocatorKind, SearchLocator

RUN_ID = UUID(int=1)
SPACE_ID = UUID(int=2)
EVIDENCE_ID = UUID(int=3)


def _citation(*, status: CitationStatus = CitationStatus.VALID) -> Citation:
    return Citation(
        evidence_id=EVIDENCE_ID,
        space_id=SPACE_ID,
        source_id=UUID(int=4),
        document_id=UUID(int=5),
        version_id=UUID(int=6),
        chunk_id=UUID(int=7),
        locator=SearchLocator(LocatorKind.LINES, 2, 3),
        excerpt_sha256="a" * 64,
        status=status,
    )


def _run(citation: Citation | None = None) -> QARunRecord:
    citation = citation or _citation()
    answer = GroundedAnswer(
        text="Published answer",
        claims=(Claim("claim-1", "Published answer", (citation.evidence_id,)),),
        citations=(citation,),
    )
    return QARunRecord(
        run_id=RUN_ID,
        attempt=QAAttempt(run_id=RUN_ID),
        conversation_id=UUID(int=8),
        question_message_id=UUID(int=9),
        space_id=SPACE_ID,
        caller_id="local",
        idempotency_key="question-1",
        versions=QARunVersions(
            skill_version="skill-v1",
            profile_version="profile-v1",
            retrieval_profile_version="retrieval-v1",
            model_identity="fake-v1",
            prompt_version="prompt-v1",
            output_schema_version="answer-v1",
            corpus_version="corpus-v1",
            dataset_version="dataset-v1",
        ),
        status=QAStatus.COMPLETED,
        result=QAResult(outcome=QAOutcome.ANSWER, answer=answer),
        answer_message_id=UUID(int=10),
    )


class FakeRuns:
    def __init__(self, run: QARunRecord | None) -> None:
        self.run = run

    async def get_run(self, _run_id: UUID) -> QARunRecord | None:
        return self.run


class FakeResolver:
    def __init__(self) -> None:
        self.calls: list[Citation] = []

    async def resolve(self, citation: Citation) -> CitationResolution:
        self.calls.append(citation)
        return CitationResolution(citation, CitationStatus.VALID, "line two\nline three")


@pytest.mark.asyncio
async def test_published_citation_service_resolves_only_server_published_evidence() -> None:
    resolver = FakeResolver()
    service = PublishedCitationService(runs=FakeRuns(_run()), resolver=resolver)

    resolution = await service.resolve(RUN_ID, EVIDENCE_ID)

    assert resolution is not None
    assert resolution.excerpt == "line two\nline three"
    assert resolver.calls == [_citation()]
    assert await service.resolve(RUN_ID, UUID(int=99)) is None
    assert resolver.calls == [_citation()]


@pytest.mark.asyncio
async def test_published_citation_service_rejects_non_published_or_cross_space_identity() -> None:
    resolver = FakeResolver()
    draft = replace(_run(), status=QAStatus.QUEUED, result=None, answer_message_id=None)
    assert (
        await PublishedCitationService(runs=FakeRuns(draft), resolver=resolver).resolve(
            RUN_ID, EVIDENCE_ID
        )
        is None
    )

    cross_space = replace(_citation(), space_id=UUID(int=100))
    assert (
        await PublishedCitationService(runs=FakeRuns(_run(cross_space)), resolver=resolver).resolve(
            RUN_ID, EVIDENCE_ID
        )
        is None
    )
    assert resolver.calls == []
