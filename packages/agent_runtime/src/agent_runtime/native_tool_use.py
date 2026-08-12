"""Feature-gated Harness v2 loop using provider-native Tool calls.

This module deliberately does not share v1's text-JSON decision parser.  A
native model turn either asks for exactly one registered Tool or returns the
terminal text which is published once by the server-owned finalizer.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from time import monotonic
from typing import Protocol, cast
from uuid import UUID

from domain.agent_runtime import (
    AgentRun,
    AgentRunContext,
    ApprovalPort,
    BudgetExceededError,
    RecoveryRejectedError,
    RunCheckpoint,
    RunError,
    RunErrorCategory,
    RunEvent,
    RunStatus,
    RunStep,
    RuntimeStateStore,
    ToolCallRecord,
    validate_recovery,
)
from domain.reasoning import ReasoningProfile
from model_gateway import (
    CapabilityAlias,
    ChatMessage,
    ChatRequest,
    ChatRole,
    ChatToolCall,
    ChatToolDefinition,
    ChatToolResult,
    ModelCapabilityRegistry,
    ModelErrorCode,
    ModelGateway,
    ModelGatewayError,
    default_model_capability_registry,
)

from .checkpoints import build_checkpoint, checkpoint_state_sha256
from .executor import NodeExecutionError
from .llm_decision import AgentToolRegistry
from .skills import PinnedSkill
from .tools import (
    JSONValue,
    ToolDefinition,
    ToolInvocation,
    ToolInvocationResult,
    ToolRef,
    ToolRegistryError,
    ToolRegistryErrorCode,
    tool_input_summary,
    tool_requires_durable_approval,
)

type CancellationCheck = Callable[[AgentRun], Awaitable[bool]]
type ClockMilliseconds = Callable[[], int]
type ApprovalRequest = Callable[[AgentRunContext, ToolCallRecord], Awaitable[str]]

_STATE_SCHEMA_VERSION = "native-tool-use-loop-state-v1"
_MAX_TERMINAL_TEXT_CHARS = 20_000


class NativeToolUseFinalizer(Protocol):
    """Publish one direct model response with an idempotent publication identity."""

    async def finalize(
        self,
        *,
        run: AgentRun,
        goal: str,
        terminal_text: str,
        publication_id: str,
        input_data: Mapping[str, JSONValue],
    ) -> JSONValue: ...


class _DefaultFinalizer:
    async def finalize(
        self,
        *,
        run: AgentRun,
        goal: str,
        terminal_text: str,
        publication_id: str,
        input_data: Mapping[str, JSONValue],
    ) -> JSONValue:
        del run, goal, publication_id, input_data
        return {"message": terminal_text}


@dataclass(frozen=True)
class NativeToolUseCall:
    call_id: str
    tool_name: str
    arguments: dict[str, JSONValue]

    @classmethod
    def from_chat_call(cls, call: ChatToolCall) -> NativeToolUseCall:
        return cls(
            call_id=call.call_id,
            tool_name=call.tool_name,
            arguments=cast(dict[str, JSONValue], _json_copy(dict(call.arguments))),
        )

    def as_chat_call(self) -> ChatToolCall:
        return ChatToolCall(
            call_id=self.call_id,
            tool_name=self.tool_name,
            arguments=self.arguments,
        )

    def as_checkpoint(self) -> dict[str, JSONValue]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "arguments": cast(dict[str, JSONValue], _json_copy(self.arguments)),
        }

    @classmethod
    def from_checkpoint(cls, value: object) -> NativeToolUseCall:
        if not isinstance(value, dict):
            raise RecoveryRejectedError("native Tool call checkpoint is invalid")
        call_id = value.get("call_id")
        tool_name = value.get("tool_name")
        arguments = value.get("arguments")
        if (
            not isinstance(call_id, str)
            or not isinstance(tool_name, str)
            or not isinstance(arguments, dict)
        ):
            raise RecoveryRejectedError("native Tool call checkpoint is invalid")
        try:
            return cls.from_chat_call(
                ChatToolCall(
                    call_id=call_id,
                    tool_name=tool_name,
                    arguments=cast(dict[str, JSONValue], arguments),
                )
            )
        except ValueError as exc:
            raise RecoveryRejectedError("native Tool call checkpoint is invalid") from exc


@dataclass(frozen=True)
class NativeToolUseObservation:
    iteration: int
    call: NativeToolUseCall
    observation: dict[str, JSONValue]
    input_summary: str
    output_summary: str

    def as_chat_result(self) -> ChatToolResult:
        return ChatToolResult(
            call_id=self.call.call_id,
            tool_name=self.call.tool_name,
            observation=self.observation,
        )

    def as_checkpoint(self) -> dict[str, JSONValue]:
        return {
            "iteration": self.iteration,
            "call": self.call.as_checkpoint(),
            "observation": cast(dict[str, JSONValue], _json_copy(self.observation)),
            "input_summary": self.input_summary,
            "output_summary": self.output_summary,
        }

    @classmethod
    def from_checkpoint(cls, value: object) -> NativeToolUseObservation:
        if not isinstance(value, dict):
            raise RecoveryRejectedError("native Tool observation checkpoint is invalid")
        iteration = value.get("iteration")
        observation = value.get("observation")
        input_summary = value.get("input_summary")
        output_summary = value.get("output_summary")
        if (
            isinstance(iteration, bool)
            or not isinstance(iteration, int)
            or iteration < 1
            or not isinstance(observation, dict)
            or not isinstance(input_summary, str)
            or not isinstance(output_summary, str)
        ):
            raise RecoveryRejectedError("native Tool observation checkpoint is invalid")
        return cls(
            iteration=iteration,
            call=NativeToolUseCall.from_checkpoint(value.get("call")),
            observation=cast(dict[str, JSONValue], _json_copy(observation)),
            input_summary=input_summary,
            output_summary=output_summary,
        )


@dataclass(frozen=True)
class NativeToolUseLoopState:
    """Local v2 recovery state. It is never an SSE or audit projection."""

    goal: str
    iteration: int = 0
    observations: tuple[NativeToolUseObservation, ...] = ()
    pending_call: NativeToolUseCall | None = None
    approval_id: str | None = None
    terminal_text: str | None = None
    publication_id: str | None = None

    def __post_init__(self) -> None:
        if not self.goal.strip() or len(self.goal) > 4_000:
            raise ValueError("Native Tool-use goal must be non-empty and bounded")
        if self.iteration < 0 or len(self.observations) > self.iteration:
            raise ValueError("Native Tool-use loop iteration is invalid")
        call_ids = tuple(item.call.call_id for item in self.observations)
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("Native Tool-use call IDs must not repeat")
        if self.pending_call is not None and self.pending_call.call_id in set(call_ids):
            raise ValueError("Native Tool-use pending call was already observed")
        if self.terminal_text is not None and (
            not self.terminal_text.strip() or len(self.terminal_text) > _MAX_TERMINAL_TEXT_CHARS
        ):
            raise ValueError("Native Tool-use terminal text is invalid")
        if (self.terminal_text is None) != (self.publication_id is None):
            raise ValueError("Native Tool-use finalization identity is incomplete")

    @classmethod
    def accepted(cls, goal: str) -> NativeToolUseLoopState:
        return cls(goal=goal)

    def request_tool(self, call: ChatToolCall) -> NativeToolUseLoopState:
        if self.pending_call is not None or self.terminal_text is not None:
            raise ValueError("Native Tool-use loop cannot request another Tool")
        native_call = NativeToolUseCall.from_chat_call(call)
        known_ids = {item.call.call_id for item in self.observations}
        if native_call.call_id in known_ids:
            raise ValueError("Native Tool-use call ID was reused")
        return replace(self, iteration=self.iteration + 1, pending_call=native_call)

    def observe(self, result: ToolInvocationResult) -> NativeToolUseLoopState:
        if self.pending_call is None:
            raise ValueError("Native Tool-use loop has no pending Tool")
        if not isinstance(result.output, dict):
            raise ValueError("Native Tool-use result must be an object")
        observation = NativeToolUseObservation(
            iteration=self.iteration,
            call=self.pending_call,
            observation=cast(dict[str, JSONValue], _json_copy(result.output)),
            input_summary=result.record.input_summary,
            output_summary=result.record.output_summary,
        )
        return replace(
            self,
            observations=self.observations + (observation,),
            pending_call=None,
            approval_id=None,
        )

    def wait_for_approval(self, approval_id: str | None) -> NativeToolUseLoopState:
        if self.pending_call is None:
            raise ValueError("Native Tool-use approval has no pending Tool")
        return replace(self, approval_id=approval_id)

    def begin_finalization(self, terminal_text: str, publication_id: str) -> NativeToolUseLoopState:
        if self.pending_call is not None or self.terminal_text is not None:
            raise ValueError("Native Tool-use loop cannot finalize now")
        return replace(
            self,
            iteration=self.iteration + 1,
            terminal_text=terminal_text,
            publication_id=publication_id,
        )

    def as_checkpoint(self) -> dict[str, JSONValue]:
        return {
            "schema_version": _STATE_SCHEMA_VERSION,
            "goal": self.goal,
            "iteration": self.iteration,
            "observations": [item.as_checkpoint() for item in self.observations],
            "pending_call": self.pending_call.as_checkpoint() if self.pending_call else None,
            "approval_id": self.approval_id,
            "terminal_text": self.terminal_text,
            "publication_id": self.publication_id,
        }

    @classmethod
    def from_checkpoint(cls, value: Mapping[str, JSONValue]) -> NativeToolUseLoopState:
        if value.get("schema_version") != _STATE_SCHEMA_VERSION:
            raise RecoveryRejectedError("checkpoint is not a native Tool-use loop state")
        observations = value.get("observations")
        if not isinstance(observations, list):
            raise RecoveryRejectedError("native Tool-use observations are invalid")
        goal = value.get("goal")
        iteration = value.get("iteration")
        if (
            not isinstance(goal, str)
            or isinstance(iteration, bool)
            or not isinstance(iteration, int)
        ):
            raise RecoveryRejectedError("native Tool-use checkpoint is invalid")
        try:
            return cls(
                goal=goal,
                iteration=iteration,
                observations=tuple(
                    NativeToolUseObservation.from_checkpoint(item) for item in observations
                ),
                pending_call=(
                    NativeToolUseCall.from_checkpoint(value["pending_call"])
                    if value.get("pending_call") is not None
                    else None
                ),
                approval_id=(
                    cast(str, value["approval_id"])
                    if isinstance(value.get("approval_id"), str)
                    else None
                ),
                terminal_text=(
                    cast(str, value["terminal_text"])
                    if isinstance(value.get("terminal_text"), str)
                    else None
                ),
                publication_id=(
                    cast(str, value["publication_id"])
                    if isinstance(value.get("publication_id"), str)
                    else None
                ),
            )
        except (TypeError, ValueError) as exc:
            raise RecoveryRejectedError("native Tool-use checkpoint is invalid") from exc


@dataclass(frozen=True)
class NativeToolUseLoopResult:
    run: AgentRun
    state: NativeToolUseLoopState
    output: JSONValue
    waiting_approval: bool = False
    error: RunError | None = None


class NativeToolUseAgentLoopExecutor:
    """A v2 executor that accepts only native Tool calls or terminal text."""

    def __init__(
        self,
        *,
        tool_registry: AgentToolRegistry,
        allowed_tools: tuple[ToolRef, ...],
        system_prompt: str,
        model_gateway: ModelGateway,
        state_store: RuntimeStateStore | None = None,
        reasoning_profile: ReasoningProfile | None = None,
        finalizer: NativeToolUseFinalizer | None = None,
        emergency_ceiling: int = 32,
        max_tokens_per_turn: int = 6_144,
        cancellation_check: CancellationCheck | None = None,
        clock_ms: ClockMilliseconds | None = None,
        approval_request: ApprovalRequest | None = None,
        approval_port: ApprovalPort | None = None,
        model_capability_registry: ModelCapabilityRegistry | None = None,
    ) -> None:
        names = tuple(ref.name for ref in allowed_tools)
        if len(names) != len(set(names)):
            raise ValueError("Native Tool-use Tools must have unique names")
        if not system_prompt.strip():
            raise ValueError("Native Tool-use system prompt must not be blank")
        if emergency_ceiling < 1 or max_tokens_per_turn < 1:
            raise ValueError("Native Tool-use limits must be positive")
        self._tool_registry = tool_registry
        self._allowed_tools = allowed_tools
        self._system_prompt = system_prompt
        self._model_gateway = model_gateway
        self._state_store = state_store
        self._reasoning_profile = reasoning_profile
        self._finalizer = finalizer or _DefaultFinalizer()
        self._emergency_ceiling = emergency_ceiling
        self._max_tokens_per_turn = max_tokens_per_turn
        self._cancellation_check = cancellation_check or _not_cancelled
        self._clock_ms = clock_ms or _monotonic_ms
        self._approval_request = approval_request
        self._approval_port = approval_port
        self._model_capability_registry = (
            model_capability_registry or default_model_capability_registry()
        )

    async def execute(
        self,
        run: AgentRun,
        pin: PinnedSkill,
        input_data: Mapping[str, JSONValue],
        *,
        goal: str,
        approval_id: str | None = None,
    ) -> NativeToolUseLoopResult:
        del pin
        return await self._execute_from(
            run,
            input_data,
            state=NativeToolUseLoopState.accepted(goal),
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
    ) -> NativeToolUseLoopResult:
        validate_recovery(run, checkpoint, caller_id=caller_id, space_id=space_id)
        if (
            checkpoint.skill_name,
            checkpoint.skill_version,
            checkpoint.skill_content_sha256,
        ) != (pin.name, pin.version, pin.content_sha256):
            raise RecoveryRejectedError(
                "checkpoint Skill pin does not match native Tool-use executor"
            )
        if (
            checkpoint.run_id != run.context.run_id
            or checkpoint.sequence != run.checkpoint_sequence
        ):
            raise RecoveryRejectedError("checkpoint does not match native Tool-use run")
        if checkpoint.next_node != "native_tool_use_loop" or not checkpoint.verified:
            raise RecoveryRejectedError("checkpoint has no native Tool-use continuation")
        raw_state = cast(dict[str, JSONValue], _json_copy(dict(checkpoint.state)))
        if checkpoint.state_sha256 != checkpoint_state_sha256(raw_state):
            raise RecoveryRejectedError("native Tool-use checkpoint digest is invalid")
        return await self._execute_from(
            run,
            input_data,
            state=NativeToolUseLoopState.from_checkpoint(raw_state),
            approval_id=approval_id,
        )

    async def _execute_from(
        self,
        run: AgentRun,
        input_data: Mapping[str, JSONValue],
        *,
        state: NativeToolUseLoopState,
        approval_id: str | None,
    ) -> NativeToolUseLoopResult:
        started_ms = self._clock_ms()
        base_elapsed_ms = run.usage.elapsed_ms
        try:
            gateway_status = self._model_gateway.status
            model_capability = self._model_capability_registry.resolve(
                gateway_status.provider,
                gateway_status.model_identity,
            )
            if (
                not model_capability.supports_native_tool_use
                or not gateway_status.supports_native_tool_use(CapabilityAlias.FAST_CHAT)
            ):
                raise NodeExecutionError(
                    code="RUN_NATIVE_TOOL_USE_UNSUPPORTED",
                    category=RunErrorCategory.DEPENDENCY,
                    message="Configured model capability does not support native Tool use.",
                )
            definitions = tuple(self._tool_registry.get(ref) for ref in self._allowed_tools)
            for definition in definitions:
                if not definition.model_visible:
                    raise NodeExecutionError(
                        code=ToolRegistryErrorCode.MODEL_OUTPUT_DENIED.value,
                        category=RunErrorCategory.PERMISSION,
                        message="Tool output is not approved for model visibility.",
                    )
            by_name = {definition.name: definition for definition in definitions}
            run = _move_to_planning(run)

            if state.terminal_text is not None:
                return await self._publish_finalization(run, state, input_data)
            if state.pending_call is not None:
                run, state, waiting = await self._complete_pending_tool(
                    run,
                    state,
                    by_name,
                    approval_id,
                )
                if waiting:
                    return NativeToolUseLoopResult(
                        run=run,
                        state=state,
                        output={"status": "waiting_approval"},
                        waiting_approval=True,
                    )

            while True:
                run = self._account_elapsed(run, started_ms, base_elapsed_ms)
                if run.status is RunStatus.CANCEL_REQUESTED or await self._cancellation_check(run):
                    return await self._cancel(run, state)
                if state.iteration >= min(self._emergency_ceiling, run.budget.max_steps):
                    raise NodeExecutionError(
                        code="RUN_NATIVE_TOOL_USE_EMERGENCY_CEILING",
                        category=RunErrorCategory.BUDGET,
                        message="Native Tool-use loop emergency ceiling was reached.",
                    )
                response = await self._model_gateway.chat(
                    self._request(input_data, state, definitions),
                    capability=CapabilityAlias.FAST_CHAT,
                )
                run = run.consume(
                    steps=1,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                )
                if len(response.tool_calls) > 1:
                    raise NodeExecutionError(
                        code="RUN_NATIVE_TOOL_USE_MULTIPLE_CALLS",
                        category=RunErrorCategory.SCHEMA,
                        message="A native Tool-use turn may request exactly one Tool.",
                    )
                if response.tool_calls:
                    call = response.tool_calls[0]
                    selected_definition = by_name.get(call.tool_name)
                    if selected_definition is None:
                        raise NodeExecutionError(
                            code="RUN_NATIVE_TOOL_USE_TOOL_DENIED",
                            category=RunErrorCategory.PERMISSION,
                            message="Model selected a Tool outside the server allowlist.",
                        )
                    try:
                        state = state.request_tool(call)
                    except ValueError as exc:
                        raise NodeExecutionError(
                            code="RUN_NATIVE_TOOL_USE_CALL_INVALID",
                            category=RunErrorCategory.SCHEMA,
                            message="Native Tool call is not valid for the current loop state.",
                        ) from exc
                    run = await self._persist(run, state)
                    run, state, waiting = await self._complete_pending_tool(
                        run,
                        state,
                        by_name,
                        approval_id,
                    )
                    approval_id = None
                    if waiting:
                        return NativeToolUseLoopResult(
                            run=run,
                            state=state,
                            output={"status": "waiting_approval"},
                            waiting_approval=True,
                        )
                    continue

                terminal_text = response.text.strip()
                if response.finish_reason == "length":
                    raise NodeExecutionError(
                        code="RUN_NATIVE_TOOL_USE_TERMINAL_TRUNCATED",
                        category=RunErrorCategory.SCHEMA,
                        message="Native Tool-use terminal text exceeded its output budget.",
                    )
                if not terminal_text:
                    raise NodeExecutionError(
                        code="RUN_NATIVE_TOOL_USE_TERMINAL_EMPTY",
                        category=RunErrorCategory.SCHEMA,
                        message="Native Tool-use turn ended without a Tool call or terminal text.",
                    )
                try:
                    state = state.begin_finalization(terminal_text, _publication_id(run))
                except ValueError as exc:
                    raise NodeExecutionError(
                        code="RUN_NATIVE_TOOL_USE_TERMINAL_INVALID",
                        category=RunErrorCategory.SCHEMA,
                        message="Native Tool-use terminal text is invalid.",
                    ) from exc
                run = run.transition(RunEvent.FINALIZE)
                run = await self._persist(run, state, next_step=RunStep.VERIFYING)
                return await self._publish_finalization(run, state, input_data)
        except (NodeExecutionError, ToolRegistryError, BudgetExceededError) as exc:
            return await self._fail(run, state, exc)
        except ModelGatewayError as exc:
            return await self._fail(
                run,
                state,
                NodeExecutionError(
                    code=_model_error_code(exc.code),
                    category=RunErrorCategory.DEPENDENCY,
                    message="Native Tool-use model request failed.",
                    retryable=exc.retryable,
                ),
            )
        except TimeoutError:
            return await self._fail(
                run,
                state,
                NodeExecutionError(
                    code="RUN_TIMED_OUT",
                    category=RunErrorCategory.BUDGET,
                    message="Native Tool-use loop total timeout was exceeded.",
                    timed_out=True,
                ),
            )
        except Exception:
            return await self._fail(
                run,
                state,
                NodeExecutionError(
                    code="RUN_NATIVE_TOOL_USE_FAILED",
                    category=RunErrorCategory.INTERNAL,
                    message="Native Tool-use loop execution failed.",
                ),
            )

    def _request(
        self,
        input_data: Mapping[str, JSONValue],
        state: NativeToolUseLoopState,
        definitions: tuple[ToolDefinition, ...],
    ) -> ChatRequest:
        return ChatRequest(
            messages=(
                ChatMessage(role=ChatRole.SYSTEM, content=self._system_prompt),
                ChatMessage(
                    role=ChatRole.USER,
                    content=json.dumps(
                        {"goal": state.goal, "input": input_data},
                        ensure_ascii=False,
                        allow_nan=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                ),
            ),
            temperature=0.0,
            max_tokens=self._max_tokens_per_turn,
            reasoning_profile=self._reasoning_profile,
            tools=tuple(
                ChatToolDefinition(
                    name=definition.name,
                    description=definition.description,
                    input_schema=definition.input_schema,
                )
                for definition in definitions
            ),
            tool_call_history=tuple(item.call.as_chat_call() for item in state.observations),
            tool_results=tuple(item.as_chat_result() for item in state.observations),
        )

    async def _complete_pending_tool(
        self,
        run: AgentRun,
        state: NativeToolUseLoopState,
        definitions: Mapping[str, ToolDefinition],
        approval_id: str | None,
    ) -> tuple[AgentRun, NativeToolUseLoopState, bool]:
        call = state.pending_call
        if call is None:
            raise RecoveryRejectedError("native Tool-use loop has no pending Tool")
        definition = definitions.get(call.tool_name)
        if definition is None:
            raise RecoveryRejectedError("native Tool-use checkpoint Tool is not allowed")
        invocation = ToolInvocation(
            ref=definition.ref,
            arguments=call.arguments,
            allowed_tools=frozenset(self._allowed_tools),
            granted_permissions=run.context.granted_permissions,
            resource_space_id=run.context.space_id,
            idempotency_key=self._idempotency_key(run, state.iteration, call),
            approval_id=approval_id or state.approval_id,
        )
        if (
            tool_requires_durable_approval(definition.permissions)
            and invocation.approval_id is None
            and not await self._is_always_allowed(run.context, definition)
        ):
            requested_approval = (
                await self._approval_request(
                    run.context,
                    ToolCallRecord(
                        tool_name=definition.name,
                        tool_version=definition.version,
                        permissions=definition.permissions,
                        idempotency_key=invocation.idempotency_key,
                        input_summary=tool_input_summary(call.arguments),
                    ),
                )
                if self._approval_request is not None
                else None
            )
            state = state.wait_for_approval(requested_approval)
            run = _move_to_executing(run).transition(RunEvent.WAIT_APPROVAL)
            run = await self._persist(run, state)
            return run, state, True
        run = _move_to_executing(run)
        result = await self._invoke_with_retry(run, invocation, definition)
        try:
            state = state.observe(result)
        except ValueError as exc:
            raise NodeExecutionError(
                code="RUN_NATIVE_TOOL_RESULT_INVALID",
                category=RunErrorCategory.SCHEMA,
                message="Native Tool-use result is not a model-visible object.",
            ) from exc
        run = await self._persist(result.run, state)
        return run, state, False

    async def _invoke_with_retry(
        self,
        run: AgentRun,
        invocation: ToolInvocation,
        definition: ToolDefinition,
    ) -> ToolInvocationResult:
        while True:
            try:
                return await self._tool_registry.invoke(run, invocation)
            except ToolRegistryError as exc:
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

    async def _publish_finalization(
        self,
        run: AgentRun,
        state: NativeToolUseLoopState,
        input_data: Mapping[str, JSONValue],
    ) -> NativeToolUseLoopResult:
        if state.terminal_text is None or state.publication_id is None:
            raise RecoveryRejectedError("native Tool-use finalization checkpoint is incomplete")
        output = await self._finalizer.finalize(
            run=run,
            goal=state.goal,
            terminal_text=state.terminal_text,
            publication_id=state.publication_id,
            input_data=input_data,
        )
        run = run.transition(RunEvent.COMPLETE)
        if self._state_store is not None:
            run = await self._state_store.finalize(run)
        return NativeToolUseLoopResult(run=run, state=state, output=output)

    async def _persist(
        self,
        run: AgentRun,
        state: NativeToolUseLoopState,
        *,
        next_step: RunStep = RunStep.EXECUTING,
    ) -> AgentRun:
        if self._state_store is None:
            return run
        run, checkpoint = build_checkpoint(
            run,
            state=state.as_checkpoint(),
            next_step=next_step,
            next_node="native_tool_use_loop",
        )
        persisted, _saved = await self._state_store.commit(run, checkpoint)
        return persisted

    async def _cancel(
        self, run: AgentRun, state: NativeToolUseLoopState
    ) -> NativeToolUseLoopResult:
        if run.status is not RunStatus.CANCEL_REQUESTED:
            run = run.transition(RunEvent.REQUEST_CANCEL)
        run = run.transition(RunEvent.CANCEL)
        if self._state_store is not None:
            run = await self._state_store.finalize(run)
        return NativeToolUseLoopResult(run=run, state=state, output=None)

    async def _fail(
        self,
        run: AgentRun,
        state: NativeToolUseLoopState,
        failure: Exception,
    ) -> NativeToolUseLoopResult:
        if isinstance(failure, NodeExecutionError):
            code, category, message, retryable, timed_out = (
                failure.code,
                failure.category,
                failure.message,
                failure.retryable,
                failure.timed_out,
            )
        elif isinstance(failure, ToolRegistryError):
            code, category, message, retryable, timed_out = (
                failure.code.value,
                _tool_error_category(failure.code),
                str(failure),
                failure.retryable,
                failure.code is ToolRegistryErrorCode.TIMEOUT,
            )
        else:
            code, category, message, retryable, timed_out = (
                "RUN_NATIVE_TOOL_USE_FAILED",
                RunErrorCategory.INTERNAL,
                "Native Tool-use loop execution failed.",
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
        run = replace(run, last_error=error)
        if self._state_store is not None:
            run = await self._state_store.finalize(run)
        return NativeToolUseLoopResult(run=run, state=state, output=None, error=error)

    async def _is_always_allowed(
        self, context: AgentRunContext, definition: ToolDefinition
    ) -> bool:
        if self._approval_port is None:
            return False
        checker = getattr(self._approval_port, "is_always_allowed", None)
        if checker is None:
            return False
        return bool(
            await checker(context, tool_name=definition.name, tool_version=definition.version)
        )

    def _account_elapsed(self, run: AgentRun, started_ms: int, base_elapsed_ms: int) -> AgentRun:
        current = base_elapsed_ms + max(0, self._clock_ms() - started_ms)
        delta = max(0, current - run.usage.elapsed_ms)
        try:
            return run.consume(elapsed_ms=delta)
        except BudgetExceededError as exc:
            raise NodeExecutionError(
                code="RUN_TIMED_OUT",
                category=RunErrorCategory.BUDGET,
                message="Native Tool-use loop total timeout was exceeded.",
                timed_out=True,
            ) from exc

    @staticmethod
    def _idempotency_key(run: AgentRun, iteration: int, call: NativeToolUseCall) -> str:
        return f"{run.context.run_id}:native:{iteration}:{call.call_id}"


def _move_to_planning(run: AgentRun) -> AgentRun:
    if run.status is RunStatus.CREATED:
        return run.transition(RunEvent.START)
    if run.status is RunStatus.WAITING_APPROVAL:
        return run.transition(RunEvent.APPROVE)
    return run


def _move_to_executing(run: AgentRun) -> AgentRun:
    run = _move_to_planning(run)
    if run.status is RunStatus.RUNNING and run.current_step is RunStep.PLANNING:
        run = run.transition(RunEvent.RETRIEVE)
    if run.status is RunStatus.RUNNING and run.current_step is RunStep.RETRIEVING:
        run = run.transition(RunEvent.EXECUTE)
    return run


def _publication_id(run: AgentRun) -> str:
    return f"assistant-publication:{run.context.run_id.hex}"


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


def _model_error_code(code: ModelErrorCode) -> str:
    return {
        ModelErrorCode.TIMEOUT: "RUN_MODEL_TIMEOUT",
        ModelErrorCode.RATE_LIMITED: "RUN_MODEL_RATE_LIMITED",
        ModelErrorCode.UNAVAILABLE: "RUN_MODEL_UNAVAILABLE",
        ModelErrorCode.INVALID_RESPONSE: "RUN_MODEL_INVALID_RESPONSE",
        ModelErrorCode.UNSUPPORTED_CAPABILITY: "RUN_MODEL_UNSUPPORTED_CAPABILITY",
        ModelErrorCode.POLICY_DENIED: "RUN_MODEL_POLICY_DENIED",
        ModelErrorCode.AUTHENTICATION: "RUN_MODEL_AUTHENTICATION_FAILED",
        ModelErrorCode.PROVIDER_ERROR: "RUN_MODEL_PROVIDER_ERROR",
    }[code]


def _json_copy(value: object) -> object:
    try:
        return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("native Tool-use state must be JSON") from exc


def _monotonic_ms() -> int:
    return int(monotonic() * 1000)


async def _not_cancelled(_run: AgentRun) -> bool:
    return False


__all__ = [
    "NativeToolUseAgentLoopExecutor",
    "NativeToolUseCall",
    "NativeToolUseFinalizer",
    "NativeToolUseLoopResult",
    "NativeToolUseLoopState",
    "NativeToolUseObservation",
]
