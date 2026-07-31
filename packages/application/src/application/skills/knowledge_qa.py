"""Provisional knowledge_qa adapter over the single Grounded QA Application port."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid5

from agent_runtime import (
    JSONValue,
    NodeExecutionContext,
    NodeExecutionError,
    NodeHandler,
    NodeOutcome,
    NodeResult,
)
from domain.agent_runtime import BudgetUsage, RunErrorCategory
from domain.grounded_qa import MAX_QUESTION_CHARS, QAOutcome, QAStatus, QuestionInput
from domain.qa_persistence import ConversationRecord, QARunRecord, QARunVersions

from application.qa.service import GroundedQAApplicationPort, GroundedQAExecutionProfile

_CONVERSATION_NAMESPACE = UUID("f941116d-dbc0-42f4-a43b-0a041ec50da4")


@dataclass(frozen=True)
class KnowledgeQASkillConfig:
    profile: GroundedQAExecutionProfile
    versions: QARunVersions
    execute_existing_run: bool = False


class KnowledgeQASkillAdapter:
    """Map Runtime context to Grounded QA without owning retrieval or citation logic."""

    def __init__(self, *, qa: GroundedQAApplicationPort, config: KnowledgeQASkillConfig) -> None:
        if (
            config.profile.planning.profile_id != config.versions.profile_version
            or config.profile.retrieval.profile_version != config.versions.retrieval_profile_version
        ):
            raise ValueError("Grounded QA Skill profile versions are inconsistent")
        self._qa = qa
        self._config = config

    def handlers(self) -> dict[str, NodeHandler]:
        return {
            "knowledge_qa_plan": self.plan,
            "knowledge_qa_delegate": self.delegate,
            "knowledge_qa_verify": self.verify,
        }

    async def plan(self, context: NodeExecutionContext) -> NodeResult:
        _parse_input(context.input)
        return NodeResult()

    async def delegate(self, context: NodeExecutionContext) -> NodeResult:
        skill_input = _parse_input(context.input)
        if context.pin.name != "knowledge_qa":
            raise NodeExecutionError(
                "SKILL_IDENTITY_MISMATCH",
                RunErrorCategory.MANIFEST,
                "Grounded QA adapter requires the knowledge_qa Skill.",
            )
        if context.pin.version != self._config.versions.skill_version:
            raise NodeExecutionError(
                "SKILL_VERSION_MISMATCH",
                RunErrorCategory.MANIFEST,
                "Grounded QA versions do not match the fixed Skill.",
            )
        if self._config.execute_existing_run:
            completed = await self._qa.execute(
                context.run.context.run_id, profile=self._config.profile
            )
        else:
            conversation_id = skill_input.conversation_id or uuid5(
                _CONVERSATION_NAMESPACE, str(context.run.context.run_id)
            )
            if skill_input.conversation_id is None:
                await self._qa.create_conversation(
                    ConversationRecord(
                        conversation_id=conversation_id,
                        space_id=context.run.context.space_id,
                        owner_id=context.run.context.caller_id,
                    )
                )
            submitted = await self._qa.submit(
                QuestionInput(
                    question=skill_input.question,
                    space_id=context.run.context.space_id,
                    caller_id=context.run.context.caller_id,
                    conversation_id=conversation_id,
                    idempotency_key=str(context.run.context.run_id),
                ),
                versions=self._config.versions,
            )
            completed = await self._qa.execute(submitted.run_id, profile=self._config.profile)
        if completed.status not in {QAStatus.COMPLETED, QAStatus.REFUSED}:
            raise _qa_failure(completed)
        usage = (
            BudgetUsage()
            if self._config.execute_existing_run
            else BudgetUsage(
                input_tokens=completed.usage.input_tokens,
                output_tokens=completed.usage.output_tokens,
            )
        )
        return NodeResult(state_updates={"qa_result": _project_run(completed)}, usage=usage)

    async def verify(self, context: NodeExecutionContext) -> NodeResult:
        output = context.state.get("qa_result")
        if not isinstance(output, dict):
            raise NodeExecutionError(
                "RUN_WORKFLOW_INVALID",
                RunErrorCategory.SCHEMA,
                "Grounded QA result is unavailable for verification.",
            )
        outcome = NodeOutcome.REFUSE if output.get("status") == "refused" else NodeOutcome.COMPLETE
        return NodeResult(outcome=outcome, output=output)


@dataclass(frozen=True)
class _SkillInput:
    question: str
    conversation_id: UUID | None


def _parse_input(value: object) -> _SkillInput:
    if not isinstance(value, dict) or set(value) - {"question", "conversation_id"}:
        raise NodeExecutionError(
            "SKILL_INPUT_INVALID",
            RunErrorCategory.INPUT,
            "knowledge_qa input does not match its fixed schema.",
        )
    question = value.get("question")
    conversation = value.get("conversation_id")
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION_CHARS:
        raise NodeExecutionError(
            "SKILL_INPUT_INVALID",
            RunErrorCategory.INPUT,
            "Question must be a non-empty bounded string.",
        )
    try:
        conversation_id = UUID(conversation) if isinstance(conversation, str) else None
    except ValueError as exc:
        raise NodeExecutionError(
            "SKILL_INPUT_INVALID",
            RunErrorCategory.INPUT,
            "Conversation identity is invalid.",
        ) from exc
    if conversation is not None and conversation_id is None:
        raise NodeExecutionError(
            "SKILL_INPUT_INVALID",
            RunErrorCategory.INPUT,
            "Conversation identity must be a UUID string.",
        )
    return _SkillInput(question=question, conversation_id=conversation_id)


def _qa_failure(run: QARunRecord) -> NodeExecutionError:
    code = run.error_code or "QA_FAILED"
    if run.status is QAStatus.CANCELLED:
        return NodeExecutionError(
            "RUN_CANCELLED", RunErrorCategory.CANCELLATION, "Grounded QA was cancelled."
        )
    if run.status is QAStatus.TIMED_OUT:
        return NodeExecutionError(
            "DEPENDENCY_QA_TIMEOUT",
            RunErrorCategory.DEPENDENCY,
            "Grounded QA timed out.",
            retryable=True,
            timed_out=True,
        )
    if code in {"QA_INVALID_INPUT", "QA_STRUCTURED_RESPONSE_INVALID", "QA_CITATION_INVALID"}:
        return NodeExecutionError(
            "SKILL_QA_RESULT_INVALID",
            RunErrorCategory.SCHEMA,
            "Grounded QA rejected the fixed request or result contract.",
        )
    if code in {"QA_SPACE_DENIED", "QA_POLICY_DENIED"}:
        return NodeExecutionError(
            "AUTH_QA_DENIED",
            RunErrorCategory.PERMISSION,
            "Grounded QA authorization or policy denied the request.",
        )
    retryable = code in {"QA_RETRIEVAL_FAILED", "QA_MODEL_FAILED", "QA_STORAGE_FAILED"}
    return NodeExecutionError(
        f"DEPENDENCY_{code.removeprefix('QA_')}",
        RunErrorCategory.DEPENDENCY,
        "Grounded QA dependency failed.",
        retryable=retryable,
    )


def _project_run(run: QARunRecord) -> dict[str, JSONValue]:
    result = run.result
    if result is None:
        raise NodeExecutionError(
            "RUN_WORKFLOW_INVALID",
            RunErrorCategory.SCHEMA,
            "Grounded QA terminal result is missing.",
        )
    payload: dict[str, JSONValue] = {
        "schema_version": "knowledge-qa-skill-output-v1",
        "status": run.status.value,
        "run_id": str(run.run_id),
        "conversation_id": str(run.conversation_id),
        "versions": {key: value for key, value in run.versions.__dict__.items()},
        "usage": {
            "input_tokens": run.usage.input_tokens,
            "output_tokens": run.usage.output_tokens,
            "model_calls": run.usage.model_calls,
        },
    }
    if result.outcome is QAOutcome.ANSWER and result.answer is not None:
        payload["result"] = {
            "type": "answer",
            "text": result.answer.text,
            "claims": [
                {
                    "claim_id": claim.claim_id,
                    "text": claim.text,
                    "evidence_ids": [str(value) for value in claim.evidence_ids],
                }
                for claim in result.answer.claims
            ],
            "citations": [
                {
                    "evidence_id": str(citation.evidence_id),
                    "source_id": str(citation.source_id),
                    "document_id": str(citation.document_id),
                    "version_id": str(citation.version_id),
                    "chunk_id": str(citation.chunk_id),
                    "locator": {
                        "kind": citation.locator.kind.value,
                        "start": citation.locator.start,
                        "end": citation.locator.end,
                    },
                    "excerpt_sha256": citation.excerpt_sha256,
                }
                for citation in result.answer.citations
            ],
            "limitations": list(result.answer.limitations),
        }
    elif result.outcome is QAOutcome.REFUSE and result.refusal is not None:
        payload["result"] = {
            "type": "refusal",
            "code": result.refusal.code.value,
            "reason": result.refusal.reason.value,
            "message": result.refusal.message,
        }
    elif result.outcome is QAOutcome.CONFLICT and result.conflict is not None:
        payload["result"] = {
            "type": "conflict",
            "evidence_ids": [str(value) for value in result.conflict.evidence_ids],
            "message": result.conflict.message,
        }
    else:
        raise NodeExecutionError(
            "RUN_WORKFLOW_INVALID",
            RunErrorCategory.SCHEMA,
            "Grounded QA result cannot be projected.",
        )
    return payload


__all__ = ["KnowledgeQASkillAdapter", "KnowledgeQASkillConfig"]
