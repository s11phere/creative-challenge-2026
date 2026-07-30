from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from application.qa.feedback_export import (
    FeedbackCandidateExporter,
    FeedbackEvidencePolicy,
    FeedbackExportError,
    FeedbackExportErrorCode,
    FeedbackReview,
)
from domain.grounded_qa import (
    Citation,
    CitationStatus,
    Claim,
    GroundedAnswer,
    QAAttempt,
    QAOutcome,
    QAResult,
    QAStatus,
)
from domain.qa_persistence import (
    FeedbackDecision,
    FeedbackRecord,
    FeedbackReviewStatus,
    QARunRecord,
    QARunVersions,
)
from domain.retrieval import LocatorKind, SearchLocator
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
SPACE_ID = UUID(int=1)
RUN_ID = UUID(int=2)
ATTEMPT_ID = UUID(int=3)
MESSAGE_ID = UUID(int=4)
QUESTION_ID = UUID(int=5)
CONVERSATION_ID = UUID(int=6)
EVIDENCE_ID = UUID(int=7)
FEEDBACK_ID = UUID(int=8)


def _run() -> QARunRecord:
    citation = Citation(
        evidence_id=EVIDENCE_ID,
        space_id=SPACE_ID,
        source_id=UUID(int=10),
        document_id=UUID(int=11),
        version_id=UUID(int=12),
        chunk_id=UUID(int=13),
        locator=SearchLocator(LocatorKind.LINES, 1, 2),
        excerpt_sha256="a" * 64,
        status=CitationStatus.VALID,
    )
    answer = GroundedAnswer(
        text="Synthetic answer",
        claims=(Claim("c1", "Synthetic claim", (EVIDENCE_ID,)),),
        citations=(citation,),
    )
    return QARunRecord(
        run_id=RUN_ID,
        attempt=QAAttempt(run_id=RUN_ID, attempt_id=ATTEMPT_ID),
        conversation_id=CONVERSATION_ID,
        question_message_id=QUESTION_ID,
        space_id=SPACE_ID,
        caller_id="local",
        idempotency_key="run-key",
        versions=QARunVersions(
            "skill-v1",
            "qa-v1",
            "retrieval-v1",
            "fake-chat-v1",
            "prompt-v1",
            "grounded-answer-v1",
            "corpus-v0",
            "dataset-v1",
        ),
        status=QAStatus.COMPLETED,
        result=QAResult(QAOutcome.ANSWER, answer=answer),
        answer_message_id=MESSAGE_ID,
    )


def _feedback() -> FeedbackRecord:
    return FeedbackRecord(
        conversation_id=CONVERSATION_ID,
        message_id=MESSAGE_ID,
        run_id=RUN_ID,
        attempt_id=ATTEMPT_ID,
        space_id=SPACE_ID,
        caller_id="local",
        idempotency_key="feedback-key",
        decision=FeedbackDecision.NEGATIVE,
        feedback_id=FEEDBACK_ID,
        note="private user note must not be exported",
        review_status=FeedbackReviewStatus.ACCEPTED,
    )


def _review() -> FeedbackReview:
    return FeedbackReview(
        feedback_id=FEEDBACK_ID,
        reviewer_id="reviewer-1",
        reviewed_at=datetime(2026, 7, 31, tzinfo=UTC),
        authorization_confirmed=True,
        redaction_complete=True,
        expected_behavior="answer",
        approved_evidence_ids=(EVIDENCE_ID,),
        gold_answer_sha256="b" * 64,
    )


def _policy(**changes: object) -> FeedbackEvidencePolicy:
    values = {
        "evidence_id": EVIDENCE_ID,
        "status": CitationStatus.VALID,
        "sensitivity": "public_demo",
        "allowed_uses": frozenset({"repository_fixture"}),
    }
    values.update(changes)
    return FeedbackEvidencePolicy(**values)  # type: ignore[arg-type]


def test_reviewed_feedback_exports_metadata_only_and_matches_schema() -> None:
    exporter = FeedbackCandidateExporter()
    candidate = exporter.export(
        feedback=_feedback(), run=_run(), review=_review(), evidence_policies=(_policy(),)
    )
    payload = candidate.as_dict()
    schema = json.loads(
        (ROOT / "cases/evals/configs/feedback-candidate-v1.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(schema).validate(payload)
    serialized = json.dumps(payload, sort_keys=True)

    assert candidate.versions["prompt"] == "prompt-v1"
    assert "private user note" not in serialized
    assert "Synthetic answer" not in serialized
    assert "Synthetic claim" not in serialized


@pytest.mark.parametrize(
    ("feedback", "review", "policy", "code"),
    [
        (
            replace(_feedback(), review_status=FeedbackReviewStatus.PENDING_REVIEW),
            _review(),
            _policy(),
            FeedbackExportErrorCode.NOT_REVIEWED,
        ),
        (
            _feedback(),
            replace(_review(), redaction_complete=False),
            _policy(),
            FeedbackExportErrorCode.REDACTION_INCOMPLETE,
        ),
        (
            _feedback(),
            _review(),
            _policy(status=CitationStatus.WITHDRAWN),
            FeedbackExportErrorCode.SOURCE_UNAVAILABLE,
        ),
        (
            _feedback(),
            _review(),
            _policy(sensitivity="private_local", allowed_uses=frozenset()),
            FeedbackExportErrorCode.SOURCE_POLICY_DENIED,
        ),
    ],
)
def test_export_rejects_unreviewed_or_unsafe_candidates(
    feedback: FeedbackRecord,
    review: FeedbackReview,
    policy: FeedbackEvidencePolicy,
    code: FeedbackExportErrorCode,
) -> None:
    with pytest.raises(FeedbackExportError) as raised:
        FeedbackCandidateExporter().export(
            feedback=feedback, run=_run(), review=review, evidence_policies=(policy,)
        )
    assert raised.value.code is code


def test_export_is_deterministic_and_deduplicates_repeated_feedback() -> None:
    item = (_feedback(), _run(), _review(), (_policy(),))
    candidates = FeedbackCandidateExporter().export_unique((item, item))
    assert len(candidates) == 1
    assert len(candidates[0].candidate_id) == 64
