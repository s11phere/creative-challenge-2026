"""Read-only view of recorded personal usage traces (debugging / Phase 6 备料).

The command never mutates data: it lists sanitized ``usage_traces`` rows ordered
by creation time. Input summaries are already bounded and redacted at record
time; this CLI only renders them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from datetime import datetime

from domain.usage_traces import UsageTrace
from infrastructure.config import settings
from infrastructure.database import Database
from infrastructure.usage_traces import PostgresUsageTraceRepository


def _render(trace: UsageTrace) -> dict[str, object]:
    return {
        "run_id": str(trace.run_id),
        "conversation_id": str(trace.conversation_id),
        "skill_name": trace.skill_name,
        "command": trace.command,
        "input_summary": trace.input_summary,
        "tools_used": list(trace.tools_used),
        "outcome": trace.outcome.value,
        "model": trace.model,
        "sensitivity": trace.sensitivity.value,
        "created_at": trace.created_at.isoformat(),
    }


def _print_text(traces: tuple[UsageTrace, ...]) -> None:
    if not traces:
        print("(no usage traces recorded)")
        return
    width = max((len(trace.skill_name or trace.command or "?") for trace in traces), default=8)
    for trace in traces:
        identity = trace.skill_name or trace.command or "?"
        print(
            f"{trace.created_at.isoformat()}  {identity:<{width}}  "
            f"{trace.outcome.value:<10}  {trace.sensitivity.value:<14}  "
            f"{trace.input_summary}"
        )


async def _query(
    database: Database, *, limit: int, since: datetime | None
) -> tuple[UsageTrace, ...]:
    try:
        return await PostgresUsageTraceRepository(database).list(limit=limit, since=since)
    finally:
        await database.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=50, help="Maximum traces to list")
    parser.add_argument("--since", help="Only traces created at or after this ISO timestamp")
    parser.add_argument("--json", action="store_true", help="Emit a JSON array")
    args = parser.parse_args(argv)

    if args.limit < 1:
        print("usage trace limit must be positive", file=sys.stderr)
        return 2
    since: datetime | None = None
    if args.since:
        try:
            since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        except ValueError:
            print("usage trace since timestamp is invalid", file=sys.stderr)
            return 2
        if since.tzinfo is None:
            print("usage trace since timestamp must include a timezone", file=sys.stderr)
            return 2

    database = Database(settings.database_url)
    try:
        traces = asyncio.run(_query(database, limit=args.limit, since=since))
    except Exception as exc:
        print(f"usage trace query rejected: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps([_render(trace) for trace in traces], ensure_ascii=False, indent=2))
    else:
        _print_text(traces)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
