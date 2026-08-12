"""Worker-side usage-trace capture and pattern distillation hooks."""

from __future__ import annotations

import logging
from uuid import UUID

from application.usage_traces import UsageTraceRecorder
from infrastructure.agent_events import PostgresAgentRunEventStore
from infrastructure.config import settings
from infrastructure.conversation_runs import PostgresConversationRunRepository
from infrastructure.database import Database
from infrastructure.qa_persistence import PostgresGroundedQARepository
from infrastructure.usage_traces import PostgresUsageTraceRepository
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)
database = Database(settings.database_url, poolclass=NullPool)


async def record_usage_trace(run_id: UUID) -> None:
    """Best-effort trace capture; failures are logged, never break the Run path."""
    try:
        recorder = UsageTraceRecorder(
            runs=PostgresConversationRunRepository(database),
            data=PostgresGroundedQARepository(database),
            qa=PostgresGroundedQARepository(database),
            agent_events=PostgresAgentRunEventStore(database),
            traces=PostgresUsageTraceRepository(database),
        )
        await recorder.record_run(run_id)
    except Exception:
        logger.exception("usage_trace_record_failed", extra={"run_id": str(run_id)})


__all__ = ["record_usage_trace"]
