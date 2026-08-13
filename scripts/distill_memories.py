"""Distill cross-session long-term memory from summaries and usage patterns (on-demand).

Phase 5 turns conversation rolling summaries plus Phase 2 usage patterns into
durable ``memory_entries``. By default the distillation runs locally with a fresh
ModelGateway; ``--enqueue`` dispatches the same work to the Dramatiq Worker
instead (requires Redis). The distilled memory is not injected until the next
Assistant turn, so this is safe to run at any time.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

from application.memory import DistillResult, MemoryDistiller
from infrastructure.config import settings
from infrastructure.database import Database
from infrastructure.memory_entries import (
    GatewayTextEmbedder,
    PostgresMemoryDistillationSource,
    PostgresMemoryEntryRepository,
)


def _render(result: DistillResult) -> dict[str, int]:
    return {
        "candidates": result.candidates,
        "inserted": result.inserted,
        "updated": result.updated,
        "merged": result.merged,
        "persisted": result.persisted,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit a JSON object")
    parser.add_argument(
        "--enqueue",
        action="store_true",
        help="Dispatch distillation to the Dramatiq Worker instead of running locally",
    )
    args = parser.parse_args(argv)

    if args.enqueue:
        try:
            from worker.memory_tasks import enqueue_memory_distill

            message = enqueue_memory_distill()
        except Exception as exc:
            print(f"memory distill enqueue rejected: {exc}", file=sys.stderr)
            return 2
        print(f"memory_distill enqueued (message_id={message.message_id})")
        return 0

    async def _distill(database: Database) -> DistillResult:
        from worker.qa_tasks import _create_gateway

        gateway = _create_gateway()
        try:
            return await MemoryDistiller(
                source=PostgresMemoryDistillationSource(database),
                entries=PostgresMemoryEntryRepository(database),
                gateway=gateway,
                embedder=GatewayTextEmbedder(gateway),
            ).distill()
        finally:
            await gateway.aclose()
            await database.dispose()

    database = Database(settings.database_url)
    try:
        result = asyncio.run(_distill(database))
    except Exception as exc:
        print(f"memory distill rejected: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(_render(result), ensure_ascii=False, indent=2))
    else:
        print(
            f"distilled {result.candidates} candidates "
            f"(inserted={result.inserted}, updated={result.updated}, "
            f"merged={result.merged}, persisted={result.persisted})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
