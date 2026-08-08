"""Publish a completed Skill result as the user-facing Assistant message.

The grounded-QA Skills (knowledge_agent and the others) already produce a
citation-validated, user-facing answer; the previous one-shot LLM synthesis
re-ran a second answer generation (latency) while dropping the citations. The
finalizer now publishes the Skill result directly without an extra model call.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from domain.conversation_run import (
    ConversationRun,
    ConversationRunRepository,
    ConversationRunStatus,
    ConversationRunUsage,
)
from domain.qa_persistence import MessageRecord, MessageRole
from model_gateway import ModelGateway

_TERMINAL_STATUSES = frozenset(
    {
        ConversationRunStatus.COMPLETED,
        ConversationRunStatus.REFUSED,
        ConversationRunStatus.FAILED,
        ConversationRunStatus.CANCELLED,
        ConversationRunStatus.TIMED_OUT,
    }
)

_FALLBACK_CONTENT = "工具执行已完成，但没有生成可展示的最终回答，请重试。"


@dataclass(frozen=True)
class FinalizationInput:
    question: str
    skill_result: str


class ConversationFinalizer:
    """Publish exactly one final Assistant message for a Skill Run.

    The Skill result is passed through verbatim rather than re-synthesized by a
    second LLM call: grounded-QA results are already final answers, so re-writing
    them only added latency and stripped their citations.
    """

    def __init__(self, *, runs: ConversationRunRepository, gateway: ModelGateway) -> None:
        self._runs = runs
        self._gateway = gateway

    async def execute(self, run: ConversationRun, *, input: FinalizationInput) -> ConversationRun:
        if run.status in _TERMINAL_STATUSES:
            return run
        if run.cancellation_requested or run.status is ConversationRunStatus.CANCEL_REQUESTED:
            return await self._runs.cancel_conversation_run(run.run_id)
        content = (input.skill_result or "").strip()[:12_000]
        if not content:
            content = _FALLBACK_CONTENT
        return await self._runs.publish_direct_message(
            run_id=run.run_id,
            message=MessageRecord(
                message_id=uuid4(),
                conversation_id=run.conversation_id,
                space_id=run.space_id,
                role=MessageRole.ASSISTANT,
                content=content,
                run_id=run.run_id,
            ),
            usage=ConversationRunUsage(
                input_tokens=run.usage.input_tokens,
                output_tokens=run.usage.output_tokens,
                model_latency_ms=run.usage.model_latency_ms,
            ),
            model_identity=self._gateway.status.provider.value,
        )


__all__ = ["ConversationFinalizer", "FinalizationInput"]
