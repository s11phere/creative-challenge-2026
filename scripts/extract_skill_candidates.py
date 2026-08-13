"""Auto-extract repeated work patterns into personal-Skill drafts (Phase 6, Path B).

Mines strong usage patterns from ``usage_traces``, drafts candidate Skills via the
Phase 4 creator mechanism, and keeps only drafts that pass the dual gate (Phase 1
structural eval + historical exemplar anchoring). Never activates automatically.
By default the extraction runs locally; ``--enqueue`` dispatches it to the
Dramatiq Worker instead (requires Redis).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

from application.skills import (
    DraftSkillEvalRunner,
    PatternExtractionResult,
    PatternExtractionService,
    PersonalSkillStore,
    SkillDraftStore,
)
from infrastructure.config import settings
from infrastructure.database import Database
from infrastructure.qa_execution import assistant_skill_registry
from infrastructure.skill_lifecycle import PostgresSkillActivationStore
from infrastructure.usage_traces import PostgresUsageTraceRepository


def _render(result: PatternExtractionResult) -> dict[str, object]:
    return {
        "candidates": result.candidates,
        "created_drafts": list(result.created_drafts),
        "rejected": [
            {"name": item.name, "reason": item.reason, "detail": item.detail}
            for item in result.rejected
        ],
        "skipped": list(result.skipped),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit a JSON object")
    parser.add_argument(
        "--enqueue",
        action="store_true",
        help="Dispatch extraction to the Dramatiq Worker instead of running locally",
    )
    args = parser.parse_args(argv)

    if args.enqueue:
        try:
            from worker.skill_extraction import enqueue_skill_pattern_extract

            message = enqueue_skill_pattern_extract()
        except Exception as exc:
            print(f"skill extraction enqueue rejected: {exc}", file=sys.stderr)
            return 2
        print(f"skill_pattern_extract enqueued (message_id={message.message_id})")
        return 0

    async def _extract(database: Database) -> PatternExtractionResult:
        try:
            registry = assistant_skill_registry()
            personal_store = PersonalSkillStore(
                registry=registry, store=PostgresSkillActivationStore(database)
            )
            drafts = SkillDraftStore(
                registry=registry,
                personal_store=personal_store,
                eval_runner=DraftSkillEvalRunner(registry=registry),
            )
            return await PatternExtractionService(
                traces=PostgresUsageTraceRepository(database),
                drafts=drafts,
                existing_names=lambda: frozenset(registry.names()).union(
                    frozenset(registry.draft_names())
                ),
            ).extract()
        finally:
            await database.dispose()

    database = Database(settings.database_url)
    try:
        result = asyncio.run(_extract(database))
    except Exception as exc:
        print(f"skill extraction rejected: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(_render(result), ensure_ascii=False, indent=2))
    else:
        print(
            f"mined {result.candidates} candidates; created drafts "
            f"{result.created_drafts or '(none)'}; rejected {len(result.rejected)}; "
            f"skipped {result.skipped or '(none)'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
