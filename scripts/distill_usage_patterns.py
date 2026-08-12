"""Recompute usage-pattern aggregates from raw usage traces (on-demand).

Phase 2 only produces patterns; nothing consumes them yet. By default the
aggregate is recomputed locally and the persisted ``usage_patterns`` snapshot is
replaced atomically. ``--enqueue`` dispatches the same work to the Dramatiq
Worker instead (requires Redis).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

from application.usage_traces import UsagePatternService
from domain.usage_traces import UsagePatternSnapshot
from infrastructure.config import settings
from infrastructure.database import Database
from infrastructure.usage_traces import PostgresUsagePatternRepository, PostgresUsageTraceRepository


def _render(pattern: UsagePatternSnapshot) -> dict[str, object]:
    return {
        "key": pattern.key,
        "skill_name": pattern.skill_name,
        "task_category": pattern.task_category,
        "tool_sequence": pattern.tool_sequence,
        "input_type": pattern.input_type,
        "frequency": pattern.frequency,
        "first_seen_at": pattern.first_seen_at.isoformat(),
        "last_seen_at": pattern.last_seen_at.isoformat(),
    }


def _print_text(patterns: tuple[UsagePatternSnapshot, ...]) -> None:
    if not patterns:
        print("(no usage patterns distilled)")
        return
    for pattern in patterns:
        skill = pattern.skill_name or "assistant"
        print(
            f"{pattern.frequency:>4}x  {skill:<24} {pattern.task_category:<10} "
            f"{pattern.input_type:<6} {pattern.tool_sequence}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit a JSON array")
    parser.add_argument(
        "--enqueue",
        action="store_true",
        help="Dispatch distillation to the Dramatiq Worker instead of running locally",
    )
    args = parser.parse_args(argv)

    if args.enqueue:
        try:
            from worker.usage_traces import enqueue_usage_pattern_distill

            message = enqueue_usage_pattern_distill()
        except Exception as exc:
            print(f"usage pattern distill enqueue rejected: {exc}", file=sys.stderr)
            return 2
        print(f"usage_patterns_distill enqueued (message_id={message.message_id})")
        return 0

    async def _distill(database: Database) -> tuple[UsagePatternSnapshot, ...]:
        try:
            return await UsagePatternService(
                traces=PostgresUsageTraceRepository(database),
                patterns=PostgresUsagePatternRepository(database),
            ).distill_all()
        finally:
            await database.dispose()

    database = Database(settings.database_url)
    try:
        patterns = asyncio.run(_distill(database))
    except Exception as exc:
        print(f"usage pattern distill rejected: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps([_render(pattern) for pattern in patterns], ensure_ascii=False, indent=2))
    else:
        print(f"distilled {len(patterns)} usage patterns")
        _print_text(patterns)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
