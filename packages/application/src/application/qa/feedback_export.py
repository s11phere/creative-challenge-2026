"""Privacy-safe reviewed feedback candidate export."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from domain.grounded_qa import CitationStatus, QAContractError, QAOutcome, QAStatus
from domain.qa_persistence import (
    FeedbackDecision,
    FeedbackRecord,
    FeedbackReviewStatus,
    QARunRecord,
)


class FeedbackExportErrorCode(StrEnum):
    NOT_REVIEWED = "FEEDBACK_NOT_REVIEWED"
    OWNERSHIP_MISMATCH = "FEEDBACK_OWNERSHIP_MISMATCH"
    REDACTION_INCOMPLETE = "FEEDBACK_REDACTION_INCOMPLETE"
    EVIDENCE_INCOMPLETE = "FEEDBACK_EVIDENCE_INCOMPLETE"
    SOURCE_UNAVAILABLE = "FEEDBACK_SOURCE_UNAVAILABLE"
    SOURCE_POLICY_DENIED = "FEEDBACK_SOURCE_POLICY_DENIED"


class FeedbackExportError(QAContractError):
    def __init__(self, code: FeedbackExportErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class FeedbackReview:
    feedback_id: UUID
    reviewer_id: str
    reviewed_at: datetime
    authorization_confirmed: bool
    redaction_complete: bool
    expected_behavior: str
    approved_evidence_ids: tuple[UUID, ...] = ()
    gold_answer_sha256: str | None = None

    def __post_init__(self) -> None:
        if not self.reviewer_id:
            raise ValueError("feedback reviewer is required")
        if self.reviewed_at.tzinfo is None:
            raise ValueError("feedback review timestamp must be timezone-aware")
        if self.expected_behavior not in {"answer", "refuse"}:
            raise ValueError("expected behavior must be answer or refuse")
        if len(self.approved_evidence_ids) != len(set(self.approved_evidence_ids)):
            raise ValueError("approved Evidence IDs must be unique")
        if self.gold_answer_sha256 is not None and not _is_sha256(self.gold_answer_sha256):
            raise ValueError("gold answer digest must be SHA-256")


@dataclass(frozen=True)
class FeedbackEvidencePolicy:
    evidence_id: UUID
    status: CitationStatus
    sensitivity: str
    allowed_uses: frozenset[str]


@dataclass(frozen=True)
class FeedbackCandidate:
    candidate_id: str
    feedback_id: UUID
    run_id: UUID
    attempt_id: UUID
    message_id: UUID
    space_id: UUID
    decision: FeedbackDecision
    expected_behavior: str
    approved_evidence_ids: tuple[UUID, ...]
    gold_answer_sha256: str | None
    versions: dict[str, str]
    reviewed_at: datetime
    schema_version: str = "feedback-candidate-v1"

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "candidate_id": self.candidate_id,
            "feedback_id": str(self.feedback_id),
            "run_id": str(self.run_id),
            "attempt_id": str(self.attempt_id),
            "message_id": str(self.message_id),
            "space_id": str(self.space_id),
            "decision": self.decision.value,
            "expected_behavior": self.expected_behavior,
            "approved_evidence_ids": [str(value) for value in self.approved_evidence_ids],
            "gold_answer_sha256": self.gold_answer_sha256,
            "versions": self.versions,
            "reviewed_at": self.reviewed_at.isoformat(),
        }


class FeedbackCandidateExporter:
    """Export reviewed metadata only; never copy user or source text."""

    def export(
        self,
        *,
        feedback: FeedbackRecord,
        run: QARunRecord,
        review: FeedbackReview,
        evidence_policies: Iterable[FeedbackEvidencePolicy],
    ) -> FeedbackCandidate:
        self._validate_review(feedback, run, review)
        policies = {policy.evidence_id: policy for policy in evidence_policies}
        self._validate_evidence(review, policies)
        versions = {
            "skill": run.versions.skill_version,
            "qa_profile": run.versions.profile_version,
            "retrieval_profile": run.versions.retrieval_profile_version,
            "model": run.versions.model_identity,
            "prompt": run.versions.prompt_version,
            "output_schema": run.versions.output_schema_version,
            "corpus": run.versions.corpus_version,
            "dataset": run.versions.dataset_version,
        }
        identity = {
            "feedback_id": str(feedback.feedback_id),
            "run_id": str(feedback.run_id),
            "message_id": str(feedback.message_id),
            "expected_behavior": review.expected_behavior,
            "evidence_ids": sorted(str(value) for value in review.approved_evidence_ids),
            "versions": versions,
        }
        candidate_id = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return FeedbackCandidate(
            candidate_id=candidate_id,
            feedback_id=feedback.feedback_id,
            run_id=feedback.run_id,
            attempt_id=feedback.attempt_id,
            message_id=feedback.message_id,
            space_id=feedback.space_id,
            decision=feedback.decision,
            expected_behavior=review.expected_behavior,
            approved_evidence_ids=tuple(sorted(review.approved_evidence_ids, key=str)),
            gold_answer_sha256=review.gold_answer_sha256,
            versions=versions,
            reviewed_at=review.reviewed_at,
        )

    def export_unique(
        self,
        items: Iterable[
            tuple[
                FeedbackRecord,
                QARunRecord,
                FeedbackReview,
                Iterable[FeedbackEvidencePolicy],
            ]
        ],
    ) -> tuple[FeedbackCandidate, ...]:
        candidates = {
            candidate.candidate_id: candidate
            for feedback, run, review, policies in items
            for candidate in (
                self.export(
                    feedback=feedback,
                    run=run,
                    review=review,
                    evidence_policies=policies,
                ),
            )
        }
        return tuple(candidates[key] for key in sorted(candidates))

    def _validate_review(
        self, feedback: FeedbackRecord, run: QARunRecord, review: FeedbackReview
    ) -> None:
        if feedback.review_status is not FeedbackReviewStatus.ACCEPTED:
            raise FeedbackExportError(
                FeedbackExportErrorCode.NOT_REVIEWED,
                "feedback must be accepted by human review before export",
            )
        if review.feedback_id != feedback.feedback_id or (
            run.run_id,
            run.attempt.attempt_id,
            run.answer_message_id,
            run.space_id,
        ) != (
            feedback.run_id,
            feedback.attempt_id,
            feedback.message_id,
            feedback.space_id,
        ):
            raise FeedbackExportError(
                FeedbackExportErrorCode.OWNERSHIP_MISMATCH,
                "feedback review target does not match the published run",
            )
        if run.status not in {QAStatus.COMPLETED, QAStatus.REFUSED} or run.result is None:
            raise FeedbackExportError(
                FeedbackExportErrorCode.OWNERSHIP_MISMATCH,
                "feedback candidate requires a published business result",
            )
        if not review.authorization_confirmed or not review.redaction_complete:
            raise FeedbackExportError(
                FeedbackExportErrorCode.REDACTION_INCOMPLETE,
                "authorization and redaction review must both be complete",
            )
        if review.expected_behavior == "answer" and not review.gold_answer_sha256:
            raise FeedbackExportError(
                FeedbackExportErrorCode.EVIDENCE_INCOMPLETE,
                "answer candidates require a reviewed gold-answer digest",
            )
        if run.result.outcome is QAOutcome.FAILED:
            raise FeedbackExportError(
                FeedbackExportErrorCode.OWNERSHIP_MISMATCH,
                "infrastructure failures are not feedback candidates",
            )

    def _validate_evidence(
        self,
        review: FeedbackReview,
        policies: dict[UUID, FeedbackEvidencePolicy],
    ) -> None:
        if review.expected_behavior == "answer" and not review.approved_evidence_ids:
            raise FeedbackExportError(
                FeedbackExportErrorCode.EVIDENCE_INCOMPLETE,
                "answer candidates require approved Evidence",
            )
        for evidence_id in review.approved_evidence_ids:
            policy = policies.get(evidence_id)
            if policy is None or policy.status is not CitationStatus.VALID:
                raise FeedbackExportError(
                    FeedbackExportErrorCode.SOURCE_UNAVAILABLE,
                    "approved Evidence is deleted, withdrawn, or unavailable",
                )
            if (
                policy.sensitivity != "public_demo"
                or "repository_fixture" not in policy.allowed_uses
            ):
                raise FeedbackExportError(
                    FeedbackExportErrorCode.SOURCE_POLICY_DENIED,
                    "Evidence is not approved for repository candidate export",
                )


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


__all__ = [
    "FeedbackCandidate",
    "FeedbackCandidateExporter",
    "FeedbackEvidencePolicy",
    "FeedbackExportError",
    "FeedbackExportErrorCode",
    "FeedbackReview",
]
