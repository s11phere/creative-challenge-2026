"""Product-level autonomous Assistant Loop over trusted Skill Tool adapters."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from importlib.resources import files
from typing import Protocol, cast
from uuid import UUID, uuid4

from agent_runtime import (
    AgentToolRegistry,
    JSONValue,
    NativeServerToolCoordinator,
    NativeSkillCatalog,
    NativeToolUseAgentLoopExecutor,
    NodeExecutionError,
    ToolRef,
)
from agent_runtime.native_tool_use import AgentLoopDebugTrace
from agent_runtime.skills import PinnedSkill
from domain.agent_runtime import (
    AgentRun,
    AgentRunContext,
    ApprovalPort,
    RunBudget,
    RunErrorCategory,
    RunStatus,
    RuntimeStateStore,
    ToolPermission,
)
from domain.agent_sse import AgentRunEventStore
from domain.assistant_sse import AssistantEventStore, AssistantEventType
from domain.conversation_run import (
    ConversationRun,
    ConversationRunKind,
    ConversationRunRepository,
    ConversationRunStatus,
    ConversationRunUsage,
)
from domain.grounded_qa import QAStatus
from domain.qa_persistence import MessageRecord, MessageRole, QARunRecord
from model_gateway import CapabilityAlias, ModelGateway

from .context import ConversationContextService
from .finalization import ConversationFinalizer, FinalizationInput, grounded_material
from .metrics import AssistantMetrics

_CONTRACT_ROOT = files("application.assistant").joinpath("contracts")
_BASE_PROMPT_V8 = _CONTRACT_ROOT.joinpath("base-system-prompt-v8.txt").read_text(encoding="utf-8")

type QARunReader = Callable[[UUID], Awaitable[QARunRecord | None]]


class AssistantLoopMessageReader(Protocol):
    async def get_message(self, message_id: UUID) -> MessageRecord | None: ...


class NativeAssistantFinalizer:
    """Publish the single direct model response from a native Tool-use Run."""

    def __init__(
        self,
        *,
        runs: ConversationRunRepository,
        model_identity: str,
    ) -> None:
        self._runs = runs
        self._model_identity = model_identity

    async def finalize(
        self,
        *,
        run: AgentRun,
        goal: str,
        terminal_text: str,
        publication_id: str,
        input_data: Mapping[str, JSONValue],
    ) -> dict[str, JSONValue]:
        del goal, publication_id, input_data
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
        published = await self._runs.publish_direct_message(
            run_id=parent.run_id,
            message=MessageRecord(
                message_id=uuid4(),
                conversation_id=parent.conversation_id,
                space_id=parent.space_id,
                role=MessageRole.ASSISTANT,
                content=terminal_text,
                run_id=parent.run_id,
            ),
            usage=_combined_usage(parent, run),
            model_identity=self._model_identity,
        )
        return {"status": published.status.value, "publication": "direct"}


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
        qa_results: QARunReader,
        context: ConversationContextService | None = None,
        metrics: AssistantMetrics | None = None,
        conversation_finalizer: ConversationFinalizer | None = None,
        native_skill_catalog: NativeSkillCatalog,
        native_server_tools: NativeServerToolCoordinator,
        native_tool_registry: AgentToolRegistry,
        native_allowed_tools: tuple[ToolRef, ...],
        native_base_tools: tuple[ToolRef, ...] = (),
        native_system_prompt: str | None = None,
        prompt_caching_allowed: Callable[[AgentRunContext], bool] | None = None,
        workspace_context: Mapping[str, JSONValue] | None = None,
        additional_permissions: frozenset[ToolPermission] = frozenset(),
        approval_port: ApprovalPort | None = None,
        debug_trace: AgentLoopDebugTrace | None = None,
    ) -> None:
        self._runs = runs
        self._messages = messages
        self._gateway = gateway
        self._events = events
        self._agent_events = agent_events
        self._runtime_state = runtime_state
        self._pin = pin
        self._budget = budget
        self._qa_results = qa_results
        self._conversation_finalizer = conversation_finalizer or ConversationFinalizer(
            runs=runs, gateway=gateway
        )
        self._native_skill_catalog = native_skill_catalog
        self._native_server_tools = native_server_tools
        self._native_tool_registry = native_tool_registry
        self._native_allowed_tools = native_allowed_tools
        self._native_base_tools = native_base_tools
        self._native_system_prompt = native_system_prompt or _BASE_PROMPT_V8
        self._prompt_caching_allowed = prompt_caching_allowed
        self._context = context
        self._metrics = metrics
        self._workspace_context = dict(workspace_context or {})
        self._additional_permissions = additional_permissions
        self._approval_port = approval_port
        self._debug_trace = debug_trace

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
        input_data: dict[str, JSONValue] = {
            "question": user_message.content,
            "conversation": cast(
                JSONValue,
                snapshot.decision_request() if snapshot is not None else user_message.content,
            ),
            "workspace": cast(JSONValue, self._workspace_context),
        }
        await self._events.append(
            run_id,
            AssistantEventType.ROUTING,
            {"status": ConversationRunStatus.RUNNING.value, "action": "agent_loop"},
        )
        if not self._gateway.status.supports_native_tool_use(CapabilityAlias.FAST_CHAT):
            return await self._fail(run_id, "RUN_NATIVE_TOOL_USE_UNSUPPORTED")
        return await self._execute_native(
            parent=parent,
            user_message=user_message,
            input_data=input_data,
            trace_id=trace_id,
        )

    async def _execute_native(
        self,
        *,
        parent: ConversationRun,
        user_message: MessageRecord,
        input_data: Mapping[str, JSONValue],
        trace_id: str,
    ) -> ConversationRun:
        executor = NativeToolUseAgentLoopExecutor(
            tool_registry=self._native_tool_registry,
            allowed_tools=self._native_allowed_tools,
            base_tools=self._native_base_tools,
            system_prompt=self._native_system_prompt,
            model_gateway=self._gateway,
            state_store=self._runtime_state,
            event_store=self._agent_events,
            reasoning_profile=parent.reasoning_profile,
            finalizer=NativeAssistantFinalizer(
                runs=self._runs,
                model_identity=self._gateway.status.provider.value,
            ),
            cancellation_check=self._cancel_requested,
            skill_catalog=self._native_skill_catalog,
            server_tools=self._native_server_tools,
            debug_trace=self._debug_trace,
            approval_request=(
                self._approval_port.request if self._approval_port is not None else None
            ),
            approval_port=self._approval_port,
            prompt_caching_allowed=self._prompt_caching_allowed,
        )
        persisted = await self._runtime_state.get_run(parent.run_id)
        checkpoint = await self._runtime_state.get_latest(parent.run_id)
        if (
            persisted is not None
            and checkpoint is not None
            and persisted.status
            not in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.TIMED_OUT}
        ):
            approval_id = None
            if isinstance(checkpoint.state, Mapping):
                candidate_approval_id = checkpoint.state.get("approval_id")
                if (
                    isinstance(candidate_approval_id, str)
                    and self._approval_port is not None
                    and await self._approval_port.is_approved(
                        candidate_approval_id, persisted.context
                    )
                ):
                    approval_id = candidate_approval_id
            result = await executor.resume(
                persisted,
                self._pin,
                checkpoint,
                input_data,
                caller_id=parent.caller_id,
                space_id=parent.space_id,
                approval_id=approval_id,
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
                    ).union(self._additional_permissions),
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
            return await self._fail(parent.run_id, result.error.code)
        if result.waiting_approval:
            return await self._runs.wait_for_approval(parent.run_id)
        if result.state.terminal_output is not None:
            qa_run = await self._qa_results(parent.run_id)
            if (
                qa_run is None
                or qa_run.status not in {QAStatus.COMPLETED, QAStatus.REFUSED}
                or qa_run.answer_message_id is None
            ):
                return await self._fail(parent.run_id, "RUN_KNOWLEDGE_FINALIZATION_REQUIRED")
            fallback = _qa_fallback_text(qa_run)
            await self._conversation_finalizer.execute(
                replace(parent, usage=_combined_usage(parent, result.run)),
                input=FinalizationInput(
                    question=user_message.content,
                    skill_result=grounded_material(qa_run, fallback),
                    fallback_content=fallback,
                    refused=qa_run.status is QAStatus.REFUSED,
                ),
            )
        completed = await self._runs.get_conversation_run(parent.run_id)
        if completed is None:
            return await self._fail(parent.run_id, "RUN_ASSISTANT_PARENT_MISSING")
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


def _qa_fallback_text(qa_run: QARunRecord) -> str:
    result = qa_run.result
    if result is None:
        return ""
    if result.answer is not None:
        return result.answer.text
    if result.refusal is not None:
        return result.refusal.message
    if result.conflict is not None:
        return result.conflict.message
    return ""


def _combined_usage(parent: ConversationRun, runtime: AgentRun) -> ConversationRunUsage:
    return ConversationRunUsage(
        input_tokens=parent.usage.input_tokens + runtime.usage.input_tokens,
        output_tokens=parent.usage.output_tokens + runtime.usage.output_tokens,
        model_latency_ms=parent.usage.model_latency_ms,
    )


__all__ = [
    "AutonomousAssistantLoopService",
]
