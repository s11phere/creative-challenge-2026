"""Product-level autonomous Assistant Loop over trusted Skill Tool adapters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from importlib.resources import files
from typing import Protocol
from uuid import UUID, uuid4

from agent_runtime import (
    AgentLoopExecutor,
    AgentToolRegistry,
    JSONValue,
    LLMDecision,
    NodeExecutionError,
    ToolRef,
)
from agent_runtime.skills import PinnedSkill
from domain.agent_loop import AgentLoopState, AgentLoopTask
from domain.agent_runtime import (
    AgentRun,
    AgentRunContext,
    RunBudget,
    RunErrorCategory,
    RunStatus,
    RuntimeStateStore,
    ToolPermission,
)
from domain.agent_sse import AgentRunEventStore
from domain.assistant_sse import AssistantEventStore, AssistantEventType
from domain.conversation_run import (
    Clarification,
    ClarificationKind,
    ConversationRun,
    ConversationRunKind,
    ConversationRunRepository,
    ConversationRunStatus,
    ConversationRunUsage,
)
from domain.grounded_qa import QAStatus
from domain.qa_persistence import MessageRecord, MessageRole, QARunRecord
from model_gateway import ModelGateway

from .context import ConversationContextService
from .finalization import ConversationFinalizer, FinalizationInput, grounded_material
from .metrics import AssistantMetrics

_CONTRACT_ROOT = files("application.assistant").joinpath("contracts")
_BASE_PROMPT_V5 = _CONTRACT_ROOT.joinpath("base-system-prompt-v5.txt").read_text(encoding="utf-8")

type QARunReader = Callable[[UUID], Awaitable[QARunRecord | None]]
type DecisionPolicy = Callable[[AgentRun, AgentLoopState, LLMDecision], LLMDecision]


class AssistantLoopMessageReader(Protocol):
    async def get_message(self, message_id: UUID) -> MessageRecord | None: ...


@dataclass(frozen=True)
class AssistantSkillContext:
    """Trusted, version-pinned Skill instructions available to this outer Loop."""

    name: str
    version: str
    description: str
    instructions: str

    def __post_init__(self) -> None:
        if not all((self.name, self.version, self.description, self.instructions.strip())):
            raise ValueError("Assistant Skill context is incomplete")


class AssistantConversationLoopFinalizer:
    """Publish one direct response or the already-published grounded QA message."""

    def __init__(
        self,
        *,
        runs: ConversationRunRepository,
        qa_results: QARunReader,
        model_identity: str,
        conversation_finalizer: ConversationFinalizer | None = None,
    ) -> None:
        self._runs = runs
        self._qa_results = qa_results
        self._conversation_finalizer = conversation_finalizer
        self._model_identity = model_identity

    async def finalize(
        self,
        *,
        run: AgentRun,
        task: AgentLoopTask,
        decision: LLMDecision,
        state: AgentLoopState,
        input_data: Mapping[str, JSONValue],
    ) -> dict[str, JSONValue]:
        del task
        parent = await self._runs.get_conversation_run(run.context.run_id)
        if parent is None:
            raise NodeExecutionError(
                "RUN_ASSISTANT_PARENT_MISSING",
                RunErrorCategory.INTERNAL,
                "Assistant parent Run is unavailable.",
            )
        if parent.status in {
            ConversationRunStatus.COMPLETED,
            ConversationRunStatus.REFUSED,
            ConversationRunStatus.FAILED,
            ConversationRunStatus.CANCELLED,
            ConversationRunStatus.TIMED_OUT,
        }:
            return {"status": parent.status.value, "publication": "existing"}
        usage = _combined_usage(parent, run)
        if _grounded_finalization_ready(state):
            qa_run = await self._qa_results(run.context.run_id)
            if (
                qa_run is None
                or qa_run.status not in {QAStatus.COMPLETED, QAStatus.REFUSED}
                or qa_run.answer_message_id is None
            ):
                raise NodeExecutionError(
                    "RUN_KNOWLEDGE_FINALIZATION_REQUIRED",
                    RunErrorCategory.SCHEMA,
                    "Grounded QA finalization is unavailable.",
                )
            question = input_data.get("question")
            fallback = _qa_result_text(qa_run)
            if not isinstance(question, str) or fallback is None:
                raise NodeExecutionError(
                    "RUN_KNOWLEDGE_FINALIZATION_REQUIRED",
                    RunErrorCategory.SCHEMA,
                    "Grounded QA finalization material is unavailable.",
                )
            if self._conversation_finalizer is None:
                raise NodeExecutionError(
                    "RUN_KNOWLEDGE_FINALIZATION_REQUIRED",
                    RunErrorCategory.INTERNAL,
                    "Grounded QA final answer writer is unavailable.",
                )
            published = await self._conversation_finalizer.execute(
                replace(parent, usage=usage),
                input=FinalizationInput(
                    question=question,
                    skill_result=grounded_material(qa_run, fallback),
                    fallback_content=fallback,
                    refused=qa_run.status is QAStatus.REFUSED,
                ),
            )
            return {"status": published.status.value, "publication": "grounded_qa_finalized"}
        if decision.action.value == "clarify":
            clarified = await self._runs.publish_clarification(
                run_id=parent.run_id,
                clarification=Clarification(
                    clarification_id=f"clarify:{parent.run_id.hex}",
                    kind=ClarificationKind.INPUT_REQUIRED,
                    message="Please provide the missing detail needed to continue.",
                ),
                usage=usage,
                model_identity=self._model_identity,
            )
            return {"status": clarified.status.value, "publication": "clarification"}
        refused = decision.action.value == "refuse"
        final_response = decision.final_response or (
            "I cannot help with that request." if refused else None
        )
        if not final_response:
            raise NodeExecutionError(
                "RUN_ASSISTANT_FINAL_RESPONSE_MISSING",
                RunErrorCategory.SCHEMA,
                "Assistant terminal response is missing.",
            )
        published = await self._runs.publish_direct_message(
            run_id=parent.run_id,
            message=MessageRecord(
                message_id=uuid4(),
                conversation_id=parent.conversation_id,
                space_id=parent.space_id,
                role=MessageRole.ASSISTANT,
                content=final_response,
                run_id=parent.run_id,
            ),
            usage=usage,
            model_identity=self._model_identity,
            refused=refused,
        )
        return {
            "status": published.status.value,
            "publication": "refusal" if refused else "direct",
        }


class AutonomousAssistantLoopService:
    """Run a direct conversation as one model-directed Skill/Tool loop."""

    def __init__(
        self,
        *,
        runs: ConversationRunRepository,
        messages: AssistantLoopMessageReader,
        gateway: ModelGateway,
        events: AssistantEventStore,
        agent_events: AgentRunEventStore,
        runtime_state: RuntimeStateStore,
        pin: PinnedSkill,
        budget: RunBudget,
        tool_registry: AgentToolRegistry,
        allowed_tools: tuple[ToolRef, ...],
        qa_results: QARunReader,
        skill_contexts: tuple[AssistantSkillContext, ...],
        context: ConversationContextService | None = None,
        metrics: AssistantMetrics | None = None,
        conversation_finalizer: ConversationFinalizer | None = None,
        decision_policy: DecisionPolicy | None = None,
    ) -> None:
        self._runs = runs
        self._messages = messages
        self._gateway = gateway
        self._events = events
        self._agent_events = agent_events
        self._runtime_state = runtime_state
        self._pin = pin
        self._budget = budget
        self._tool_registry = tool_registry
        self._allowed_tools = allowed_tools
        self._qa_results = qa_results
        self._conversation_finalizer = conversation_finalizer or ConversationFinalizer(
            runs=runs, gateway=gateway
        )
        self._skill_contexts = skill_contexts
        self._context = context
        self._metrics = metrics
        self._decision_policy = decision_policy

    async def execute(self, run_id: UUID, *, trace_id: str) -> ConversationRun | None:
        parent = await self._runs.get_conversation_run(run_id)
        if parent is None:
            return None
        if parent.run_kind is not ConversationRunKind.ASSISTANT_TURN:
            raise ValueError("RUN_AGENT_DECISION_INVALID")
        if parent.status in {
            ConversationRunStatus.COMPLETED,
            ConversationRunStatus.REFUSED,
            ConversationRunStatus.FAILED,
            ConversationRunStatus.CANCELLED,
            ConversationRunStatus.TIMED_OUT,
        }:
            await self._emit_terminal(parent)
            return parent
        if parent.cancellation_requested:
            cancelled = await self._runs.cancel_conversation_run(run_id)
            await self._emit_terminal(cancelled)
            return cancelled
        user_message = await self._messages.get_message(parent.user_message_id)
        if (
            user_message is None
            or user_message.role is not MessageRole.USER
            or user_message.conversation_id != parent.conversation_id
            or user_message.space_id != parent.space_id
        ):
            return await self._fail(run_id, "RUN_AGENT_DECISION_INVALID")
        snapshot = await self._context.snapshot(parent) if self._context is not None else None
        input_data = {
            "question": user_message.content,
            "conversation": snapshot.standalone_request()
            if snapshot is not None
            else user_message.content,
        }
        await self._events.append(
            run_id,
            AssistantEventType.ROUTING,
            {"status": ConversationRunStatus.RUNNING.value, "action": "agent_loop"},
        )
        finalizer = AssistantConversationLoopFinalizer(
            runs=self._runs,
            qa_results=self._qa_results,
            conversation_finalizer=self._conversation_finalizer,
            model_identity=self._gateway.status.provider.value,
        )
        executor = AgentLoopExecutor(
            tool_registry=self._tool_registry,
            allowed_tools=self._allowed_tools,
            system_prompt=self._system_prompt(),
            model_gateway=self._gateway,
            state_store=self._runtime_state,
            event_store=self._agent_events,
            reasoning_profile=parent.reasoning_profile,
            finalizer=finalizer,
            cancellation_check=self._cancel_requested,
            decision_policy=self._decision_policy,
        )
        persisted = await self._runtime_state.get_run(run_id)
        checkpoint = await self._runtime_state.get_latest(run_id)
        if (
            persisted is not None
            and checkpoint is not None
            and persisted.status
            not in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.TIMED_OUT}
        ):
            result = await executor.resume(
                persisted,
                self._pin,
                checkpoint,
                input_data,
                caller_id=parent.caller_id,
                space_id=parent.space_id,
            )
        else:
            runtime = AgentRun(
                context=AgentRunContext(
                    run_id=parent.run_id,
                    space_id=parent.space_id,
                    skill_name=self._pin.name,
                    skill_version=self._pin.version,
                    skill_content_sha256=self._pin.content_sha256,
                    trace_id=trace_id,
                    caller_id=parent.caller_id,
                    granted_permissions=frozenset(
                        {ToolPermission.READ_KNOWLEDGE, ToolPermission.MODEL}
                    ),
                ),
                budget=self._budget,
            )
            result = await executor.execute(
                runtime,
                self._pin,
                input_data,
                goal=user_message.content,
            )
        if result.error is not None:
            failed = await self._fail(run_id, result.error.code)
            return failed
        completed = await self._runs.get_conversation_run(run_id)
        if completed is None:
            return await self._fail(run_id, "RUN_ASSISTANT_PARENT_MISSING")
        if self._metrics is not None:
            self._metrics.record_usage(
                run_kind=completed.run_kind.value,
                input_tokens=result.run.usage.input_tokens,
                output_tokens=result.run.usage.output_tokens,
                latency_ms=0.0,
            )
        await self._emit_terminal(completed)
        return completed

    async def _cancel_requested(self, runtime: AgentRun) -> bool:
        parent = await self._runs.get_conversation_run(runtime.context.run_id)
        return parent is None or parent.cancellation_requested

    def _system_prompt(self) -> str:
        sections = [_BASE_PROMPT_V5]
        for skill in self._skill_contexts:
            sections.append(
                f"\n<active_skill name={skill.name!r} version={skill.version!r}>\n"
                f"{skill.description}\n{skill.instructions.strip()}\n</active_skill>"
            )
        return "\n".join(sections)

    async def _fail(self, run_id: UUID, error_code: str) -> ConversationRun:
        failed = await self._runs.fail_conversation_run(run_id, error_code=error_code)
        await self._emit_terminal(failed)
        return failed

    async def _emit_terminal(self, run: ConversationRun) -> None:
        if run.status in {ConversationRunStatus.COMPLETED, ConversationRunStatus.REFUSED}:
            await self._events.append(
                run.run_id,
                AssistantEventType.COMPLETED,
                {"status": run.status.value, "action": "agent_loop"},
            )
        elif run.status is ConversationRunStatus.FAILED:
            await self._events.append(
                run.run_id,
                AssistantEventType.FAILED,
                {"status": run.status.value, "error_code": run.error_code},
            )
        elif run.status is ConversationRunStatus.CANCELLED:
            await self._events.append(
                run.run_id,
                AssistantEventType.CANCELLED,
                {"status": run.status.value},
            )


def _grounded_finalization_ready(state: AgentLoopState) -> bool:
    return any(
        observation.tool_name == "finalize_answer"
        and isinstance(observation.model_output, dict)
        and observation.model_output.get("ready") is True
        and observation.model_output.get("publication") == "grounded_qa"
        for observation in state.observations
    )


def _qa_result_text(qa_run: QARunRecord) -> str | None:
    result = qa_run.result
    if result is None:
        return None
    if result.answer is not None:
        return result.answer.text
    if result.refusal is not None:
        return result.refusal.message
    return None


def _combined_usage(parent: ConversationRun, runtime: AgentRun) -> ConversationRunUsage:
    return ConversationRunUsage(
        input_tokens=parent.usage.input_tokens + runtime.usage.input_tokens,
        output_tokens=parent.usage.output_tokens + runtime.usage.output_tokens,
        model_latency_ms=parent.usage.model_latency_ms,
    )


__all__ = [
    "AssistantConversationLoopFinalizer",
    "AssistantSkillContext",
    "AutonomousAssistantLoopService",
]
