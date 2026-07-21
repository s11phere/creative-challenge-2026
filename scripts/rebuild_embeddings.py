"""Plan or enqueue a controlled embedding rebuild for explicit Sources."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from uuid import UUID

from application.ingestion import EmbeddingRebuildService
from domain.embedding import EmbeddingIdentity
from infrastructure.config import settings
from infrastructure.database import Database
from infrastructure.repositories import (
    DocumentRepository,
    DocumentVersionRepository,
    IngestionTaskRepository,
    SourceRepository,
)
from infrastructure.telemetry_context import new_trace_id


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-id", action="append", required=True)
    parser.add_argument("--model-revision", required=True)
    parser.add_argument("--query-instruction-version", default="none-v1")
    parser.add_argument("--document-instruction-version", default="none-v1")
    parser.add_argument("--normalization", default="none")
    parser.add_argument("--precision", default="float32")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--confirm-version",
        help="Required with --apply; must equal the computed embedding version",
    )
    return parser


async def _execute(args: argparse.Namespace, identity: EmbeddingIdentity) -> dict[str, object]:
    source_ids = tuple(UUID(value) for value in args.source_id)
    database = Database(settings.database_url)
    pending_task_ids: list[UUID] = []
    source_summaries: list[dict[str, object]] = []
    try:
        async with database.session() as session:
            service = EmbeddingRebuildService(
                source_repo=SourceRepository(session),
                document_repo=DocumentRepository(session),
                version_repo=DocumentVersionRepository(session),
                task_repo=IngestionTaskRepository(session),
            )
            for source_id in source_ids:
                plan = await service.plan_source(source_id, identity)
                summary: dict[str, object] = {
                    "source_id": str(source_id),
                    "document_count": len(plan),
                    "current_version_ids": [str(item.current_version_id) for item in plan],
                }
                if args.apply:
                    prepared = await service.prepare_source(source_id, identity)
                    pending_task_ids.extend(task.id for task in prepared.tasks)
                    summary.update(
                        {
                            "candidate_version_ids": [
                                str(candidate.id) for candidate in prepared.candidates
                            ],
                            "task_ids": [str(task.id) for task in prepared.tasks],
                            "skipped_document_ids": [
                                str(document_id) for document_id in prepared.skipped_document_ids
                            ],
                        }
                    )
                source_summaries.append(summary)
            if args.apply:
                await session.commit()

        if args.apply:
            from worker.ingestion_tasks import enqueue_ingestion_task  # noqa: PLC0415

            for task_id in pending_task_ids:
                enqueue_ingestion_task(task_id=str(task_id), trace_id=new_trace_id())
        return {
            "mode": "apply" if args.apply else "plan",
            "embedding_version": identity.version,
            "sources": source_summaries,
        }
    finally:
        await database.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        identity = EmbeddingIdentity(
            model_revision=args.model_revision,
            query_instruction_version=args.query_instruction_version,
            document_instruction_version=args.document_instruction_version,
            normalization=args.normalization,
            precision=args.precision,
        )
        if args.apply and args.confirm_version != identity.version:
            print(
                f"confirmation mismatch: pass --confirm-version {identity.version}",
                file=sys.stderr,
            )
            return 2
        result = asyncio.run(_execute(args, identity))
    except (ValueError, OSError) as exc:
        print(f"embedding rebuild rejected: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
