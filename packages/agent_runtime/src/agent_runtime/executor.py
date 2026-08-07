"""Deterministic, bounded workflow execution with safe audit events."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from time import monotonic
from typing import Any, Protocol, cast
from uuid import UUID

import yaml
from domain.agent_runtime import (
    AgentRun,
    BudgetExceededError,
    BudgetUsage,
    RecoveryRejectedError,
    RunCheckpoint,
    RunError,
    RunErrorCategory,
    RunEvent,
    RunStatus,
    RunStep,
    RuntimeStateStore,
    ToolPermission,
    ToolRegistry,
    validate_recovery,
)
from jsonschema import Draft202012Validator
from model_gateway import ModelErrorCode, ModelGateway, ModelGatewayError
from yaml.events import AliasEvent

from .checkpoints import build_checkpoint, checkpoint_state_sha256
from .skills import (
    FileSystemSkillRegistry,
    PinnedSkill,
    SkillPackage,
    SkillRegistryError,
    SkillRegistryErrorCode,
)
from .tools import JSONValue

WORKFLOW_SCHEMA: dict[str, JSONValue] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["workflow_version", "start", "nodes"],
    "properties": {
        "workflow_version": {"const": "1"},
        "start": {"type": "string", "minLength": 1},
        "nodes": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id",
                    "step",
                    "handler",
                    "next",
                    "max_retries",
                    "required_permissions",
                    "reserve",
                ],
                "properties": {
                    "id": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                    "step": {
                        "enum": [step.value for step in RunStep if step != RunStep.WAITING_APPROVAL]
                    },
                    "handler": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                    "next": {"type": ["string", "null"]},
                    "max_retries": {"type": "integer", "minimum": 0, "maximum": 3},
                    "required_permissions": {
                        "type": "array",
                        "uniqueItems": True,
                        "items": {"enum": [permission.value for permission in ToolPermission]},
                    },
                    "reserve": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["tool_calls", "input_tokens", "output_tokens"],
                        "properties": {
                            "tool_calls": {"type": "integer", "minimum": 0},
                            "input_tokens": {"type": "integer", "minimum": 0},
                            "output_tokens": {"type": "integer", "minimum": 0},
                        },
                    },
                },
            },
        },
    },
}


class NodeOutcome(StrEnum):
    CONTINUE = "continue"
    COMPLETE = "complete"
    REFUSE = "refuse"


@dataclass(frozen=True)
class NodeExecutionError(Exception):
    code: str
    category: RunErrorCategory
    message: str
    retryable: bool = False
    timed_out: bool = False

    def __post_init__(self) -> None:
        if not self.code.startswith(("SKILL_", "TOOL_", "RUN_", "AUTH_", "DEPENDENCY_")):
            raise ValueError("node error code does not use an ADR-006 prefix")


class RuntimeAuditEventType(StrEnum):
    RUN_STARTED = "run_started"
    STATE_CHANGED = "state_changed"
    NODE_STARTED = "node_started"
    NODE_COMPLETED = "node_completed"
    NODE_RETRYING = "node_retrying"
    RUN_COMPLETED = "run_completed"
    RUN_REFUSED = "run_refused"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"
    RUN_TIMED_OUT = "run_timed_out"


@dataclass(frozen=True)
class NodeBudgetReservation:
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        if min(self.tool_calls, self.input_tokens, self.output_tokens) < 0:
            raise ValueError("node budget reservation cannot be negative")


@dataclass(frozen=True)
class WorkflowNode:
    id: str
    step: RunStep
    handler: str
    next_node: str | None
    max_retries: int
    required_permissions: frozenset[ToolPermission]
    reserve: NodeBudgetReservation


@dataclass(frozen=True)
class WorkflowDefinition:
    workflow_version: str
    start: str
    nodes: tuple[WorkflowNode, ...]
    source_sha256: str

    def node_map(self) -> dict[str, WorkflowNode]:
        return {node.id: node for node in self.nodes}


@dataclass(frozen=True)
class NodeExecutionContext:
    run: AgentRun
    pin: PinnedSkill
    input: Mapping[str, JSONValue]
    state: Mapping[str, JSONValue]
    model_gateway: ModelGateway


@dataclass(frozen=True)
class NodeResult:
    outcome: NodeOutcome = NodeOutcome.CONTINUE
    state_updates: Mapping[str, JSONValue] = field(default_factory=dict)
    output: JSONValue = None
    usage: BudgetUsage = field(default_factory=BudgetUsage)

    def __post_init__(self) -> None:
        if self.usage.steps or self.usage.elapsed_ms:
            raise ValueError("node handlers cannot account steps or elapsed time directly")


type NodeHandler = Callable[[NodeExecutionContext], Awaitable[NodeResult]]
type CancellationCheck = Callable[[AgentRun], Awaitable[bool]]
type ClockMilliseconds = Callable[[], int]


@dataclass(frozen=True)
class RuntimeAuditEvent:
    event_version: int
    event_type: RuntimeAuditEventType
    run_id: str
    trace_id: str
    skill_name: str
    skill_version: str
    skill_content_sha256: str
    status: RunStatus
    step: RunStep | None
    node_id: str | None = None
    error_code: str | None = None
    details: tuple[tuple[str, str], ...] = ()


class RuntimeAuditSink(Protocol):
    async def record(self, event: RuntimeAuditEvent) -> None: ...


@dataclass(frozen=True)
class RuntimeExecutionResult:
    run: AgentRun
    output: JSONValue
    refused: bool
    error: RunError | None
    events: tuple[RuntimeAuditEvent, ...]


class DeterministicWorkflowExecutor:
    def __init__(
        self,
        *,
        skill_registry: FileSystemSkillRegistry,
        model_gateway: ModelGateway,
        handlers: Mapping[str, NodeHandler],
        available_capabilities: frozenset[str] = frozenset(),
        tool_registry: ToolRegistry | None = None,
        audit_sink: RuntimeAuditSink | None = None,
        state_store: RuntimeStateStore | None = None,
        cancellation_check: CancellationCheck | None = None,
        clock_ms: ClockMilliseconds | None = None,
    ) -> None:
        self._skill_registry = skill_registry
        self._model_gateway = model_gateway
        self._handlers = dict(handlers)
        self._available_capabilities = available_capabilities | frozenset(
            capability.value for capability in model_gateway.status.capabilities
        )
        self._tool_registry = tool_registry
        self._audit_sink = audit_sink
        self._state_store = state_store
        self._cancellation_check = cancellation_check or _not_cancelled
        self._clock_ms = clock_ms or _monotonic_ms

    async def execute(
        self,
        run: AgentRun,
        pin: PinnedSkill,
        input_data: Mapping[str, JSONValue],
    ) -> RuntimeExecutionResult:
        return await self._execute_from(run, pin, input_data, state={}, node_id=None)

    async def resume(
        self,
        run: AgentRun,
        pin: PinnedSkill,
        checkpoint: RunCheckpoint,
        input_data: Mapping[str, JSONValue],
        *,
        caller_id: str,
        space_id: UUID,
    ) -> RuntimeExecutionResult:
        validate_recovery(run, checkpoint, caller_id=caller_id, space_id=space_id)
        self._skill_registry.validate_checkpoint_compatibility(
            pin, checkpoint, tool_registry=self._tool_registry
        )
        checkpoint_state = cast(Mapping[str, JSONValue], checkpoint.state)
        if checkpoint.state_sha256 != checkpoint_state_sha256(checkpoint_state):
            raise RecoveryRejectedError("checkpoint state digest is invalid")
        if checkpoint.next_node is None:
            raise RecoveryRejectedError("checkpoint has no safe continuation")
        state = cast(dict[str, JSONValue], deepcopy(dict(checkpoint.state)))
        return await self._execute_from(
            run, pin, input_data, state=state, node_id=checkpoint.next_node
        )

    async def _execute_from(
        self,
        run: AgentRun,
        pin: PinnedSkill,
        input_data: Mapping[str, JSONValue],
        *,
        state: dict[str, JSONValue],
        node_id: str | None,
    ) -> RuntimeExecutionResult:
        events: list[RuntimeAuditEvent] = []
        started_ms = self._clock_ms()
        base_elapsed_ms = run.usage.elapsed_ms
        try:
            package = self._validate_start(run, pin)
            workflow = load_workflow(package)
            if workflow.source_sha256 != pin.entrypoint_sha256:
                raise _RuntimeError(
                    code="SKILL_DIGEST_MISMATCH",
                    category=RunErrorCategory.MANIFEST,
                    message="Workflow does not match its fixed Skill.",
                )
            self._validate_dependencies(package)
            run = await self._emit(
                run,
                pin,
                events,
                RuntimeAuditEventType.RUN_STARTED,
            )
            node_map = workflow.node_map()
            node_id = node_id or workflow.start
            while True:
                run = self._account_elapsed(run, started_ms, base_elapsed_ms)
                if run.status == RunStatus.CANCEL_REQUESTED or await self._cancellation_check(run):
                    if run.status != RunStatus.CANCEL_REQUESTED:
                        run = run.transition(RunEvent.REQUEST_CANCEL)
                    run = run.transition(RunEvent.CANCEL)
                    await self._emit(
                        run,
                        pin,
                        events,
                        RuntimeAuditEventType.RUN_CANCELLED,
                    )
                    run = await self._finalize(run)
                    return RuntimeExecutionResult(run, None, False, None, tuple(events))
                node = node_map[node_id]
                run = await self._enter_step(run, pin, node.step, events)
                self._validate_node(package, run, node)
                run.usage.add(
                    steps=1,
                    tool_calls=node.reserve.tool_calls,
                    input_tokens=node.reserve.input_tokens,
                    output_tokens=node.reserve.output_tokens,
                    budget=run.budget,
                )
                run = run.consume(steps=1)
                await self._emit(
                    run,
                    pin,
                    events,
                    RuntimeAuditEventType.NODE_STARTED,
                    node_id=node.id,
                )
                handler = self._handlers[node.handler]
                remaining_seconds = max(
                    0.001,
                    (run.budget.timeout_seconds * 1000 - run.usage.elapsed_ms) / 1000,
                )
                context = NodeExecutionContext(
                    run=run,
                    pin=pin,
                    input=input_data,
                    state=dict(state),
                    model_gateway=self._model_gateway,
                )
                attempt = 0
                while True:
                    try:
                        result = await asyncio.wait_for(handler(context), timeout=remaining_seconds)
                        break
                    except ModelGatewayError as exc:
                        if not exc.retryable or attempt >= node.max_retries:
                            raise
                        run = self._account_elapsed(run, started_ms, base_elapsed_ms)
                        attempt += 1
                        await self._emit(
                            run,
                            pin,
                            events,
                            RuntimeAuditEventType.NODE_RETRYING,
                            node_id=node.id,
                            error_code=f"DEPENDENCY_{exc.code.value}",
                            details=(("retry_count", str(attempt)),),
                        )
                        remaining_seconds = max(
                            0.001,
                            (run.budget.timeout_seconds * 1000 - run.usage.elapsed_ms) / 1000,
                        )
                        context = replace(context, run=run)
                    except TimeoutError as exc:
                        raise _RuntimeError(
                            code="DEPENDENCY_NODE_TIMEOUT",
                            category=RunErrorCategory.DEPENDENCY,
                            message="Workflow node timed out.",
                            timed_out=True,
                        ) from exc
                self._validate_node_usage(node, result.usage)
                run = run.consume(
                    tool_calls=result.usage.tool_calls,
                    input_tokens=result.usage.input_tokens,
                    output_tokens=result.usage.output_tokens,
                )
                run = self._account_elapsed(run, started_ms, base_elapsed_ms)
                state.update(result.state_updates)
                await self._emit(
                    run,
                    pin,
                    events,
                    RuntimeAuditEventType.NODE_COMPLETED,
                    node_id=node.id,
                    details=(("outcome", result.outcome.value),),
                )
                if result.outcome in {NodeOutcome.COMPLETE, NodeOutcome.REFUSE}:
                    if run.current_step != RunStep.VERIFYING:
                        raise _RuntimeError(
                            code="RUN_WORKFLOW_INVALID",
                            category=RunErrorCategory.SCHEMA,
                            message="Workflow can terminate only from verification.",
                        )
                    run = run.transition(RunEvent.COMPLETE)
                    event_type = (
                        RuntimeAuditEventType.RUN_REFUSED
                        if result.outcome == NodeOutcome.REFUSE
                        else RuntimeAuditEventType.RUN_COMPLETED
                    )
                    await self._emit(run, pin, events, event_type)
                    run = await self._finalize(run)
                    return RuntimeExecutionResult(
                        run=run,
                        output=result.output,
                        refused=result.outcome == NodeOutcome.REFUSE,
                        error=None,
                        events=tuple(events),
                    )
                if node.next_node is None:
                    raise _RuntimeError(
                        code="RUN_WORKFLOW_INVALID",
                        category=RunErrorCategory.SCHEMA,
                        message="Non-terminal workflow node has no successor.",
                    )
                if self._state_store is not None:
                    next_step = node_map[node.next_node].step
                    run, checkpoint = build_checkpoint(
                        run, state=state, next_step=next_step, next_node=node.next_node
                    )
                    run, _ = await self._state_store.commit(run, checkpoint)
                node_id = node.next_node
        except ModelGatewayError as exc:
            failure = _model_failure(exc)
        except SkillRegistryError as exc:
            failure = _RuntimeError(
                code=exc.code.value,
                category=RunErrorCategory.MANIFEST,
                message=str(exc),
            )
        except BudgetExceededError:
            failure = _RuntimeError(
                code="RUN_BUDGET_EXCEEDED",
                category=RunErrorCategory.BUDGET,
                message="Run budget is exhausted.",
            )
        except NodeExecutionError as exc:
            failure = _RuntimeError(
                code=exc.code,
                category=exc.category,
                message=exc.message,
                retryable=exc.retryable,
                timed_out=exc.timed_out,
            )
        except _RuntimeError as exc:
            failure = exc
        except Exception:
            failure = _RuntimeError(
                code="RUN_NODE_FAILED",
                category=RunErrorCategory.INTERNAL,
                message="Workflow node failed.",
            )
        return await self._fail(run, pin, events, failure)

    def _validate_start(self, run: AgentRun, pin: PinnedSkill) -> SkillPackage:
        package = self._skill_registry.validate_pin(pin)
        context = run.context
        if (context.skill_name, context.skill_version, context.skill_content_sha256) != (
            pin.name,
            pin.version,
            pin.content_sha256,
        ):
            raise _RuntimeError(
                code="SKILL_DIGEST_MISMATCH",
                category=RunErrorCategory.MANIFEST,
                message="Run context does not match its fixed Skill.",
            )
        if run.budget != package.manifest.budgets:
            raise _RuntimeError(
                code="RUN_BUDGET_MISMATCH",
                category=RunErrorCategory.BUDGET,
                message="Run budget does not match the fixed Skill.",
            )
        return package

    def _validate_dependencies(self, package: SkillPackage) -> None:
        manifest = package.manifest
        if not manifest.required_capabilities.issubset(self._available_capabilities):
            raise _RuntimeError(
                code="DEPENDENCY_CAPABILITY_UNAVAILABLE",
                category=RunErrorCategory.DEPENDENCY,
                message="Skill requires an unavailable capability.",
            )
        if manifest.required_tools and self._tool_registry is None:
            raise _RuntimeError(
                code="TOOL_NOT_FOUND",
                category=RunErrorCategory.DEPENDENCY,
                message="Skill requires a Tool Registry.",
            )
        if self._tool_registry is not None:
            for tool in manifest.required_tools:
                if not self._tool_registry.is_available(tool.name, tool.version):
                    raise _RuntimeError(
                        code="TOOL_NOT_FOUND",
                        category=RunErrorCategory.DEPENDENCY,
                        message="Skill requires an unavailable Tool version.",
                    )
        gateway_capabilities = frozenset(
            capability.value for capability in self._model_gateway.status.capabilities
        )
        if not self._model_gateway.status.available and manifest.required_capabilities.intersection(
            gateway_capabilities
        ):
            raise _RuntimeError(
                code="DEPENDENCY_MODEL_UNAVAILABLE",
                category=RunErrorCategory.DEPENDENCY,
                message="Model capability is unavailable.",
            )

    def _validate_node(self, package: SkillPackage, run: AgentRun, node: WorkflowNode) -> None:
        if node.handler not in self._handlers:
            raise _RuntimeError(
                code="RUN_HANDLER_UNREGISTERED",
                category=RunErrorCategory.SCHEMA,
                message="Workflow handler is not registered by application startup.",
            )
        if not node.required_permissions.issubset(package.manifest.permissions):
            raise _RuntimeError(
                code="AUTH_PERMISSION_DENIED",
                category=RunErrorCategory.PERMISSION,
                message="Workflow node exceeds fixed Skill permissions.",
            )
        if not node.required_permissions.issubset(run.context.granted_permissions):
            raise _RuntimeError(
                code="AUTH_PERMISSION_DENIED",
                category=RunErrorCategory.PERMISSION,
                message="Caller has not granted all workflow node permissions.",
            )
        if run.status != RunStatus.RUNNING:
            raise _RuntimeError(
                code="RUN_INVALID_STATE",
                category=RunErrorCategory.INTERNAL,
                message="Workflow node cannot execute from the current state.",
            )

    @staticmethod
    def _validate_node_usage(node: WorkflowNode, usage: BudgetUsage) -> None:
        if (
            usage.tool_calls > node.reserve.tool_calls
            or usage.input_tokens > node.reserve.input_tokens
            or usage.output_tokens > node.reserve.output_tokens
        ):
            raise _RuntimeError(
                code="RUN_USAGE_RESERVATION_EXCEEDED",
                category=RunErrorCategory.BUDGET,
                message="Workflow node exceeded its declared reservation.",
            )

    async def _enter_step(
        self,
        run: AgentRun,
        pin: PinnedSkill,
        step: RunStep,
        events: list[RuntimeAuditEvent],
    ) -> AgentRun:
        if run.current_step == step:
            return run
        transitions = {
            (RunStatus.CREATED, None, RunStep.PLANNING): RunEvent.START,
            (RunStatus.RUNNING, RunStep.PLANNING, RunStep.RETRIEVING): RunEvent.RETRIEVE,
            (RunStatus.RUNNING, RunStep.RETRIEVING, RunStep.EXECUTING): RunEvent.EXECUTE,
            (RunStatus.RUNNING, RunStep.EXECUTING, RunStep.VERIFYING): RunEvent.VERIFY,
        }
        event = transitions.get((run.status, run.current_step, step))
        if event is None:
            raise _RuntimeError(
                code="RUN_WORKFLOW_INVALID",
                category=RunErrorCategory.SCHEMA,
                message="Workflow step order is invalid.",
            )
        updated = run.transition(event)
        await self._emit(updated, pin, events, RuntimeAuditEventType.STATE_CHANGED)
        return updated

    def _account_elapsed(self, run: AgentRun, started_ms: int, base_elapsed_ms: int) -> AgentRun:
        current = base_elapsed_ms + max(0, self._clock_ms() - started_ms)
        delta = max(0, current - run.usage.elapsed_ms)
        try:
            return run.consume(elapsed_ms=delta)
        except BudgetExceededError as exc:
            raise _RuntimeError(
                code="RUN_TIMED_OUT",
                category=RunErrorCategory.BUDGET,
                message="Run total timeout was exceeded.",
                timed_out=True,
            ) from exc

    async def _fail(
        self,
        run: AgentRun,
        pin: PinnedSkill,
        events: list[RuntimeAuditEvent],
        failure: _RuntimeError,
    ) -> RuntimeExecutionResult:
        error = RunError(
            code=failure.code,
            category=failure.category,
            message=failure.message,
            retryable=failure.retryable,
            safe_summary=failure.message,
        )
        if run.status not in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.TIMED_OUT,
        }:
            run = run.transition(RunEvent.TIMEOUT if failure.timed_out else RunEvent.FAIL)
        run = replace(run, last_error=error)
        await self._emit(
            run,
            pin,
            events,
            RuntimeAuditEventType.RUN_TIMED_OUT
            if failure.timed_out
            else RuntimeAuditEventType.RUN_FAILED,
            error_code=failure.code,
        )
        # Ordinary failures retain their last verified checkpoint for an explicit retry.
        # A timeout is terminal by contract and cannot be safely resumed.
        if failure.timed_out:
            run = await self._finalize(run)
        return RuntimeExecutionResult(run, None, False, error, tuple(events))

    async def _finalize(self, run: AgentRun) -> AgentRun:
        if self._state_store is None:
            return run
        return await self._state_store.finalize(run)

    async def _emit(
        self,
        run: AgentRun,
        pin: PinnedSkill,
        events: list[RuntimeAuditEvent],
        event_type: RuntimeAuditEventType,
        *,
        node_id: str | None = None,
        error_code: str | None = None,
        details: tuple[tuple[str, str], ...] = (),
    ) -> AgentRun:
        event = RuntimeAuditEvent(
            event_version=1,
            event_type=event_type,
            run_id=str(run.context.run_id),
            trace_id=run.context.trace_id,
            skill_name=pin.name,
            skill_version=pin.version,
            skill_content_sha256=pin.content_sha256,
            status=run.status,
            step=run.current_step,
            node_id=node_id,
            error_code=error_code,
            details=details,
        )
        events.append(event)
        if self._audit_sink is not None:
            await self._audit_sink.record(event)
        return run


@dataclass(frozen=True)
class _RuntimeError(Exception):
    code: str
    category: RunErrorCategory
    message: str
    retryable: bool = False
    timed_out: bool = False


def load_workflow(package: SkillPackage) -> WorkflowDefinition:
    path = package.root / Path(package.manifest.entrypoint)
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    try:
        if any(isinstance(event, AliasEvent) for event in yaml.parse(text)):
            raise SkillRegistryError(
                SkillRegistryErrorCode.INVALID_MANIFEST,
                "Workflow YAML aliases are not allowed.",
            )
        loaded: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SkillRegistryError(
            SkillRegistryErrorCode.INVALID_MANIFEST,
            "Workflow YAML is invalid.",
        ) from exc
    if not isinstance(loaded, dict):
        raise SkillRegistryError(
            SkillRegistryErrorCode.INVALID_MANIFEST,
            "Workflow must be an object.",
        )
    data = cast(dict[str, Any], loaded)
    error = next(Draft202012Validator(WORKFLOW_SCHEMA).iter_errors(data), None)
    if error is not None:
        location = "/".join(str(part) for part in error.absolute_path) or "root"
        raise SkillRegistryError(
            SkillRegistryErrorCode.INVALID_MANIFEST,
            f"Workflow validation failed at {location}.",
        )
    raw_nodes = cast(list[dict[str, Any]], data["nodes"])
    nodes: list[WorkflowNode] = []
    for raw in raw_nodes:
        reserve = cast(dict[str, int], raw["reserve"])
        nodes.append(
            WorkflowNode(
                id=cast(str, raw["id"]),
                step=RunStep(cast(str, raw["step"])),
                handler=cast(str, raw["handler"]),
                next_node=cast(str | None, raw["next"]),
                max_retries=cast(int, raw["max_retries"]),
                required_permissions=frozenset(
                    ToolPermission(value) for value in cast(list[str], raw["required_permissions"])
                ),
                reserve=NodeBudgetReservation(**reserve),
            )
        )
    node_ids = [node.id for node in nodes]
    if len(node_ids) != len(set(node_ids)):
        raise SkillRegistryError(
            SkillRegistryErrorCode.INVALID_MANIFEST,
            "Workflow node IDs must be unique.",
        )
    start = cast(str, data["start"])
    if start not in node_ids:
        raise SkillRegistryError(
            SkillRegistryErrorCode.INVALID_MANIFEST,
            "Workflow start node does not exist.",
        )
    if any(node.next_node is not None and node.next_node not in node_ids for node in nodes):
        raise SkillRegistryError(
            SkillRegistryErrorCode.INVALID_MANIFEST,
            "Workflow successor does not exist.",
        )
    digests = dict(package.file_digests)
    return WorkflowDefinition(
        workflow_version=cast(str, data["workflow_version"]),
        start=start,
        nodes=tuple(nodes),
        source_sha256=digests[package.manifest.entrypoint],
    )


def _model_failure(error: ModelGatewayError) -> _RuntimeError:
    timed_out = error.code == ModelErrorCode.TIMEOUT
    return _RuntimeError(
        code=f"DEPENDENCY_{error.code.value}",
        category=RunErrorCategory.DEPENDENCY,
        message="Model dependency failed.",
        retryable=error.retryable,
        timed_out=timed_out,
    )


async def _not_cancelled(_run: AgentRun) -> bool:
    return False


def _monotonic_ms() -> int:
    return int(monotonic() * 1000)
