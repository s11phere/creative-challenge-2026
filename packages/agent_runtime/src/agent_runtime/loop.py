"""Recoverable generic Agent Loop execution over the existing Runtime ports."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from time import monotonic
from typing import Protocol, cast
from uuid import UUID

from domain.agent_loop import (
    AgentLoopCompletionCheck,
    AgentLoopNoProgressError,
    AgentLoopPhase,
    AgentLoopState,
    AgentLoopStopReason,
    AgentLoopTask,
    AgentLoopToolObservation,
)
from domain.agent_runtime import (
    AgentRun,
    BudgetExceededError,
    RecoveryRejectedError,
    RunCheckpoint,
    RunError,
    RunErrorCategory,
    RunEvent,
    RunStatus,
    RunStep,
    RuntimeStateStore,
    validate_recovery,
)
from domain.agent_sse import AgentRunEventStore, AgentRunEventType
from domain.reasoning import ReasoningProfile
from model_gateway import ModelGateway

from .checkpoints import build_checkpoint, checkpoint_state_sha256
from .executor import NodeExecutionContext, NodeExecutionError
from .llm_decision import (
    AgentToolRegistry,
    LLMDecision,
    LLMDecisionAction,
    LLMDecisionNode,
)
from .skills import PinnedSkill
from .tools import (
    JSONValue,
    ToolDefinition,
    ToolInvocation,
    ToolInvocationResult,
    ToolRef,
    ToolRegistryError,
    ToolRegistryErrorCode,
    tool_requires_durable_approval,
)

type CancellationCheck = Callable[[AgentRun], Awaitable[bool]]
type ClockMilliseconds = Callable[[], int]


class AgentLoopFinalizer(Protocol):
    async def finalize(
        self,
        *,
        run: AgentRun,
        task: AgentLoopTask,
        decision: LLMDecision,
        state: AgentLoopState,
        input_data: Mapping[str, JSONValue],
    ) -> JSONValue: ...


@dataclass(frozen=True)
class AgentLoopResult:
    run: AgentRun
    state: AgentLoopState
    output: JSONValue
    refused: bool = False
    clarified: bool = False
    waiting_approval: bool = False
    error: RunError | None = None


class _DefaultFinalizer:
    async def finalize(
        self,
        *,
        run: AgentRun,
        task: AgentLoopTask,
        decision: LLMDecision,
        state: AgentLoopState,
        input_data: Mapping[str, JSONValue],
    ) -> JSONValue:
        del run, task, state, input_data
        return {"action": decision.action.value, "reason": decision.reason or "completed"}


class AgentLoopExecutor:
    """Run a model/Tool loop with a server-side emergency ceiling and checkpoints."""

    def __init__(
        self,
        *,
        tool_registry: AgentToolRegistry,
        allowed_tools: tuple[ToolRef, ...],
        system_prompt: str,
        model_gateway: ModelGateway,
        state_store: RuntimeStateStore | None = None,
        event_store: AgentRunEventStore | None = None,
        reasoning_profile: ReasoningProfile | None = None,
        finalizer: AgentLoopFinalizer | None = None,
        emergency_ceiling: int = 32,
        max_tokens_per_decision: int = 512,
        cancellation_check: CancellationCheck | None = None,
        clock_ms: ClockMilliseconds | None = None,
    ) -> None:
        names = tuple(ref.name for ref in allowed_tools)
        if not allowed_tools or len(names) != len(set(names)):
            raise ValueError("Agent Loop Tools must have unique names")
        if not system_prompt.strip():
            raise ValueError("Agent Loop system prompt must not be blank")
        if emergency_ceiling < 1 or max_tokens_per_decision < 1:
            raise ValueError("Agent Loop limits must be positive")
        self._tool_registry = tool_registry
        self._allowed_tools = allowed_tools
        self._system_prompt = system_prompt
        self._model_gateway = model_gateway
        self._state_store = state_store
        self._event_store = event_store
        self._reasoning_profile = reasoning_profile or ReasoningProfile.unresolved()
        self._finalizer = finalizer or _DefaultFinalizer()
        self._emergency_ceiling = emergency_ceiling
        self._max_tokens_per_decision = max_tokens_per_decision
        self._cancellation_check = cancellation_check or _not_cancelled
        self._clock_ms = clock_ms or _monotonic_ms

    async def execute(
        self,
        run: AgentRun,
        pin: PinnedSkill,
        input_data: Mapping[str, JSONValue],
        *,
        goal: str,
        subquestions: tuple[str, ...] = (),
        approval_id: str | None = None,
        lease_id: str | None = None,
    ) -> AgentLoopResult:
        state = AgentLoopState.accepted(AgentLoopTask(goal, subquestions), lease_id=lease_id)
        return await self._execute_from(
            run,
            pin,
            input_data,
            state=state,
            approval_id=approval_id,
        )

    async def resume(
        self,
        run: AgentRun,
        pin: PinnedSkill,
        checkpoint: RunCheckpoint,
        input_data: Mapping[str, JSONValue],
        *,
        caller_id: str,
        space_id: UUID,
        approval_id: str | None = None,
        lease_id: str | None = None,
    ) -> AgentLoopResult:
        validate_recovery(run, checkpoint, caller_id=caller_id, space_id=space_id)
        state_value = cast(dict[str, object], deepcopy_json(dict(checkpoint.state)))
        if checkpoint.state_sha256 != checkpoint_state_sha256(
            cast(Mapping[str, JSONValue], state_value)
        ):
            raise RecoveryRejectedError("Agent Loop checkpoint state digest is invalid")
        state = AgentLoopState.from_checkpoint(state_value)
        if lease_id is not None and state.lease_id not in {None, lease_id}:
            raise RecoveryRejectedError("Agent Loop checkpoint lease does not match")
        if approval_id is not None and state.approval_id not in {None, approval_id}:
            raise RecoveryRejectedError("Agent Loop checkpoint approval does not match")
        return await self._execute_from(
            run,
            pin,
            input_data,
            state=state,
            approval_id=approval_id,
        )

    async def _execute_from(
        self,
        run: AgentRun,
        pin: PinnedSkill,
        input_data: Mapping[str, JSONValue],
        *,
        state: AgentLoopState,
        approval_id: str | None,
    ) -> AgentLoopResult:
        started_ms = self._clock_ms()
        base_elapsed_ms = run.usage.elapsed_ms
        definitions = tuple(self._tool_registry.get(ref) for ref in self._allowed_tools)
        by_name = {definition.name: definition for definition in definitions}
        for definition in definitions:
            if not definition.model_visible:
                raise NodeExecutionError(
                    code=ToolRegistryErrorCode.MODEL_OUTPUT_DENIED.value,
                    category=RunErrorCategory.PERMISSION,
                    message="Tool output is not approved for model visibility.",
                )

        history: list[JSONValue] = [
            {
                "tool_name": item.tool_name,
                "tool_version": item.tool_version,
                "output_summary": item.output_summary,
                "error_code": item.error_code,
            }
            for item in state.observations
        ]
        try:
            await self._emit(
                run,
                AgentRunEventType.ACCEPTED,
                {
                    "status": "accepted",
                    "reasoning_profile_schema_version": self._reasoning_profile.schema_version,
                    "requested_effort": self._reasoning_profile.requested_effort.value,
                    "effective_effort": self._reasoning_profile.effective_effort.value,
                    "provider": self._reasoning_profile.provider,
                    "model": self._reasoning_profile.model,
                    "mapping_version": self._reasoning_profile.mapping_version,
                    "mode": self._reasoning_profile.mode.value,
                    "downgrade_reason": self._reasoning_profile.downgrade_reason.value,
                    "continuation": "checkpoint",
                },
                event_key="accepted",
            )
            run, state = self._activate(run, state, approval_id)
            if state.phase is AgentLoopPhase.WAITING_APPROVAL:
                return AgentLoopResult(
                    run=run,
                    state=state,
                    output={"status": "waiting_approval"},
                    waiting_approval=True,
                )
            if state.phase is AgentLoopPhase.FINALIZING:
                return await self._resume_finalization(run, state, input_data)
            decision_node = LLMDecisionNode(
                allowed_tools=frozenset(ref.name for ref in self._allowed_tools),
                system_prompt=self._system_prompt,
                max_tokens=self._max_tokens_per_decision,
            )
            if state.phase is AgentLoopPhase.TOOL_REQUESTED and state.pending_tool_name:
                run, state, pending_result = await self._execute_pending_tool(
                    run, state, by_name, approval_id
                )
                history.append(
                    {
                        "tool_name": pending_result.record.tool_name,
                        "tool_version": pending_result.record.tool_version,
                        "output": pending_result.output,
                        "output_summary": pending_result.record.output_summary,
                    }
                )
                run = await self._persist(run, state)
                approval_id = None
            while True:
                run = self._account_elapsed(run, started_ms, base_elapsed_ms)
                if run.status == RunStatus.CANCEL_REQUESTED or await self._cancellation_check(run):
                    return await self._cancel(run, state)
                if state.iteration >= min(self._emergency_ceiling, run.budget.max_steps):
                    raise NodeExecutionError(
                        code="RUN_LLM_EMERGENCY_CEILING",
                        category=RunErrorCategory.BUDGET,
                        message="Agent Loop emergency ceiling was reached.",
                    )
                context = NodeExecutionContext(
                    run=run,
                    pin=pin,
                    input=input_data,
                    state={
                        "goal": state.task.goal,
                        "subquestions": list(state.task.subquestions),
                        "iteration": state.iteration,
                        "observations": history,
                    },
                    model_gateway=self._model_gateway,
                )
                decision, decision_usage = await decision_node.decide(context)
                run = run.consume(
                    steps=1,
                    input_tokens=decision_usage.input_tokens,
                    output_tokens=decision_usage.output_tokens,
                )
                state = state.begin_iteration(decision.as_json())
                await self._emit(
                    run,
                    AgentRunEventType.ITERATION_STARTED,
                    {
                        "status": "planning",
                        "iteration": state.iteration,
                        "tool_call_count": run.usage.tool_calls,
                        "observation_count": len(state.observations),
                    },
                    event_key=f"iteration:{state.iteration}:started",
                )
                if decision.action is not LLMDecisionAction.CALL_TOOL:
                    return await self._finalize_decision(run, state, input_data, decision)

                assert decision.tool_name is not None
                definition = by_name[decision.tool_name]
                invocation = ToolInvocation(
                    ref=definition.ref,
                    arguments=decision.arguments,
                    allowed_tools=frozenset(self._allowed_tools),
                    granted_permissions=run.context.granted_permissions,
                    resource_space_id=run.context.space_id,
                    idempotency_key=self._idempotency_key(run, state.iteration, decision),
                    approval_id=approval_id,
                )
                try:
                    state = state.request_tool(
                        name=definition.name,
                        version=definition.version,
                        arguments=cast(dict[str, object], decision.arguments),
                        idempotency_key=invocation.idempotency_key,
                        request_fingerprint=self._request_fingerprint(definition, decision),
                    )
                except AgentLoopNoProgressError as exc:
                    raise NodeExecutionError(
                        code="RUN_LLM_NO_PROGRESS",
                        category=RunErrorCategory.BUDGET,
                        message="Agent Loop repeated a Tool request without progress.",
                    ) from exc
                await self._emit(
                    run,
                    AgentRunEventType.TOOL_REQUESTED,
                    {
                        "status": "requested",
                        "iteration": state.iteration,
                        "tool_name": definition.name,
                        "tool_version": definition.version,
                        "input_summary": _summary_digest(decision.arguments),
                        "retry_count": 0,
                    },
                    event_key=f"iteration:{state.iteration}:tool_requested",
                )
                run = _move_to_executing(run)
                run = await self._persist(run, state)
                if tool_requires_durable_approval(definition.permissions) and approval_id is None:
                    state = state.wait_for_approval()
                    run = run.transition(RunEvent.WAIT_APPROVAL)
                    await self._emit(
                        run,
                        AgentRunEventType.APPROVAL_REQUIRED,
                        {
                            "status": "waiting_approval",
                            "iteration": state.iteration,
                            "tool_name": definition.name,
                            "tool_version": definition.version,
                            "input_summary": _summary_digest(decision.arguments),
                            "retry_count": 0,
                        },
                        event_key=f"iteration:{state.iteration}:approval_required",
                    )
                    run = await self._persist(run, state)
                    return AgentLoopResult(
                        run=run,
                        state=state,
                        output={"status": "waiting_approval"},
                        waiting_approval=True,
                    )
                state = state.start_tool()
                result = await self._invoke_with_retry(
                    run, invocation, definition, iteration=state.iteration
                )
                run = result.run
                observation = AgentLoopToolObservation(
                    iteration=state.iteration,
                    tool_name=definition.name,
                    tool_version=definition.version,
                    idempotency_key=invocation.idempotency_key,
                    input_summary=result.record.input_summary,
                    output_summary=result.record.output_summary,
                    error_code=result.record.error_code,
                    retry_count=result.record.retry_count,
                    duration_ms=result.record.duration_ms,
                )
                state = state.observe(observation)
                history.append(
                    {
                        "tool_name": definition.name,
                        "tool_version": definition.version,
                        "output": result.output,
                        "output_summary": result.record.output_summary,
                    }
                )
                run = await self._persist(run, state)
                approval_id = None
        except (NodeExecutionError, ToolRegistryError, BudgetExceededError) as exc:
            return await self._fail(run, state, exc)
        except TimeoutError:
            return await self._fail(
                run,
                state,
                NodeExecutionError(
                    code="RUN_TIMED_OUT",
                    category=RunErrorCategory.BUDGET,
                    message="Agent Loop total timeout was exceeded.",
                    timed_out=True,
                ),
            )
        except Exception:
            return await self._fail(
                run,
                state,
                NodeExecutionError(
                    code="RUN_AGENT_LOOP_FAILED",
                    category=RunErrorCategory.INTERNAL,
                    message="Agent Loop execution failed.",
                ),
            )

    async def _finalize_decision(
        self,
        run: AgentRun,
        state: AgentLoopState,
        input_data: Mapping[str, JSONValue],
        decision: LLMDecision,
    ) -> AgentLoopResult:
        stop_reason = {
            LLMDecisionAction.COMPLETE: AgentLoopStopReason.GOAL_COMPLETE,
            LLMDecisionAction.CLARIFY: AgentLoopStopReason.USER_CLARIFICATION,
            LLMDecisionAction.REFUSE: AgentLoopStopReason.POLICY_REFUSAL,
        }[decision.action]
        completion = AgentLoopCompletionCheck(
            goal_complete=decision.action is LLMDecisionAction.COMPLETE,
            evidence_sufficient=decision.action is LLMDecisionAction.COMPLETE,
            has_conflict=False,
        )
        state = state.begin_finalization(
            completion=completion,
            stop_reason=stop_reason,
            finalization_action=decision.action.value,
            finalizer_publication_id=_publication_id(run),
        )
        run = run.transition(RunEvent.FINALIZE)
        await self._emit(
            run,
            AgentRunEventType.FINALIZING,
            {
                "status": "finalizing",
                "iteration": state.iteration,
                "stop_reason": stop_reason.value,
                "goal_complete": completion.goal_complete,
                "evidence_sufficient": completion.evidence_sufficient,
                "has_conflict": completion.has_conflict,
                "publication_id": state.finalizer_publication_id or _publication_id(run),
            },
            event_key="finalizing",
        )
        run = await self._persist(run, state)
        return await self._publish_finalization(run, state, input_data, decision)

    async def _resume_finalization(
        self,
        run: AgentRun,
        state: AgentLoopState,
        input_data: Mapping[str, JSONValue],
    ) -> AgentLoopResult:
        if state.finalization_action is None or state.finalizer_publication_id is None:
            raise RecoveryRejectedError("Agent Loop finalization checkpoint is incomplete")
        decision = LLMDecision(
            action=LLMDecisionAction(state.finalization_action),
            reason="checkpointed finalization",
        )
        return await self._publish_finalization(run, state, input_data, decision)

    async def _publish_finalization(
        self,
        run: AgentRun,
        state: AgentLoopState,
        input_data: Mapping[str, JSONValue],
        decision: LLMDecision,
    ) -> AgentLoopResult:
        output = await self._finalizer.finalize(
            run=run,
            task=state.task,
            decision=decision,
            state=state,
            input_data=input_data,
        )
        state = state.publish(
            refused=decision.action is LLMDecisionAction.REFUSE,
            clarified=decision.action is LLMDecisionAction.CLARIFY,
        )
        run = run.transition(RunEvent.COMPLETE)
        run = await self._finalize_run(run)
        event_type = {
            LLMDecisionAction.COMPLETE: AgentRunEventType.COMPLETED,
            LLMDecisionAction.CLARIFY: AgentRunEventType.CLARIFYING,
            LLMDecisionAction.REFUSE: AgentRunEventType.REFUSED,
        }[decision.action]
        await self._emit(
            run,
            event_type,
            {
                "status": run.status.value,
                "stop_reason": state.stop_reason.value if state.stop_reason else "failed",
                "iteration": state.iteration,
                "publication_id": state.finalizer_publication_id or _publication_id(run),
            },
            event_key="terminal",
        )
        return AgentLoopResult(
            run=run,
            state=state,
            output=output,
            refused=decision.action is LLMDecisionAction.REFUSE,
            clarified=decision.action is LLMDecisionAction.CLARIFY,
        )

    async def _invoke_with_retry(
        self,
        run: AgentRun,
        invocation: ToolInvocation,
        definition: ToolDefinition,
        *,
        iteration: int,
    ) -> ToolInvocationResult:
        while True:
            await self._emit(
                run,
                AgentRunEventType.TOOL_STARTED,
                {
                    "status": "running",
                    "iteration": iteration,
                    "tool_name": definition.name,
                    "tool_version": definition.version,
                    "input_summary": _summary_digest(invocation.arguments),
                    "retry_count": invocation.retry_count,
                },
                event_key=f"iteration:{iteration}:tool_started:{invocation.retry_count}",
            )
            try:
                result = await self._tool_registry.invoke(run, invocation)
            except ToolRegistryError as exc:
                record = exc.record
                await self._emit(
                    run,
                    AgentRunEventType.TOOL_OUTPUT,
                    _tool_event_payload(
                        definition,
                        iteration=iteration,
                        status="failed",
                        input_summary=(
                            record.input_summary
                            if record is not None
                            else _summary_digest(invocation.arguments)
                        ),
                        output_summary=(
                            record.output_summary if record is not None else "sha256:unavailable"
                        ),
                        error_code=exc.code.value,
                        retry_count=(
                            record.retry_count if record is not None else invocation.retry_count
                        ),
                        duration_ms=record.duration_ms if record is not None else 0,
                    ),
                    event_key=f"iteration:{iteration}:tool_output:{invocation.retry_count}",
                )
                if exc.code is ToolRegistryErrorCode.APPROVAL_REQUIRED:
                    raise
                if not exc.retryable or invocation.retry_count >= definition.max_retries:
                    raise NodeExecutionError(
                        code=exc.code.value,
                        category=_tool_error_category(exc.code),
                        message=str(exc),
                        retryable=exc.retryable,
                    ) from exc
                invocation = replace(
                    invocation,
                    retry_count=invocation.retry_count + 1,
                    idempotency_key=(
                        f"{invocation.idempotency_key}:retry:{invocation.retry_count + 1}"
                    ),
                )
                continue
            await self._emit(
                result.run,
                AgentRunEventType.TOOL_OUTPUT,
                _tool_event_payload(
                    definition,
                    iteration=iteration,
                    status="succeeded",
                    input_summary=result.record.input_summary,
                    output_summary=result.record.output_summary,
                    error_code=result.record.error_code,
                    retry_count=result.record.retry_count,
                    duration_ms=result.record.duration_ms,
                ),
                event_key=f"iteration:{iteration}:tool_output:{invocation.retry_count}",
            )
            return result

    async def _execute_pending_tool(
        self,
        run: AgentRun,
        state: AgentLoopState,
        definitions: Mapping[str, ToolDefinition],
        approval_id: str | None,
    ) -> tuple[AgentRun, AgentLoopState, ToolInvocationResult]:
        if state.pending_tool_name is None or state.pending_tool_version is None:
            raise RecoveryRejectedError("checkpoint has no pending Tool identity")
        definition = definitions.get(state.pending_tool_name)
        if definition is None or definition.version != state.pending_tool_version:
            raise RecoveryRejectedError("checkpoint Tool identity is not in the allowlist")
        arguments = cast(dict[str, JSONValue], state.pending_arguments or {})
        invocation = ToolInvocation(
            ref=definition.ref,
            arguments=arguments,
            allowed_tools=frozenset(self._allowed_tools),
            granted_permissions=run.context.granted_permissions,
            resource_space_id=run.context.space_id,
            idempotency_key=state.last_idempotency_key or "",
            approval_id=approval_id,
        )
        state = state.start_tool()
        result = await self._invoke_with_retry(
            run, invocation, definition, iteration=state.iteration
        )
        observation = AgentLoopToolObservation(
            iteration=state.iteration,
            tool_name=definition.name,
            tool_version=definition.version,
            idempotency_key=invocation.idempotency_key,
            input_summary=result.record.input_summary,
            output_summary=result.record.output_summary,
            error_code=result.record.error_code,
            retry_count=result.record.retry_count,
            duration_ms=result.record.duration_ms,
        )
        return result.run, state.observe(observation), result

    async def _persist(self, run: AgentRun, state: AgentLoopState) -> AgentRun:
        if self._state_store is None:
            return run
        run, checkpoint = build_checkpoint(
            run,
            state=cast(Mapping[str, JSONValue], state.as_checkpoint()),
            next_step=RunStep.EXECUTING,
            next_node="agent_loop",
        )
        persisted, saved_checkpoint = await self._state_store.commit(run, checkpoint)
        await self._emit(
            persisted,
            AgentRunEventType.CHECKPOINT_SAVED,
            {
                "status": "checkpointed",
                "iteration": state.iteration,
                "checkpoint_sequence": saved_checkpoint.sequence,
                "checkpoint_sha256": saved_checkpoint.state_sha256,
                "continuation": saved_checkpoint.next_node or "none",
            },
            event_key=f"checkpoint:{saved_checkpoint.sequence}",
        )
        return persisted

    async def _finalize_run(self, run: AgentRun) -> AgentRun:
        if self._state_store is None:
            return run
        return await self._state_store.finalize(run)

    async def _cancel(self, run: AgentRun, state: AgentLoopState) -> AgentLoopResult:
        if run.status != RunStatus.CANCEL_REQUESTED:
            run = run.transition(RunEvent.REQUEST_CANCEL)
        run = run.transition(RunEvent.CANCEL)
        state = state.stop(AgentLoopPhase.CANCELLED, AgentLoopStopReason.CANCELLED)
        run = await self._finalize_run(run)
        await self._emit(
            run,
            AgentRunEventType.CANCELLED,
            {
                "status": run.status.value,
                "stop_reason": AgentLoopStopReason.CANCELLED.value,
                "iteration": state.iteration,
            },
            event_key="terminal",
        )
        return AgentLoopResult(run, state, None, error=None)

    async def _fail(
        self, run: AgentRun, state: AgentLoopState, failure: Exception
    ) -> AgentLoopResult:
        if isinstance(failure, NodeExecutionError):
            code, category, message, retryable = (
                failure.code,
                failure.category,
                failure.message,
                failure.retryable,
            )
            timed_out = failure.timed_out
        elif isinstance(failure, ToolRegistryError):
            code, category, message, retryable = (
                failure.code.value,
                _tool_error_category(failure.code),
                str(failure),
                failure.retryable,
            )
            timed_out = failure.code is ToolRegistryErrorCode.TIMEOUT
        else:
            code, category, message, retryable, timed_out = (
                "RUN_BUDGET_EXCEEDED",
                RunErrorCategory.BUDGET,
                str(failure),
                False,
                False,
            )
        error = RunError(
            code=code,
            category=category,
            message=message,
            retryable=retryable,
            safe_summary=message,
        )
        if run.status not in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.TIMED_OUT,
        }:
            run = run.transition(RunEvent.TIMEOUT if timed_out else RunEvent.FAIL)
        phase = AgentLoopPhase.TIMED_OUT if timed_out else AgentLoopPhase.FAILED
        reason = AgentLoopStopReason.TIMED_OUT if timed_out else AgentLoopStopReason.FAILED
        state = state.stop(phase, reason)
        run = replace(run, last_error=error)
        run = await self._finalize_run(run)
        await self._emit(
            run,
            AgentRunEventType.TIMED_OUT if timed_out else AgentRunEventType.FAILED,
            {
                "status": run.status.value,
                "stop_reason": reason.value,
                "iteration": state.iteration,
                "error_code": error.code,
            },
            event_key="terminal",
        )
        return AgentLoopResult(run, state, None, error=error)

    async def _emit(
        self,
        run: AgentRun,
        event_type: AgentRunEventType,
        payload: Mapping[str, object],
        *,
        event_key: str,
    ) -> None:
        if self._event_store is None:
            return
        await self._event_store.append(
            run.context.run_id,
            event_type,
            payload,
            event_key=event_key,
        )

    def _activate(
        self, run: AgentRun, state: AgentLoopState, approval_id: str | None
    ) -> tuple[AgentRun, AgentLoopState]:
        if run.status == RunStatus.WAITING_APPROVAL:
            if approval_id is None:
                return run, state
            run = run.transition(RunEvent.APPROVE)
            state = replace(state, phase=AgentLoopPhase.TOOL_REQUESTED, approval_id=approval_id)
        if run.status == RunStatus.CREATED:
            run = run.transition(RunEvent.START)
        if state.phase is AgentLoopPhase.ACCEPTED:
            state = state.start()
        return run, state

    def _account_elapsed(self, run: AgentRun, started_ms: int, base_elapsed_ms: int) -> AgentRun:
        current = base_elapsed_ms + max(0, self._clock_ms() - started_ms)
        delta = max(0, current - run.usage.elapsed_ms)
        try:
            return run.consume(elapsed_ms=delta)
        except BudgetExceededError as exc:
            raise NodeExecutionError(
                code="RUN_TIMED_OUT",
                category=RunErrorCategory.BUDGET,
                message="Agent Loop total timeout was exceeded.",
                timed_out=True,
            ) from exc

    @staticmethod
    def _idempotency_key(run: AgentRun, iteration: int, decision: LLMDecision) -> str:
        encoded = json.dumps(decision.arguments, ensure_ascii=True, sort_keys=True).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        return f"{run.context.run_id}:loop:{iteration}:{decision.tool_name}:{digest}"

    @staticmethod
    def _request_fingerprint(definition: ToolDefinition, decision: LLMDecision) -> str:
        encoded = json.dumps(decision.arguments, ensure_ascii=True, sort_keys=True).encode("utf-8")
        return f"{definition.name}:{definition.version}:{hashlib.sha256(encoded).hexdigest()}"


def _summary_digest(value: Mapping[str, JSONValue]) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _tool_event_payload(
    definition: ToolDefinition,
    *,
    iteration: int,
    status: str,
    input_summary: str,
    output_summary: str,
    error_code: str | None,
    retry_count: int,
    duration_ms: int,
) -> dict[str, str | int]:
    payload: dict[str, str | int] = {
        "status": status,
        "iteration": iteration,
        "tool_name": definition.name,
        "tool_version": definition.version,
        "input_summary": input_summary,
        "output_summary": output_summary,
        "retry_count": retry_count,
        "duration_ms": duration_ms,
    }
    if error_code is not None:
        payload["error_code"] = error_code
    return payload


def _publication_id(run: AgentRun) -> str:
    return f"assistant-publication:{run.context.run_id.hex}"


def _move_to_executing(run: AgentRun) -> AgentRun:
    if run.status == RunStatus.WAITING_APPROVAL:
        run = run.transition(RunEvent.APPROVE)
    if run.status == RunStatus.CREATED:
        run = run.transition(RunEvent.START)
    if run.status == RunStatus.RUNNING and run.current_step == RunStep.PLANNING:
        run = run.transition(RunEvent.RETRIEVE)
    if run.status == RunStatus.RUNNING and run.current_step == RunStep.RETRIEVING:
        run = run.transition(RunEvent.EXECUTE)
    return run


def _tool_error_category(code: ToolRegistryErrorCode) -> RunErrorCategory:
    if code in {
        ToolRegistryErrorCode.NOT_ALLOWED,
        ToolRegistryErrorCode.PERMISSION_DENIED,
        ToolRegistryErrorCode.SPACE_MISMATCH,
        ToolRegistryErrorCode.APPROVAL_REQUIRED,
        ToolRegistryErrorCode.MODEL_OUTPUT_DENIED,
        ToolRegistryErrorCode.PATH_DENIED,
    }:
        return RunErrorCategory.PERMISSION
    if code in {
        ToolRegistryErrorCode.INPUT_INVALID,
        ToolRegistryErrorCode.OUTPUT_INVALID,
        ToolRegistryErrorCode.ENCODING_INVALID,
        ToolRegistryErrorCode.IDEMPOTENCY_CONFLICT,
    }:
        return RunErrorCategory.SCHEMA
    if code is ToolRegistryErrorCode.BUDGET_EXCEEDED:
        return RunErrorCategory.BUDGET
    if code is ToolRegistryErrorCode.CANCELLED:
        return RunErrorCategory.CANCELLATION
    return RunErrorCategory.DEPENDENCY


def _monotonic_ms() -> int:
    return int(monotonic() * 1000)


async def _not_cancelled(_run: AgentRun) -> bool:
    return False


def deepcopy_json(value: object) -> object:
    """Copy checkpoint JSON without importing a serialization framework into Domain."""
    return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))


__all__ = ["AgentLoopExecutor", "AgentLoopFinalizer", "AgentLoopResult"]
