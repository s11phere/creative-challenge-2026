"""Feature-gated Harness v2 loop using provider-native Tool calls.

This module deliberately does not share v1's text-JSON decision parser.  A
native model turn either asks for exactly one registered Tool or returns the
terminal text which is published once by the server-owned finalizer.
"""

from __future__ import annotations

import hashlib
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
    ToolPermission,
    validate_recovery,
)
from domain.agent_sse import AGENT_RUN_SSE_V4, AgentRunEventStore, AgentRunEventType
from domain.reasoning import ReasoningProfile
from model_gateway import (
    CapabilityAlias,
    ChatMessage,
    ChatRequest,
    ChatResponse,
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
from .loop import AgentLoopDebugTrace
from .native_model_context import (
    MODEL_CONTEXT_SCHEMA_VERSION,
    NativeDecisionHistoryItem,
    NativeModelContextV2,
    NativeModelObservation,
    project_model_observation,
)
from .native_skill_catalog import (
    NativeSkillCatalog,
    NativeSkillPin,
    NativeSkillSelection,
)
from .skills import PinnedSkill, SkillRegistryError
from .tools import (
    QA_ANSWER_MARKER,
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
type PromptCacheAllowed = Callable[[AgentRunContext], bool]

_STATE_SCHEMA_VERSION = "native-tool-use-loop-state-v2"
_PROMPT_CACHE_SCHEMA_VERSION = "assistant-base-prompt-v8"
_MAX_TERMINAL_TEXT_CHARS = 120_000
_MAX_SELECTED_SKILLS = 2
_DEFAULT_MAX_SELECTED_SKILL_INSTRUCTION_BYTES = 96 * 1024
_MAX_MULTI_CALL_PROTOCOL_RETRIES = 1
_INVOKE_SKILL_TOOL_NAME = "invoke_skill"


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
class _NativeToolSurface:
    definitions: tuple[ToolDefinition, ...]
    bootstrap_tools: tuple[ChatToolDefinition, ...] = ()

    @property
    def by_name(self) -> Mapping[str, ToolDefinition]:
        return {definition.name: definition for definition in self.definitions}

    @property
    def chat_tools(self) -> tuple[ChatToolDefinition, ...]:
        return self.bootstrap_tools + tuple(
            ChatToolDefinition(
                name=definition.name,
                description=definition.description,
                input_schema=definition.input_schema,
            )
            for definition in self.definitions
        )


@dataclass(frozen=True)
class NativeToolUseLoopState:
    """Local v2 recovery state. It is never an SSE or audit projection."""

    goal: str
    iteration: int = 0
    observations: tuple[NativeToolUseObservation, ...] = ()
    pending_call: NativeToolUseCall | None = None
    selected_skills: tuple[NativeSkillPin, ...] = ()
    approval_id: str | None = None
    terminal_text: str | None = None
    terminal_output: dict[str, JSONValue] | None = None
    publication_id: str | None = None
    multi_call_protocol_retries: int = 0
    context_schema_version: str = MODEL_CONTEXT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.goal.strip() or len(self.goal) > 4_000:
            raise ValueError("Native Tool-use goal must be non-empty and bounded")
        if self.context_schema_version != MODEL_CONTEXT_SCHEMA_VERSION:
            raise ValueError("Native Tool-use context schema version is unsupported")
        if self.iteration < 0 or len(self.observations) > self.iteration:
            raise ValueError("Native Tool-use loop iteration is invalid")
        if not 0 <= self.multi_call_protocol_retries <= _MAX_MULTI_CALL_PROTOCOL_RETRIES:
            raise ValueError("Native Tool-use protocol retry count is invalid")
        call_ids = tuple(item.call.call_id for item in self.observations)
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("Native Tool-use call IDs must not repeat")
        if self.pending_call is not None and self.pending_call.call_id in set(call_ids):
            raise ValueError("Native Tool-use pending call was already observed")
        selected_names = tuple(skill.name for skill in self.selected_skills)
        if len(selected_names) > _MAX_SELECTED_SKILLS or len(selected_names) != len(
            set(selected_names)
        ):
            raise ValueError("Native Tool-use selected Skill stack is invalid")
        if self.terminal_text is not None and (
            not self.terminal_text.strip() or len(self.terminal_text) > _MAX_TERMINAL_TEXT_CHARS
        ):
            raise ValueError("Native Tool-use terminal text is invalid")
        if self.terminal_text is not None and self.terminal_output is not None:
            raise ValueError("Native Tool-use cannot have two terminal payloads")
        if self.terminal_output is not None:
            try:
                copied_output = _json_copy(self.terminal_output)
                if not isinstance(copied_output, dict):
                    raise ValueError("Native Tool-use server terminal output must be an object")
            except ValueError as exc:
                raise ValueError("Native Tool-use server terminal output must be JSON") from exc
        if (self.terminal_text is None and self.terminal_output is None) != (
            self.publication_id is None
        ):
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

    def retry_after_multiple_calls(self) -> NativeToolUseLoopState:
        if (
            self.pending_call is not None
            or self.terminal_text is not None
            or self.terminal_output is not None
        ):
            raise ValueError("Native Tool-use loop cannot retry its protocol now")
        if self.multi_call_protocol_retries >= _MAX_MULTI_CALL_PROTOCOL_RETRIES:
            raise ValueError("Native Tool-use protocol retry limit was reached")
        return replace(
            self,
            iteration=self.iteration + 1,
            multi_call_protocol_retries=self.multi_call_protocol_retries + 1,
        )

    def select_skill(self, selection: NativeSkillSelection) -> NativeToolUseLoopState:
        if self.pending_call is None:
            raise ValueError("Native Tool-use Skill selection has no pending Tool")
        if selection.pin.name in {skill.name for skill in self.selected_skills}:
            return self
        if len(self.selected_skills) >= _MAX_SELECTED_SKILLS:
            raise ValueError("Native Tool-use selected Skill stack limit was reached")
        return replace(self, selected_skills=self.selected_skills + (selection.pin,))

    def begin_finalization(self, terminal_text: str, publication_id: str) -> NativeToolUseLoopState:
        if (
            self.pending_call is not None
            or self.terminal_text is not None
            or self.terminal_output is not None
        ):
            raise ValueError("Native Tool-use loop cannot finalize now")
        return replace(
            self,
            iteration=self.iteration + 1,
            terminal_text=terminal_text,
            publication_id=publication_id,
        )

    def begin_server_finalization(
        self, terminal_output: dict[str, JSONValue], publication_id: str
    ) -> NativeToolUseLoopState:
        if (
            self.pending_call is not None
            or self.terminal_text is not None
            or self.terminal_output is not None
        ):
            raise ValueError("Native Tool-use loop cannot finalize now")
        if not publication_id:
            raise ValueError("Native Tool-use server publication identity is required")
        try:
            output = _json_copy(terminal_output)
            if not isinstance(output, dict):
                raise ValueError("Native Tool-use server terminal output must be an object")
        except ValueError as exc:
            raise ValueError("Native Tool-use server terminal output must be JSON") from exc
        return replace(
            self,
            terminal_output=cast(dict[str, JSONValue], output),
            publication_id=publication_id,
        )

    def as_checkpoint(self) -> dict[str, JSONValue]:
        return {
            "schema_version": _STATE_SCHEMA_VERSION,
            "goal": self.goal,
            "iteration": self.iteration,
            "observations": [item.as_checkpoint() for item in self.observations],
            "pending_call": self.pending_call.as_checkpoint() if self.pending_call else None,
            "selected_skills": [
                {
                    "name": skill.name,
                    "version": skill.version,
                    "content_sha256": skill.content_sha256,
                }
                for skill in self.selected_skills
            ],
            "approval_id": self.approval_id,
            "terminal_text": self.terminal_text,
            "terminal_output": cast(
                dict[str, JSONValue] | None,
                _json_copy(self.terminal_output) if self.terminal_output is not None else None,
            ),
            "publication_id": self.publication_id,
            "multi_call_protocol_retries": self.multi_call_protocol_retries,
            "context_schema_version": self.context_schema_version,
        }

    @classmethod
    def from_checkpoint(cls, value: Mapping[str, JSONValue]) -> NativeToolUseLoopState:
        if value.get("schema_version") != _STATE_SCHEMA_VERSION:
            raise RecoveryRejectedError("checkpoint is not a native Tool-use loop state")
        observations = value.get("observations")
        selected_skills = value.get("selected_skills")
        if not isinstance(observations, list) or not isinstance(selected_skills, list):
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
                selected_skills=tuple(
                    _native_skill_pin_from_checkpoint(item) for item in selected_skills
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
                terminal_output=(
                    cast(dict[str, JSONValue], _json_copy(value["terminal_output"]))
                    if value.get("terminal_output") is not None
                    else None
                ),
                publication_id=(
                    cast(str, value["publication_id"])
                    if isinstance(value.get("publication_id"), str)
                    else None
                ),
                multi_call_protocol_retries=(
                    cast(int, value["multi_call_protocol_retries"])
                    if isinstance(value.get("multi_call_protocol_retries"), int)
                    and not isinstance(value.get("multi_call_protocol_retries"), bool)
                    else 0
                ),
                context_schema_version=(
                    cast(str, value["context_schema_version"])
                    if isinstance(value.get("context_schema_version"), str)
                    else MODEL_CONTEXT_SCHEMA_VERSION
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


@dataclass(frozen=True)
class NativeServerToolResult:
    """A server-owned v2 Tool outcome that may also terminate the Run."""

    run: AgentRun
    record: ToolCallRecord
    observation: dict[str, JSONValue]
    terminal_output: dict[str, JSONValue] | None = None
    publication_id: str | None = None

    def __post_init__(self) -> None:
        if (self.terminal_output is None) != (self.publication_id is None):
            raise ValueError("Native server Tool terminal identity is incomplete")
        try:
            _json_copy(self.observation)
            _json_copy(self.terminal_output) if self.terminal_output is not None else None
        except ValueError as exc:
            raise ValueError("Native server Tool output must be JSON") from exc


class NativeServerToolCoordinator(Protocol):
    """Application-owned server gates for native v2 knowledge Tools."""

    def tool_refs(self) -> tuple[ToolRef, ...]: ...

    def blocks_direct_terminal(self, selected_skill_names: frozenset[str]) -> bool: ...

    async def execute(
        self,
        run: AgentRun,
        state: NativeToolUseLoopState,
        call: NativeToolUseCall,
        input_data: Mapping[str, JSONValue],
    ) -> NativeServerToolResult: ...

    async def finalize_server_terminal(
        self,
        *,
        run: AgentRun,
        goal: str,
        terminal_output: dict[str, JSONValue],
        publication_id: str,
        input_data: Mapping[str, JSONValue],
    ) -> JSONValue: ...


class NativeToolUseAgentLoopExecutor:
    """A v2 executor that accepts only native Tool calls or terminal text."""

    def __init__(
        self,
        *,
        tool_registry: AgentToolRegistry,
        allowed_tools: tuple[ToolRef, ...],
        base_tools: tuple[ToolRef, ...] = (),
        system_prompt: str,
        model_gateway: ModelGateway,
        state_store: RuntimeStateStore | None = None,
        reasoning_profile: ReasoningProfile | None = None,
        finalizer: NativeToolUseFinalizer | None = None,
        emergency_ceiling: int = 128,
        max_tokens_per_turn: int = 32_768,
        cancellation_check: CancellationCheck | None = None,
        clock_ms: ClockMilliseconds | None = None,
        approval_request: ApprovalRequest | None = None,
        approval_port: ApprovalPort | None = None,
        model_capability_registry: ModelCapabilityRegistry | None = None,
        skill_catalog: NativeSkillCatalog | None = None,
        server_tools: NativeServerToolCoordinator | None = None,
        debug_trace: AgentLoopDebugTrace | None = None,
        event_store: AgentRunEventStore | None = None,
        prompt_caching_allowed: PromptCacheAllowed | None = None,
        max_selected_skill_instruction_bytes: int = (_DEFAULT_MAX_SELECTED_SKILL_INSTRUCTION_BYTES),
    ) -> None:
        names = tuple(ref.name for ref in allowed_tools)
        if len(names) != len(set(names)):
            raise ValueError("Native Tool-use Tools must have unique names")
        base_names = tuple(ref.name for ref in base_tools)
        if len(base_names) != len(set(base_names)) or not set(base_tools).issubset(allowed_tools):
            raise ValueError("Native base Tools must be unique and server-allowlisted")
        if not system_prompt.strip():
            raise ValueError("Native Tool-use system prompt must not be blank")
        if (
            emergency_ceiling < 1
            or max_tokens_per_turn < 1
            or max_selected_skill_instruction_bytes < 1
        ):
            raise ValueError("Native Tool-use limits must be positive")
        self._tool_registry = tool_registry
        self._allowed_tools = allowed_tools
        self._base_tools = base_tools
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
        self._skill_catalog = skill_catalog
        self._server_tools = server_tools
        self._debug_trace = debug_trace
        self._event_store = event_store
        self._prompt_caching_allowed = prompt_caching_allowed
        self._max_selected_skill_instruction_bytes = max_selected_skill_instruction_bytes
        if server_tools is not None:
            server_names = tuple(ref.name for ref in server_tools.tool_refs())
            if len(server_names) != len(set(server_names)):
                raise ValueError("Native server Tool names must be unique")

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
        state = NativeToolUseLoopState.from_checkpoint(raw_state)
        self._validate_recovery_selected_skills(state)
        return await self._execute_from(
            run,
            input_data,
            state=state,
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
            run = _move_to_planning(run)
            if run.checkpoint_sequence == 0:
                await self._emit(
                    run,
                    AgentRunEventType.ACCEPTED,
                    {
                        "status": "accepted",
                        "harness_version": "native-tool-use-v2",
                    },
                    event_key="native:accepted",
                )

            if state.terminal_text is not None:
                return await self._publish_finalization(run, state, input_data)
            if state.pending_call is not None:
                surface = self._tool_surface(state)
                run, state, waiting = await self._complete_pending_tool(
                    run,
                    state,
                    surface,
                    approval_id,
                    input_data,
                )
                if waiting:
                    return NativeToolUseLoopResult(
                        run=run,
                        state=state,
                        output={"status": "waiting_approval"},
                        waiting_approval=True,
                    )
                if state.terminal_output is not None:
                    return await self._publish_finalization(run, state, input_data)

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
                surface = self._tool_surface(state)
                cache_key, cache_mode = self._cache_decision(run, state, surface)
                request = self._request(input_data, state, surface, cache_key=cache_key)
                response = await self._model_gateway.chat(
                    request,
                    capability=CapabilityAlias.FAST_CHAT,
                )
                run = run.consume(
                    steps=1,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                )
                await self._trace_model_round(
                    state,
                    request,
                    response,
                    cache_mode=cache_mode,
                )
                await self._emit_cache_usage(
                    run,
                    state,
                    surface,
                    response,
                    cache_mode=cache_mode,
                    visible_observation_bytes=_json_size(
                        tuple(item.observation for item in request.tool_results)
                    ),
                )
                if len(response.tool_calls) > 1:
                    if state.multi_call_protocol_retries >= _MAX_MULTI_CALL_PROTOCOL_RETRIES:
                        raise NodeExecutionError(
                            code="RUN_NATIVE_TOOL_USE_MULTIPLE_CALLS",
                            category=RunErrorCategory.SCHEMA,
                            message="A native Tool-use turn may request exactly one Tool.",
                        )
                    state = state.retry_after_multiple_calls()
                    run = await self._persist(run, state)
                    continue
                if response.tool_calls:
                    call = response.tool_calls[0]
                    if call.tool_name not in surface.by_name and call.tool_name not in {
                        tool.name for tool in surface.bootstrap_tools
                    }:
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
                        surface,
                        approval_id,
                        input_data,
                    )
                    approval_id = None
                    if waiting:
                        return NativeToolUseLoopResult(
                            run=run,
                            state=state,
                            output={"status": "waiting_approval"},
                            waiting_approval=True,
                        )
                    if state.terminal_output is not None:
                        return await self._publish_finalization(run, state, input_data)
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
                if self._server_tools is not None and self._server_tools.blocks_direct_terminal(
                    frozenset(skill.name for skill in state.selected_skills)
                ):
                    raise NodeExecutionError(
                        code="RUN_NATIVE_TOOL_USE_KNOWLEDGE_TERMINAL_DENIED",
                        category=RunErrorCategory.PERMISSION,
                        message="A selected knowledge Skill requires a server-owned answer.",
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
        surface: _NativeToolSurface,
        *,
        cache_key: str | None = None,
    ) -> ChatRequest:
        selected_instructions = self._selected_skill_instructions(state)
        catalog_text = self._skill_catalog_text()
        context = self._model_context(state, surface)
        projected_results = tuple(
            ChatToolResult(
                call_id=item.call.call_id,
                tool_name=item.call.tool_name,
                observation=self._model_observation_for(item, surface),
            )
            for item in state.observations
        )
        return ChatRequest(
            messages=(
                ChatMessage(
                    role=ChatRole.SYSTEM,
                    content="\n\n".join(
                        (
                            self._system_prompt,
                            *((catalog_text,) if catalog_text else ()),
                            *selected_instructions,
                        )
                    ),
                ),
                ChatMessage(
                    role=ChatRole.USER,
                    content=json.dumps(
                        {
                            "model_context": context.as_dict(),
                            "goal": state.goal,
                            "input": input_data,
                        },
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
            tools=surface.chat_tools,
            tool_call_history=tuple(item.call.as_chat_call() for item in state.observations),
            tool_results=projected_results,
            cache_key=cache_key,
        )

    def _model_context(
        self,
        state: NativeToolUseLoopState,
        surface: _NativeToolSurface,
    ) -> NativeModelContextV2:
        decisions: list[NativeDecisionHistoryItem] = []
        observations: list[NativeModelObservation] = []
        for item in state.observations[-8:]:
            projection = self._model_observation_for(item, surface)
            summary = cast(str, projection.get("summary", f"{item.call.tool_name} completed."))
            status = self._native_model_status(projection.get("status"))
            decisions.append(
                NativeDecisionHistoryItem(
                    iteration=item.iteration,
                    kind="tool",
                    tool_name=item.call.tool_name,
                    status=status,
                    summary=summary,
                    unresolved_item=self._unresolved_item(item),
                )
            )
            observations.append(
                NativeModelObservation(
                    iteration=item.iteration,
                    tool_name=item.call.tool_name,
                    status=status,
                    summary=summary,
                )
            )
        if state.terminal_text is not None or state.terminal_output is not None:
            summary = (
                "Server-owned knowledge terminal."
                if state.terminal_output is not None
                else "Direct terminal response."
            )
            decisions.append(
                NativeDecisionHistoryItem(
                    iteration=state.iteration,
                    kind="terminal",
                    status="terminal",
                    summary=summary,
                )
            )
        selected_skills = tuple(
            cast(
                dict[str, JSONValue],
                {
                    "name": skill.name,
                    "version": skill.version,
                    "content_sha256": skill.content_sha256,
                },
            )
            for skill in state.selected_skills
        )
        return NativeModelContextV2(
            goal=state.goal,
            selected_skills=selected_skills,
            decision_history=tuple(decisions[-12:]),
            observations=tuple(observations[-8:]),
            progress_summary=self._progress_summary(state),
            approval_pending=state.approval_id is not None,
            cancellation_requested=False,
        )

    def _model_observation_for(
        self,
        item: NativeToolUseObservation,
        surface: _NativeToolSurface,
    ) -> dict[str, JSONValue]:
        if item.call.tool_name == _INVOKE_SKILL_TOOL_NAME:
            return {
                "status": "succeeded",
                "summary": f"Selected Skill {item.observation.get('name', 'unknown')}.",
            }
        definition = surface.by_name.get(item.call.tool_name)
        if definition is None:
            raise NodeExecutionError(
                code="RUN_NATIVE_TOOL_USE_CONTEXT_PROJECTION_DENIED",
                category=RunErrorCategory.SCHEMA,
                message="Native Tool observation has no model projection definition.",
            )
        return project_model_observation(
            item.observation,
            definition.model_observation_schema,
            summary=self._observation_summary(item),
        )

    @staticmethod
    def _observation_summary(item: NativeToolUseObservation) -> str:
        observation = item.observation
        if item.call.tool_name == "knowledge_retrieve":
            matched = observation.get("matched_count")
            searches = observation.get("search_count")
            return (
                f"Coverage: {matched if isinstance(matched, int) else 0} matched "
                f"across {searches if isinstance(searches, int) else 0} searches."
            )
        if item.call.tool_name == "knowledge_answer":
            outcome = observation.get("outcome")
            return f"Grounded QA outcome {outcome}."
        return f"{item.call.tool_name} completed."

    @staticmethod
    def _native_model_status(value: JSONValue | None) -> str:
        if value == "needs_retrieval":
            return "needs_input"
        if value == "verification_failed":
            return "failed"
        return "succeeded"

    @staticmethod
    def _unresolved_item(item: NativeToolUseObservation) -> str | None:
        if (
            item.call.tool_name == "knowledge_retrieve"
            and item.observation.get("recommended_next") == "knowledge_retrieve"
        ):
            return "Retrieval coverage remains insufficient."
        if item.call.tool_name == "knowledge_answer":
            if item.observation.get("status") == "needs_retrieval":
                return "Grounded QA still requires retrieval coverage."
            if item.observation.get("status") == "verification_failed":
                return "Grounded QA verification failed."
        return None

    @staticmethod
    def _progress_summary(state: NativeToolUseLoopState) -> str:
        pending = "server terminal pending" if state.pending_call is not None else "none"
        terminal = (
            "direct"
            if state.terminal_text is not None
            else "server-owned"
            if state.terminal_output is not None
            else "none"
        )
        retry_note = (
            ";previous_multiple_tool_request_not_executed=choose_one_tool_or_terminal_text"
            if state.multi_call_protocol_retries
            else ""
        )
        return (
            f"resolved={len(state.observations)};pending={pending};"
            f"approval={state.approval_id or 'none'};terminal={terminal};"
            f"protocol_retries={state.multi_call_protocol_retries}{retry_note}"
        )

    async def _emit(
        self,
        run: AgentRun,
        event_type: AgentRunEventType,
        payload: Mapping[str, JSONValue],
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
            schema_version=AGENT_RUN_SSE_V4,
        )

    @staticmethod
    def _tool_family(tool_name: str, definition: ToolDefinition | None) -> str:
        if tool_name == _INVOKE_SKILL_TOOL_NAME:
            return "bootstrap"
        if tool_name in {"knowledge_retrieve", "knowledge_answer"}:
            return "knowledge"
        if tool_name.startswith("fs_"):
            return "workspace"
        if tool_name.startswith("skill_"):
            return "skill_creator"
        if definition is not None and ToolPermission.EXECUTE_PROCESS in definition.permissions:
            return "command"
        if definition is not None and ToolPermission.WRITE_KNOWLEDGE in definition.permissions:
            return "workspace"
        return "read"

    async def _emit_tool_started(
        self,
        run: AgentRun,
        state: NativeToolUseLoopState,
        *,
        tool_name: str,
        tool_version: str,
        input_summary: str,
        tool_family: str,
    ) -> None:
        await self._emit(
            run,
            AgentRunEventType.TOOL_STARTED,
            {
                "status": "running",
                "iteration": state.iteration,
                "tool_name": tool_name,
                "tool_version": tool_version,
                "input_summary": input_summary,
                "tool_family": tool_family,
            },
            event_key=f"native:tool_started:{state.iteration}:{tool_name}:{tool_version}",
        )

    async def _emit_tool_output(
        self,
        run: AgentRun,
        state: NativeToolUseLoopState,
        surface: _NativeToolSurface,
        *,
        tool_name: str,
        tool_version: str,
        input_summary: str,
        output_summary: str,
        retry_count: int,
        duration_ms: int,
        tool_family: str,
        decision_summary: str,
    ) -> None:
        item = state.observations[-1]
        projection = self._model_observation_for(item, surface)
        context = self._model_context(state, surface)
        await self._emit(
            run,
            AgentRunEventType.TOOL_OUTPUT,
            {
                "status": "succeeded",
                "iteration": state.iteration,
                "tool_name": tool_name,
                "tool_version": tool_version,
                "input_summary": input_summary,
                "output_summary": output_summary,
                "retry_count": retry_count,
                "duration_ms": duration_ms,
                "tool_family": tool_family,
                "decision_summary": decision_summary,
                "visible_observation_bytes": _json_size(projection),
                "context_digest": context.digest(),
            },
            event_key=f"native:tool_output:{state.iteration}:{tool_name}:{tool_version}",
        )

    async def _emit_cache_usage(
        self,
        run: AgentRun,
        state: NativeToolUseLoopState,
        surface: _NativeToolSurface,
        response: ChatResponse,
        *,
        cache_mode: str,
        visible_observation_bytes: int,
    ) -> None:
        context = self._model_context(state, surface)
        await self._emit(
            run,
            AgentRunEventType.CACHE_USED,
            {
                "iteration": state.iteration,
                "cache_mode": cache_mode,
                "cache_read_tokens": response.usage.cached_input_tokens,
                "cache_write_tokens": response.usage.cache_write_input_tokens,
                "visible_observation_bytes": visible_observation_bytes,
                "context_digest": context.digest(),
                "tool_count": len(surface.chat_tools),
            },
            event_key=f"native:cache:{state.iteration}",
        )

    async def _emit_terminal(
        self,
        run: AgentRun,
        state: NativeToolUseLoopState,
        surface: _NativeToolSurface,
        *,
        terminal_kind: str,
        stop_reason: str,
    ) -> None:
        outcome = state.terminal_output.get("outcome") if state.terminal_output else None
        event_type = (
            AgentRunEventType.REFUSED
            if outcome in {"refusal", "conflict"}
            else AgentRunEventType.COMPLETED
        )
        context = self._model_context(state, surface)
        await self._emit(
            run,
            event_type,
            {
                "status": event_type.value,
                "iteration": state.iteration,
                "publication_id": state.publication_id or _publication_id(run),
                "stop_reason": stop_reason,
                "harness_version": "native-tool-use-v2",
                "terminal_kind": terminal_kind,
                "context_digest": context.digest(),
            },
            event_key=f"native:terminal:{state.iteration}",
        )

    def _cache_decision(
        self,
        run: AgentRun,
        state: NativeToolUseLoopState,
        surface: _NativeToolSurface,
    ) -> tuple[str | None, str]:
        if self._prompt_caching_allowed is None or not self._prompt_caching_allowed(run.context):
            return None, "unsupported"
        gateway_status = self._model_gateway.status
        model_capability = self._model_capability_registry.resolve(
            gateway_status.provider,
            gateway_status.model_identity,
        )
        if not model_capability.supports_prompt_caching or (
            not gateway_status.supports_prompt_caching(CapabilityAlias.FAST_CHAT)
        ):
            return None, "unsupported"
        return f"sha256:{self._static_cache_digest(state, surface)}", "requested"

    def _static_cache_digest(
        self,
        state: NativeToolUseLoopState,
        surface: _NativeToolSurface,
    ) -> str:
        status = self._model_gateway.status
        definitions = sorted(
            (
                {
                    "name": definition.name,
                    "version": definition.version,
                    "input_schema": dict(definition.input_schema),
                }
                for definition in surface.definitions
            ),
            key=lambda item: (item["name"], item["version"]),
        )
        selected_skills = sorted(
            (
                {
                    "name": skill.name,
                    "version": skill.version,
                    "content_sha256": skill.content_sha256,
                }
                for skill in state.selected_skills
            ),
            key=lambda item: (item["name"], item["version"]),
        )
        payload = {
            "schema_version": _PROMPT_CACHE_SCHEMA_VERSION,
            "system_prompt_sha256": hashlib.sha256(self._system_prompt.encode("utf-8")).hexdigest(),
            "selected_skills": selected_skills,
            "tools": definitions,
            "provider": status.provider.value,
            "model": status.model_identity,
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()

    async def _trace_model_round(
        self,
        state: NativeToolUseLoopState,
        request: ChatRequest,
        response: ChatResponse,
        *,
        cache_mode: str,
    ) -> None:
        if self._debug_trace is None:
            return
        surface = self._tool_surface(state)
        context = self._model_context(state, surface)
        selected_instructions = self._selected_skill_instructions(state)
        visible_observation_bytes = sum(
            len(
                json.dumps(
                    item.observation,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
            for item in request.tool_results
        )
        await self._debug_trace.record(
            "agent_round",
            round_number=state.iteration + 1,
            phase="native_tool_use",
            harness_version="native-tool-use-v2",
            schema_version="agent-harness-trace-v2",
            context_digest=context.digest(),
            cache_mode=cache_mode,
            visible_observation_bytes=visible_observation_bytes,
            input={
                "tool_count": len(request.tools),
                "message_count": len(request.messages),
                "cache_key": request.cache_key,
                "tools": [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "input_schema": tool.input_schema,
                    }
                    for tool in request.tools
                ],
                "skill_routes": [
                    {
                        "name": route.pin.name,
                        "version": route.pin.version,
                        "description": route.description,
                        "command": route.command,
                        "adapter_available": route.adapter_available,
                    }
                    for route in (
                        self._skill_catalog.list_routes() if self._skill_catalog is not None else ()
                    )
                ],
                "tool_call_history": [
                    {"call_id": call.call_id, "tool_name": call.tool_name}
                    for call in request.tool_call_history
                ],
                "tool_results": [
                    {
                        "call_id": result.call_id,
                        "tool_name": result.tool_name,
                        "observation": result.observation,
                    }
                    for result in request.tool_results
                ],
                "static_prompt_bytes": len(
                    "\n\n".join((self._system_prompt, *selected_instructions)).encode("utf-8")
                ),
                "dynamic_context_bytes": sum(
                    len(message.content.encode("utf-8"))
                    for message in request.messages
                    if message.role is not ChatRole.SYSTEM
                ),
                "eager_skill_instruction_bytes": sum(
                    len(instruction.encode("utf-8")) for instruction in selected_instructions
                ),
                "unselected_skill_instruction_bytes": 0,
            },
            output={
                "usage": {
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "cache_read_tokens": response.usage.cached_input_tokens,
                    "cache_write_tokens": response.usage.cache_write_input_tokens,
                },
                "tool_calls": len(response.tool_calls),
                "finish_reason": response.finish_reason,
            },
        )

    async def _trace_tool_success(
        self,
        *,
        tool_name: str,
        tool_version: str,
        idempotency_key: str,
        input_summary: str,
        output_summary: str,
        retry_count: int,
        duration_ms: int,
        tool_family: str,
        decision_summary: str,
    ) -> None:
        if self._debug_trace is None:
            return
        input_projection = {
            "summary": input_summary,
            "family": tool_family,
        }
        await self._debug_trace.record(
            "tool_call",
            harness_version="native-tool-use-v2",
            schema_version="agent-harness-trace-v2",
            tool_name=tool_name,
            tool_version=tool_version,
            idempotency_key=idempotency_key,
            input=input_projection,
        )
        await self._debug_trace.record(
            "tool_result",
            harness_version="native-tool-use-v2",
            schema_version="agent-harness-trace-v2",
            tool_name=tool_name,
            tool_version=tool_version,
            idempotency_key=idempotency_key,
            output={
                "summary": output_summary,
                "decision_summary": decision_summary,
                "retry_count": retry_count,
                "duration_ms": duration_ms,
                "family": tool_family,
            },
        )

    async def _trace_tool_failure(
        self,
        *,
        tool_name: str,
        tool_version: str,
        idempotency_key: str,
        input_summary: str,
        tool_family: str,
        error: BaseException,
    ) -> None:
        if self._debug_trace is None:
            return
        code = getattr(error, "code", None)
        if not isinstance(code, str):
            code = type(error).__name__
        await self._debug_trace.record(
            "tool_call",
            harness_version="native-tool-use-v2",
            schema_version="agent-harness-trace-v2",
            tool_name=tool_name,
            tool_version=tool_version,
            idempotency_key=idempotency_key,
            input={"summary": input_summary, "family": tool_family},
        )
        await self._debug_trace.record(
            "tool_error",
            harness_version="native-tool-use-v2",
            schema_version="agent-harness-trace-v2",
            tool_name=tool_name,
            tool_version=tool_version,
            idempotency_key=idempotency_key,
            error={"code": code, "message": str(error)[:2000]},
        )

    async def _complete_pending_tool(
        self,
        run: AgentRun,
        state: NativeToolUseLoopState,
        surface: _NativeToolSurface,
        approval_id: str | None,
        input_data: Mapping[str, JSONValue],
    ) -> tuple[AgentRun, NativeToolUseLoopState, bool]:
        call = state.pending_call
        if call is None:
            raise RecoveryRejectedError("native Tool-use loop has no pending Tool")
        if self._server_tools is not None and call.tool_name in {
            ref.name for ref in self._server_tools.tool_refs()
        }:
            definition = surface.by_name.get(call.tool_name)
            if definition is None or not definition.permissions.issubset(
                run.context.granted_permissions
            ):
                raise NodeExecutionError(
                    code="RUN_NATIVE_TOOL_USE_TOOL_DENIED",
                    category=RunErrorCategory.PERMISSION,
                    message="Native server Tool permissions were not granted.",
                )
            await self._emit_tool_started(
                run,
                state,
                tool_name=definition.name,
                tool_version=definition.version,
                input_summary=tool_input_summary(call.arguments),
                tool_family=self._tool_family(definition.name, definition),
            )
            return await self._complete_server_tool(run, state, surface, input_data)
        if self._skill_catalog is not None and call.tool_name == _INVOKE_SKILL_TOOL_NAME:
            return await self._complete_bootstrap_tool(run, state)
        definition = surface.by_name.get(call.tool_name)
        if definition is None:
            raise RecoveryRejectedError("native Tool-use checkpoint Tool is not allowed")
        is_qa_workspace_write = (
            call.tool_name == "fs_write" and call.arguments.get("content") == QA_ANSWER_MARKER
        )
        if is_qa_workspace_write and self._server_tools is not None:
            restore_facts = getattr(self._server_tools, "restore_finalization_facts", None)
            if restore_facts is not None:
                await restore_facts(run.context.run_id)
            resolver = getattr(self._server_tools, "resolve_workspace_write", None)
            if resolver is not None:
                call = replace(
                    call,
                    arguments=cast(
                        dict[str, JSONValue],
                        dict(resolver(run.context.run_id, dict(call.arguments))),
                    ),
                )
        invocation = ToolInvocation(
            ref=definition.ref,
            arguments=call.arguments,
            allowed_tools=frozenset(definition.ref for definition in surface.definitions),
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
            await self._emit(
                run,
                AgentRunEventType.APPROVAL_REQUIRED,
                {
                    "status": "waiting_approval",
                    "iteration": state.iteration,
                    "tool_name": definition.name,
                    "tool_version": definition.version,
                    "approval_id": requested_approval or "unavailable",
                    "input_summary": tool_input_summary(call.arguments),
                    "retry_count": 0,
                    "tool_family": self._tool_family(definition.name, definition),
                    **_tool_display_payload(definition, call.arguments),
                },
                event_key=(
                    f"native:approval_required:{state.iteration}:"
                    f"{definition.name}:{definition.version}"
                ),
            )
            run = await self._persist(run, state)
            return run, state, True
        run = _move_to_executing(run)
        await self._emit_tool_started(
            run,
            state,
            tool_name=definition.name,
            tool_version=definition.version,
            input_summary=tool_input_summary(call.arguments),
            tool_family=self._tool_family(definition.name, definition),
        )
        try:
            result = await self._invoke_with_retry(run, invocation, definition)
        except BaseException as error:
            await self._trace_tool_failure(
                tool_name=definition.name,
                tool_version=definition.version,
                idempotency_key=invocation.idempotency_key,
                input_summary=tool_input_summary(call.arguments),
                tool_family=self._tool_family(definition.name, definition),
                error=error,
            )
            raise
        try:
            state = state.observe(result)
        except ValueError as exc:
            raise NodeExecutionError(
                code="RUN_NATIVE_TOOL_RESULT_INVALID",
                category=RunErrorCategory.SCHEMA,
                message="Native Tool-use result is not a model-visible object.",
            ) from exc
        await self._trace_tool_success(
            tool_name=definition.name,
            tool_version=definition.version,
            idempotency_key=result.record.idempotency_key,
            input_summary=result.record.input_summary,
            output_summary=result.record.output_summary,
            retry_count=result.record.retry_count,
            duration_ms=result.record.duration_ms,
            tool_family=self._tool_family(definition.name, definition),
            decision_summary=self._observation_summary(state.observations[-1]),
        )
        await self._emit_tool_output(
            run,
            state,
            surface,
            tool_name=definition.name,
            tool_version=definition.version,
            input_summary=result.record.input_summary,
            output_summary=result.record.output_summary,
            retry_count=result.record.retry_count,
            duration_ms=result.record.duration_ms,
            tool_family=self._tool_family(definition.name, definition),
            decision_summary=self._observation_summary(state.observations[-1]),
        )
        if is_qa_workspace_write and self._server_tools is not None:
            note_written = getattr(self._server_tools, "note_workspace_written", None)
            if note_written is not None:
                note_written(run.context.run_id)
            terminal_projection = getattr(self._server_tools, "server_terminal_projection", None)
            if terminal_projection is not None:
                projection = terminal_projection(run.context.run_id)
                if projection is not None:
                    terminal_output, publication_id = projection
                    state = state.begin_server_finalization(terminal_output, publication_id)
                    run = _move_to_executing(result.run).transition(RunEvent.FINALIZE)
                    run = await self._persist(run, state, next_step=RunStep.VERIFYING)
                    return run, state, False
        run = await self._persist(result.run, state)
        return run, state, False

    async def _complete_server_tool(
        self,
        run: AgentRun,
        state: NativeToolUseLoopState,
        surface: _NativeToolSurface,
        input_data: Mapping[str, JSONValue],
    ) -> tuple[AgentRun, NativeToolUseLoopState, bool]:
        call = state.pending_call
        coordinator = self._server_tools
        if call is None or coordinator is None:
            raise RecoveryRejectedError("native server Tool continuation is unavailable")
        if call.tool_name not in {ref.name for ref in coordinator.tool_refs()}:
            raise RecoveryRejectedError("native server Tool is not owned by the coordinator")
        definition = surface.by_name.get(call.tool_name)
        try:
            result = await coordinator.execute(run, state, call, input_data)
        except BaseException as error:
            await self._trace_tool_failure(
                tool_name=call.tool_name,
                tool_version=(definition.version if definition is not None else "1.0.0"),
                idempotency_key=self._idempotency_key(run, state.iteration, call),
                input_summary=tool_input_summary(call.arguments),
                tool_family=self._tool_family(call.tool_name, definition),
                error=error,
            )
            raise
        if result.run.context.run_id != run.context.run_id:
            raise NodeExecutionError(
                code="RUN_NATIVE_TOOL_USE_SERVER_RUN_INVALID",
                category=RunErrorCategory.SCHEMA,
                message="Native server Tool returned a Run outside the current loop.",
            )
        invocation_result = ToolInvocationResult(
            output=result.observation,
            run=result.run,
            record=result.record,
        )
        try:
            state = state.observe(invocation_result)
            await self._emit_tool_output(
                run,
                state,
                surface,
                tool_name=result.record.tool_name,
                tool_version=result.record.tool_version,
                input_summary=result.record.input_summary,
                output_summary=result.record.output_summary,
                retry_count=result.record.retry_count,
                duration_ms=result.record.duration_ms,
                tool_family=self._tool_family(result.record.tool_name, None),
                decision_summary=self._observation_summary(state.observations[-1]),
            )
            if result.terminal_output is not None and result.publication_id is not None:
                state = state.begin_server_finalization(
                    result.terminal_output, result.publication_id
                )
        except ValueError as exc:
            raise NodeExecutionError(
                code="RUN_NATIVE_TOOL_RESULT_INVALID",
                category=RunErrorCategory.SCHEMA,
                message="Native server Tool result is not a model-visible object.",
            ) from exc
        await self._trace_tool_success(
            tool_name=result.record.tool_name,
            tool_version=result.record.tool_version,
            idempotency_key=result.record.idempotency_key,
            input_summary=result.record.input_summary,
            output_summary=result.record.output_summary,
            retry_count=result.record.retry_count,
            duration_ms=result.record.duration_ms,
            tool_family=self._tool_family(result.record.tool_name, definition),
            decision_summary=self._observation_summary(state.observations[-1]),
        )
        updated_run = _move_to_executing(result.run)
        if state.terminal_output is not None:
            updated_run = updated_run.transition(RunEvent.FINALIZE)
        run = await self._persist(
            updated_run,
            state,
            next_step=RunStep.VERIFYING if state.terminal_output is not None else RunStep.EXECUTING,
        )
        return run, state, False

    async def _complete_bootstrap_tool(
        self, run: AgentRun, state: NativeToolUseLoopState
    ) -> tuple[AgentRun, NativeToolUseLoopState, bool]:
        catalog = self._skill_catalog
        call = state.pending_call
        if catalog is None or call is None:
            raise RecoveryRejectedError("native Tool-use bootstrap Tool is unavailable")
        if call.tool_name == _INVOKE_SKILL_TOOL_NAME:
            name = _skill_name_argument(call.arguments)
            try:
                selection = catalog.select(name)
                candidate_state = state.select_skill(selection)
                self._selected_skill_instructions(candidate_state)
                state = candidate_state
                await self._emit(
                    run,
                    AgentRunEventType.SKILL_ACTIVATED,
                    {
                        "status": "activated",
                        "iteration": state.iteration,
                        "skill_name": selection.pin.name,
                        "skill_version": selection.pin.version,
                    },
                    event_key=f"native:skill:{selection.pin.name}:{selection.pin.version}",
                )
            except ValueError as exc:
                raise NodeExecutionError(
                    code="RUN_NATIVE_TOOL_USE_SKILL_DENIED",
                    category=RunErrorCategory.PERMISSION,
                    message="Requested Skill is not available through the native runtime.",
                ) from exc
            observation: dict[str, JSONValue] = {
                "name": selection.pin.name,
                "version": selection.pin.version,
                "selected": True,
            }
        else:
            raise RecoveryRejectedError("native Tool-use bootstrap Tool is invalid")
        run = run.consume(tool_calls=1)
        await self._emit_tool_started(
            run,
            state,
            tool_name=call.tool_name,
            tool_version="1.0.0",
            input_summary=tool_input_summary(call.arguments),
            tool_family="bootstrap",
        )
        result = ToolInvocationResult(
            output=observation,
            run=run,
            record=ToolCallRecord(
                tool_name=call.tool_name,
                tool_version="1.0.0",
                permissions=frozenset({ToolPermission.READ_KNOWLEDGE}),
                idempotency_key=self._idempotency_key(run, state.iteration, call),
                input_summary=tool_input_summary(call.arguments),
                output_summary=_bootstrap_output_summary(observation),
            ),
        )
        try:
            state = state.observe(result)
            await self._trace_tool_success(
                tool_name=call.tool_name,
                tool_version="1.0.0",
                idempotency_key=result.record.idempotency_key,
                input_summary=result.record.input_summary,
                output_summary=result.record.output_summary,
                retry_count=result.record.retry_count,
                duration_ms=result.record.duration_ms,
                tool_family="bootstrap",
                decision_summary=self._observation_summary(state.observations[-1]),
            )
            await self._emit_tool_output(
                run,
                state,
                self._tool_surface(state),
                tool_name=call.tool_name,
                tool_version="1.0.0",
                input_summary=result.record.input_summary,
                output_summary=result.record.output_summary,
                retry_count=result.record.retry_count,
                duration_ms=result.record.duration_ms,
                tool_family="bootstrap",
                decision_summary=self._observation_summary(state.observations[-1]),
            )
        except ValueError as exc:
            raise NodeExecutionError(
                code="RUN_NATIVE_TOOL_RESULT_INVALID",
                category=RunErrorCategory.SCHEMA,
                message="Native Tool-use bootstrap result is invalid.",
            ) from exc
        run = await self._persist(run, state)
        return run, state, False

    def _tool_surface(self, state: NativeToolUseLoopState) -> _NativeToolSurface:
        if self._skill_catalog is None:
            definitions = tuple(self._tool_registry.get(ref) for ref in self._allowed_tools)
            self._validate_model_visible_definitions(definitions)
            return _NativeToolSurface(definitions=definitions)
        selections = tuple(self._skill_catalog.resolve(pin) for pin in state.selected_skills)
        selected_tools = tuple(ref for selection in selections for ref in selection.allowed_tools)
        combined_tools = tuple(dict.fromkeys((*selected_tools, *self._base_tools)))
        if not set(combined_tools).issubset(self._allowed_tools):
            raise NodeExecutionError(
                code="RUN_NATIVE_TOOL_USE_SKILL_TOOL_DENIED",
                category=RunErrorCategory.PERMISSION,
                message="Selected Skill exposes a Tool outside the server allowlist.",
            )
        selected_names = frozenset(selection.pin.name for selection in selections)
        if (
            self._server_tools is not None
            and self._server_tools.blocks_direct_terminal(selected_names)
            and not set(self._server_tools.tool_refs()).issubset(combined_tools)
        ):
            raise NodeExecutionError(
                code="RUN_NATIVE_TOOL_USE_KNOWLEDGE_TOOL_DENIED",
                category=RunErrorCategory.PERMISSION,
                message="Selected knowledge Skill does not expose the server knowledge Tools.",
            )
        definitions = tuple(self._tool_registry.get(ref) for ref in combined_tools)
        self._validate_model_visible_definitions(definitions)
        if {definition.name for definition in definitions} & {
            _INVOKE_SKILL_TOOL_NAME,
        }:
            raise NodeExecutionError(
                code="RUN_NATIVE_TOOL_USE_SKILL_TOOL_CONFLICT",
                category=RunErrorCategory.SCHEMA,
                message="Selected Skills cannot shadow bootstrap Tool names.",
            )
        return _NativeToolSurface(
            definitions=definitions,
            bootstrap_tools=(
                ChatToolDefinition(
                    name=_INVOKE_SKILL_TOOL_NAME,
                    description="Select one active Skill by name for a later model turn.",
                    input_schema={
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["name"],
                        "properties": {"name": {"type": "string", "minLength": 1}},
                    },
                ),
            ),
        )

    def _selected_skill_instructions(self, state: NativeToolUseLoopState) -> tuple[str, ...]:
        if self._skill_catalog is None:
            return ()
        selections = tuple(self._skill_catalog.resolve(pin) for pin in state.selected_skills)
        instructions = tuple(selection.instructions for selection in selections)
        if sum(len(item.encode("utf-8")) for item in instructions) > (
            self._max_selected_skill_instruction_bytes
        ):
            raise NodeExecutionError(
                code="RUN_NATIVE_TOOL_USE_SKILL_CONTEXT_LIMIT",
                category=RunErrorCategory.BUDGET,
                message="Selected Skill instructions exceed the native context budget.",
            )
        return instructions

    def _skill_catalog_text(self) -> str:
        if self._skill_catalog is None:
            return ""
        routes = self._skill_catalog.list_routes()
        if not routes:
            return ""
        lines = [
            "Available Skill routes (call `invoke_skill` with one `name` when a route is needed):",
        ]
        for route in routes:
            availability = "callable" if route.adapter_available else "entry-point only"
            lines.append(
                f"- {route.pin.name} v{route.pin.version} "
                f"[{availability}] command={route.command}: {route.description}"
            )
        return "\n".join(lines)

    def _validate_recovery_selected_skills(self, state: NativeToolUseLoopState) -> None:
        if state.selected_skills and self._skill_catalog is None:
            raise RecoveryRejectedError("native Tool-use recovery has no Skill catalog")
        if self._skill_catalog is not None:
            try:
                self._selected_skill_instructions(state)
            except (SkillRegistryError, ValueError, NodeExecutionError) as exc:
                raise RecoveryRejectedError(
                    "native Tool-use selected Skill pin is invalid"
                ) from exc

    @staticmethod
    def _validate_model_visible_definitions(definitions: tuple[ToolDefinition, ...]) -> None:
        if len({definition.name for definition in definitions}) != len(definitions):
            raise NodeExecutionError(
                code="RUN_NATIVE_TOOL_USE_SKILL_TOOL_CONFLICT",
                category=RunErrorCategory.SCHEMA,
                message="Selected Skills expose conflicting Tool names.",
            )
        for definition in definitions:
            if not definition.model_visible:
                raise NodeExecutionError(
                    code=ToolRegistryErrorCode.MODEL_OUTPUT_DENIED.value,
                    category=RunErrorCategory.PERMISSION,
                    message="Tool output is not approved for model visibility.",
                )

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
        if state.terminal_output is not None and state.publication_id is not None:
            if self._server_tools is None:
                raise RecoveryRejectedError("native server finalization coordinator is unavailable")
            output = await self._server_tools.finalize_server_terminal(
                run=run,
                goal=state.goal,
                terminal_output=state.terminal_output,
                publication_id=state.publication_id,
                input_data=input_data,
            )
            outcome = state.terminal_output.get("outcome")
            await self._emit_terminal(
                run,
                state,
                self._tool_surface(state),
                terminal_kind="grounded",
                stop_reason=(
                    "evidence_insufficient"
                    if outcome == "refusal"
                    else "evidence_conflict"
                    if outcome == "conflict"
                    else "goal_complete"
                ),
            )
            run = run.transition(RunEvent.COMPLETE)
            if self._state_store is not None:
                run = await self._state_store.finalize(run)
            return NativeToolUseLoopResult(run=run, state=state, output=output)
        if state.terminal_text is None or state.publication_id is None:
            raise RecoveryRejectedError("native Tool-use finalization checkpoint is incomplete")
        output = await self._finalizer.finalize(
            run=run,
            goal=state.goal,
            terminal_text=state.terminal_text,
            publication_id=state.publication_id,
            input_data=input_data,
        )
        await self._emit_terminal(
            run,
            state,
            self._tool_surface(state),
            terminal_kind="direct",
            stop_reason="goal_complete",
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
        await self._emit(
            run,
            AgentRunEventType.CANCELLED,
            {
                "status": "cancelled",
                "iteration": state.iteration,
                "stop_reason": "cancelled",
                "terminal_kind": "direct",
            },
            event_key=f"native:terminal:{state.iteration}",
        )
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
        await self._emit(
            run,
            AgentRunEventType.TIMED_OUT if timed_out else AgentRunEventType.FAILED,
            {
                "status": "timed_out" if timed_out else "failed",
                "iteration": state.iteration,
                "stop_reason": "timed_out" if timed_out else "failed",
                "error_code": code,
                "terminal_kind": "direct",
            },
            event_key=f"native:terminal:{state.iteration}",
        )
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


def _tool_display_payload(
    definition: ToolDefinition, arguments: Mapping[str, JSONValue]
) -> dict[str, str]:
    if definition.name in {"fs_list", "fs_read", "fs_write"}:
        path = arguments.get("path")
        return {"path": _display_text(path)} if isinstance(path, str) else {}
    if definition.name != "shell_exec":
        return {}
    executable = arguments.get("executable")
    argv = arguments.get("argv")
    cwd = arguments.get("cwd")
    payload: dict[str, str] = {}
    if isinstance(executable, str) and isinstance(argv, list):
        payload["command"] = _display_text(
            " ".join([executable, *[item for item in argv if isinstance(item, str)]])
        )
    if isinstance(cwd, str):
        payload["cwd"] = _display_text(cwd)
    return payload


def _display_text(value: str) -> str:
    cleaned = "".join(character if character.isprintable() else " " for character in value)
    return " ".join(cleaned.split())[:512]


def _native_skill_pin_from_checkpoint(value: object) -> NativeSkillPin:
    if not isinstance(value, dict):
        raise ValueError("native Skill pin checkpoint is invalid")
    name = value.get("name")
    version = value.get("version")
    content_sha256 = value.get("content_sha256")
    if not all(isinstance(item, str) for item in (name, version, content_sha256)):
        raise ValueError("native Skill pin checkpoint is invalid")
    return NativeSkillPin(
        name=cast(str, name),
        version=cast(str, version),
        content_sha256=cast(str, content_sha256),
    )


def _skill_name_argument(arguments: Mapping[str, JSONValue]) -> str:
    if set(arguments) != {"name"} or not isinstance(arguments.get("name"), str):
        raise NodeExecutionError(
            code="RUN_NATIVE_TOOL_USE_BOOTSTRAP_INPUT_INVALID",
            category=RunErrorCategory.SCHEMA,
            message="invoke_skill requires exactly one non-empty name.",
        )
    name = cast(str, arguments["name"]).strip()
    if not name:
        raise NodeExecutionError(
            code="RUN_NATIVE_TOOL_USE_BOOTSTRAP_INPUT_INVALID",
            category=RunErrorCategory.SCHEMA,
            message="invoke_skill requires exactly one non-empty name.",
        )
    return name


def _bootstrap_output_summary(observation: Mapping[str, JSONValue]) -> str:
    name = observation.get("name")
    return f"Selected Skill {name}." if isinstance(name, str) else "Selected a Skill."


def _json_size(value: object) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )


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
    "NativeServerToolCoordinator",
    "NativeServerToolResult",
    "NativeToolUseAgentLoopExecutor",
    "NativeToolUseCall",
    "NativeToolUseFinalizer",
    "NativeToolUseLoopResult",
    "NativeToolUseLoopState",
    "NativeToolUseObservation",
]
