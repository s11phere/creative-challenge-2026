"""Export accepted feedback as metadata-only development candidates.

The command never reads or writes question text, answer text, notes, prompts,
provider output, or source excerpts. It refuses to overwrite an existing file
and rejects frozen/holdout paths.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from uuid import UUID

import yaml
from application.qa.feedback_export import (
    FeedbackCandidateExporter,
    FeedbackEvidencePolicy,
    FeedbackReview,
)
from domain.qa_persistence import FeedbackReviewStatus
from infrastructure.config import settings
from infrastructure.database import Database
from infrastructure.qa_persistence import PostgresGroundedQARepository


def _manifest_policies(path: Path) -> dict[str, tuple[str, frozenset[str]]]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    result: dict[str, tuple[str, frozenset[str]]] = {}
    for space in payload.get("spaces", []):
        for source in space.get("sources", []):
            result[source["source_key"]] = (
                source.get("sensitivity", "restricted"),
                frozenset(source.get("allowed_uses", [])),
            )
    return result


def _assert_output_path(path: Path) -> None:
    normalized = "/".join(path.parts).lower()
    forbidden = ("holdout", "manifest.yaml", "datasets/knowledge-qa-v0")
    if any(value in normalized for value in forbidden):
        raise ValueError("feedback candidates cannot overwrite frozen or holdout data")
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {path}")


async def export_candidates(*, output: Path, manifest: Path, space_id: UUID) -> int:
    _assert_output_path(output)
    policies = _manifest_policies(manifest)
    database = Database(settings.database_url)
    repository = PostgresGroundedQARepository(database)
    try:
        items = []
        for feedback in await repository.list_feedback(space_id, FeedbackReviewStatus.ACCEPTED):
            run = await repository.get_run(feedback.run_id)
            if run is None or feedback.reviewed_at is None:
                continue
            evidence = await repository.list_evidence(feedback.attempt_id)
            evidence_policies = []
            for record in evidence:
                sensitivity, allowed_uses = policies.get(
                    record.candidate.source_key, ("restricted", frozenset())
                )
                evidence_policies.append(
                    FeedbackEvidencePolicy(
                        evidence_id=record.candidate.evidence_id,
                        status=record.resolution_status,
                        sensitivity=sensitivity,
                        allowed_uses=allowed_uses,
                    )
                )
            review = FeedbackReview(
                feedback_id=feedback.feedback_id,
                reviewer_id=feedback.reviewer_id or "",
                reviewed_at=feedback.reviewed_at,
                authorization_confirmed=feedback.authorization_confirmed,
                redaction_complete=feedback.redaction_complete,
                expected_behavior=feedback.expected_behavior or "refuse",
                approved_evidence_ids=feedback.approved_evidence_ids,
                gold_answer_sha256=feedback.gold_answer_sha256,
            )
            items.append((feedback, run, review, tuple(evidence_policies)))
        candidates = FeedbackCandidateExporter().export_unique(items)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8", newline="\n") as handle:
            for candidate in candidates:
                handle.write(json.dumps(candidate.as_dict(), sort_keys=True) + "\n")
        return len(candidates)
    finally:
        await database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--manifest", type=Path, default=Path("cases/evals/corpus/v0/manifest.yaml")
    )
    parser.add_argument("--space-id", type=UUID, required=True)
    args = parser.parse_args()
    count = asyncio.run(
        export_candidates(output=args.output, manifest=args.manifest, space_id=args.space_id)
    )
    print(f"exported {count} metadata-only feedback candidates")


if __name__ == "__main__":
    main()
