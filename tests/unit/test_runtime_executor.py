from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from agent_runtime.checkpoints import InMemoryRuntimeStateStore
from agent_runtime.executor import (
    DeterministicWorkflowExecutor,
    NodeExecutionContext,
    NodeHandler,
    NodeOutcome,
    NodeResult,
    RuntimeAuditEvent,
    RuntimeAuditEventType,
    RuntimeAuditSink,
    load_workflow,
)
from agent_runtime.skills import FileSystemSkillRegistry, PinnedSkill
from domain.agent_runtime import (
    AgentRun,
    AgentRunContext,
    BudgetUsage,
    RecoveryRejectedError,
    RunStatus,
    RuntimeStateStore,
)
from model_gateway import (
    ChatMessage,
    ChatRequest,
    ChatRole,
    FakeModelGateway,
    FakeScenario,
)


class FakeToolRegistry:
    def __init__(self, available: bool = True) -> None:
        self.available = available

    def is_available(self, name: str, version: str) -> bool:
        return self.available and (name, version) == ("search_knowledge", "1.0.0")


class MemoryAuditSink(RuntimeAuditSink):
    def __init__(self) -> None:
        self.events: list[RuntimeAuditEvent] = []

    async def record(self, event: RuntimeAuditEvent) -> None:
        self.events.append(event)


def workflow_data() -> dict[str, object]:
    return {
        "workflow_version": "1",
        "start": "plan",
        "nodes": [
            {
                "id": "plan",
                "step": "planning",
                "handler": "plan",
                "next": "retrieve",
                "max_retries": 0,
                "required_permissions": [],
                "reserve": {"tool_calls": 0, "input_tokens": 0, "output_tokens": 0},
            },
            {
                "id": "retrieve",
                "step": "retrieving",
                "handler": "retrieve",
                "next": "generate",
                "max_retries": 0,
                "required_permissions": ["read_knowledge"],
                "reserve": {"tool_calls": 1, "input_tokens": 0, "output_tokens": 0},
            },
            {
                "id": "generate",
                "step": "executing",
                "handler": "generate",
                "next": "verify",
                "max_retries": 0,
                "required_permissions": ["model"],
                "reserve": {"tool_calls": 0, "input_tokens": 20, "output_tokens": 20},
            },
            {
                "id": "verify",
                "step": "verifying",
                "handler": "verify",
                "next": None,
                "max_retries": 0,
                "required_permissions": [],
                "reserve": {"tool_calls": 0, "input_tokens": 0, "output_tokens": 0},
            },
        ],
    }


def manifest_data(*, max_input_tokens: int = 100) -> dict[str, object]:
    return {
        "manifest_version": "1",
        "name": "executor_fixture",
        "version": "1.0.0",
        "description": "Synthetic deterministic executor fixture.",
        "input_schema": "schemas/input.json",
        "output_schema": "schemas/output.json",
        "required_tools": [{"name": "search_knowledge", "version": "1.0.0"}],
        "required_capabilities": ["fast_chat"],
        "permissions": ["read_knowledge", "model"],
        "budgets": {
            "max_steps": 4,
            "max_tool_calls": 2,
            "max_input_tokens": max_input_tokens,
            "max_output_tokens": 100,
            "timeout_seconds": 5,
        },
        "entrypoint": "workflow.yaml",
        "compatibility": {
            "runtime": ">=0.1.0,<1.0.0",
            "checkpoint_schema_versions": [2],
        },
        "prompts": ["prompts/system.md"],
        "evals": ["evals/cases.jsonl"],
    }


def write_skill(
    root: Path,
    *,
    workflow: dict[str, object] | None = None,
    manifest: dict[str, object] | None = None,
) -> Path:
    package = root / "executor-fixture"
    (package / "schemas").mkdir(parents=True)
    (package / "prompts").mkdir()
    (package / "evals").mkdir()
    (package / "skill.yaml").write_text(
        yaml.safe_dump(manifest or manifest_data(), sort_keys=False), encoding="utf-8"
    )
    (package / "workflow.yaml").write_text(
        yaml.safe_dump(workflow or workflow_data(), sort_keys=False), encoding="utf-8"
    )
    input_schema = {
        "type": "object",
        "properties": {"question": {"type": "string", "minLength": 1}},
        "required": ["question"],
        "additionalProperties": False,
    }
    output_schema = {"type": "object"}
    (package / "schemas" / "input.json").write_text(json.dumps(input_schema), encoding="utf-8")
    (package / "schemas" / "output.json").write_text(json.dumps(output_schema), encoding="utf-8")
    (package / "prompts" / "system.md").write_text("Synthetic fixture.\n", encoding="utf-8")
    (package / "evals" / "cases.jsonl").write_text('{"case_id":"synthetic"}\n', encoding="utf-8")
    (package / "README.md").write_text("# Executor fixture\n", encoding="utf-8")
    return package


def registry_and_pin(
    tmp_path: Path,
    *,
    workflow: dict[str, object] | None = None,
    manifest: dict[str, object] | None = None,
) -> tuple[FileSystemSkillRegistry, PinnedSkill, AgentRun]:
    write_skill(tmp_path, workflow=workflow, manifest=manifest)
    registry = FileSystemSkillRegistry(tmp_path)
    package = registry.register(registry.load("executor-fixture"))
    registry.activate(package.manifest.name, package.manifest.version)
    pin = registry.pin(package.manifest.name)
    run = AgentRun(
        context=AgentRunContext(
            run_id=UUID("00000000-0000-4000-8000-000000000001"),
            space_id=UUID("00000000-0000-4000-8000-000000000002"),
            skill_name=pin.name,
            skill_version=pin.version,
            skill_content_sha256=pin.content_sha256,
            trace_id="trace-executor-fixture",
            caller_id="user-fixture",
            granted_permissions=package.manifest.permissions,
        ),
        budget=package.manifest.budgets,
    )
    return registry, pin, run


def handlers(*, refuse: bool = False, calls: list[str] | None = None) -> dict[str, NodeHandler]:
    recorded = calls if calls is not None else []

    async def plan(_context: NodeExecutionContext) -> NodeResult:
        recorded.append("plan")
        return NodeResult()

    async def retrieve(_context: NodeExecutionContext) -> NodeResult:
        recorded.append("retrieve")
        return NodeResult(
            state_updates={"evidence_id": "evidence-fixture"},
            usage=BudgetUsage(tool_calls=1),
        )

    async def generate(context: NodeExecutionContext) -> NodeResult:
        recorded.append("generate")
        question = context.input["question"]
        assert isinstance(question, str)
        response = await context.model_gateway.chat(
            ChatRequest(messages=(ChatMessage(ChatRole.USER, question),), max_tokens=20)
        )
        return NodeResult(
            state_updates={"answer": response.text},
            usage=BudgetUsage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            ),
        )

    async def verify(context: NodeExecutionContext) -> NodeResult:
        recorded.append("verify")
        if refuse:
            return NodeResult(
                outcome=NodeOutcome.REFUSE,
                output={"refusal": "insufficient_evidence"},
            )
        return NodeResult(
            outcome=NodeOutcome.COMPLETE,
            output={"answer": context.state["answer"]},
        )

    return {"plan": plan, "retrieve": retrieve, "generate": generate, "verify": verify}


def executor(
    registry: FileSystemSkillRegistry,
    *,
    scenario: FakeScenario = FakeScenario.NORMAL,
    node_handlers: Mapping[str, NodeHandler] | None = None,
    tool_available: bool = True,
    sink: MemoryAuditSink | None = None,
    state_store: RuntimeStateStore | None = None,
    cancellation_check: Callable[[AgentRun], Awaitable[bool]] | None = None,
    clock_ms: Callable[[], int] | None = None,
) -> DeterministicWorkflowExecutor:
    return DeterministicWorkflowExecutor(
        skill_registry=registry,
        model_gateway=FakeModelGateway(scenario=scenario),
        handlers=node_handlers or handlers(),
        tool_registry=FakeToolRegistry(tool_available),
        audit_sink=sink,
        state_store=state_store,
        cancellation_check=cancellation_check,
        clock_ms=clock_ms,
    )


@pytest.mark.asyncio
async def test_success_is_deterministic_and_audited_without_content(tmp_path: Path) -> None:
    registry, pin, run = registry_and_pin(tmp_path)
    sink = MemoryAuditSink()
    runtime = executor(registry, sink=sink, clock_ms=lambda: 0)
    input_data = {"question": "private synthetic question"}
    first = await runtime.execute(run, pin, input_data)
    second = await runtime.execute(run, pin, input_data)
    assert first.run.status == RunStatus.COMPLETED
    assert first.output == second.output
    assert first.run.usage.steps == second.run.usage.steps == 4
    assert first.run.usage.tool_calls == second.run.usage.tool_calls == 1
    assert [event.event_type for event in first.events] == [
        event.event_type for event in second.events
    ]
    assert first.events[-1].event_type == RuntimeAuditEventType.RUN_COMPLETED
    assert "private synthetic question" not in repr(first.events)
    assert sink.events[: len(first.events)] == list(first.events)


@pytest.mark.asyncio
async def test_refusal_is_a_normal_completed_result(tmp_path: Path) -> None:
    registry, pin, run = registry_and_pin(tmp_path)
    result = await executor(
        registry, node_handlers=handlers(refuse=True), clock_ms=lambda: 0
    ).execute(run, pin, {"question": "unsupported"})
    assert result.run.status == RunStatus.COMPLETED
    assert result.refused
    assert result.error is None
    assert result.events[-1].event_type == RuntimeAuditEventType.RUN_REFUSED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "status", "code", "retryable"),
    [
        (FakeScenario.TIMEOUT, RunStatus.TIMED_OUT, "DEPENDENCY_MODEL_TIMEOUT", True),
        (
            FakeScenario.RATE_LIMITED,
            RunStatus.FAILED,
            "DEPENDENCY_MODEL_RATE_LIMITED",
            True,
        ),
        (
            FakeScenario.INVALID_RESPONSE,
            RunStatus.FAILED,
            "DEPENDENCY_MODEL_INVALID_RESPONSE",
            False,
        ),
    ],
)
async def test_model_failures_have_stable_terminal_semantics(
    tmp_path: Path,
    scenario: FakeScenario,
    status: RunStatus,
    code: str,
    retryable: bool,
) -> None:
    registry, pin, run = registry_and_pin(tmp_path)
    result = await executor(registry, scenario=scenario, clock_ms=lambda: 0).execute(
        run, pin, {"question": "fixture"}
    )
    assert result.run.status == status
    assert result.error is not None
    assert result.error.code == code
    assert result.error.retryable is retryable


@pytest.mark.asyncio
async def test_retryable_model_failure_uses_bounded_node_policy(tmp_path: Path) -> None:
    workflow = workflow_data()
    nodes = workflow["nodes"]
    assert isinstance(nodes, list)
    assert isinstance(nodes[2], dict)
    nodes[2]["max_retries"] = 2
    registry, pin, run = registry_and_pin(tmp_path, workflow=workflow)
    called: list[str] = []
    result = await executor(
        registry,
        scenario=FakeScenario.RATE_LIMITED,
        node_handlers=handlers(calls=called),
        clock_ms=lambda: 0,
    ).execute(run, pin, {"question": "fixture"})
    assert result.run.status == RunStatus.FAILED
    assert called.count("generate") == 3
    retry_events = [
        event for event in result.events if event.event_type == RuntimeAuditEventType.NODE_RETRYING
    ]
    assert [event.details for event in retry_events] == [
        (("retry_count", "1"),),
        (("retry_count", "2"),),
    ]


@pytest.mark.asyncio
async def test_budget_reservation_rejects_before_node_handler(tmp_path: Path) -> None:
    registry, pin, run = registry_and_pin(tmp_path, manifest=manifest_data(max_input_tokens=5))
    called: list[str] = []
    result = await executor(
        registry, node_handlers=handlers(calls=called), clock_ms=lambda: 0
    ).execute(run, pin, {"question": "fixture"})
    assert result.run.status == RunStatus.FAILED
    assert result.error is not None
    assert result.error.code == "RUN_BUDGET_EXCEEDED"
    assert called == ["plan", "retrieve"]


@pytest.mark.asyncio
async def test_cancellation_and_total_timeout_are_terminal(tmp_path: Path) -> None:
    registry, pin, run = registry_and_pin(tmp_path)
    called: list[str] = []

    async def cancelled(_run: AgentRun) -> bool:
        return True

    cancelled_result = await executor(
        registry,
        node_handlers=handlers(calls=called),
        cancellation_check=cancelled,
        clock_ms=lambda: 0,
    ).execute(run, pin, {"question": "fixture"})
    assert cancelled_result.run.status == RunStatus.CANCELLED
    assert not called

    ticks = iter((0, 6000))
    timed_out = await executor(registry, clock_ms=lambda: next(ticks, 6000)).execute(
        run, pin, {"question": "fixture"}
    )
    assert timed_out.run.status == RunStatus.TIMED_OUT
    assert timed_out.error is not None
    assert timed_out.error.code == "RUN_TIMED_OUT"


@pytest.mark.asyncio
async def test_unregistered_handler_and_permission_are_rejected(tmp_path: Path) -> None:
    missing_workflow = workflow_data()
    nodes = missing_workflow["nodes"]
    assert isinstance(nodes, list)
    assert isinstance(nodes[0], dict)
    nodes[0]["handler"] = "missing_handler"
    registry, pin, run = registry_and_pin(tmp_path, workflow=missing_workflow)
    result = await executor(registry, clock_ms=lambda: 0).execute(run, pin, {"question": "fixture"})
    assert result.error is not None
    assert result.error.code == "RUN_HANDLER_UNREGISTERED"

    permission_root = tmp_path / "permission"
    permission_workflow = workflow_data()
    permission_nodes = permission_workflow["nodes"]
    assert isinstance(permission_nodes, list)
    assert isinstance(permission_nodes[0], dict)
    permission_nodes[0]["required_permissions"] = ["external_network"]
    permission_registry, permission_pin, permission_run = registry_and_pin(
        permission_root, workflow=permission_workflow
    )
    denied = await executor(permission_registry, clock_ms=lambda: 0).execute(
        permission_run, permission_pin, {"question": "fixture"}
    )
    assert denied.error is not None
    assert denied.error.code == "AUTH_PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_caller_permissions_cannot_be_expanded_by_skill(tmp_path: Path) -> None:
    registry, pin, run = registry_and_pin(tmp_path)
    restricted_context = replace(run.context, granted_permissions=frozenset())
    restricted_run = replace(run, context=restricted_context)
    result = await executor(registry, clock_ms=lambda: 0).execute(
        restricted_run, pin, {"question": "fixture"}
    )
    assert result.run.status == RunStatus.FAILED
    assert result.error is not None
    assert result.error.code == "AUTH_PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_missing_tool_is_rejected_before_workflow(tmp_path: Path) -> None:
    registry, pin, run = registry_and_pin(tmp_path)
    called: list[str] = []
    result = await executor(
        registry,
        node_handlers=handlers(calls=called),
        tool_available=False,
        clock_ms=lambda: 0,
    ).execute(run, pin, {"question": "fixture"})
    assert result.run.status == RunStatus.FAILED
    assert result.error is not None
    assert result.error.code == "TOOL_NOT_FOUND"
    assert not called


@pytest.mark.asyncio
async def test_resume_continues_after_last_committed_node(tmp_path: Path) -> None:
    registry, pin, run = registry_and_pin(tmp_path)
    store = InMemoryRuntimeStateStore()
    first_calls: list[str] = []

    async def fail_generate(_context: NodeExecutionContext) -> NodeResult:
        first_calls.append("generate")
        raise RuntimeError("synthetic interruption")

    interrupted_handlers = handlers(calls=first_calls)
    interrupted_handlers["generate"] = fail_generate
    interrupted = await executor(
        registry, node_handlers=interrupted_handlers, state_store=store, clock_ms=lambda: 0
    ).execute(run, pin, {"question": "fixture"})
    assert interrupted.run.status is RunStatus.FAILED
    stored_run = await store.get_run(run.context.run_id)
    checkpoint = await store.get_latest(run.context.run_id)
    assert stored_run is not None and checkpoint is not None
    assert checkpoint.next_node == "generate"

    resumed_calls: list[str] = []
    resumed = await executor(
        registry, node_handlers=handlers(calls=resumed_calls), state_store=store, clock_ms=lambda: 0
    ).resume(
        stored_run,
        pin,
        checkpoint,
        {"question": "fixture"},
        caller_id=run.context.caller_id,
        space_id=run.context.space_id,
    )

    assert resumed.run.status is RunStatus.COMPLETED
    assert resumed_calls == ["generate", "verify"]
    assert first_calls == ["plan", "retrieve", "generate"]


@pytest.mark.asyncio
async def test_completed_run_replaces_the_recovery_snapshot_with_its_terminal_state(
    tmp_path: Path,
) -> None:
    registry, pin, run = registry_and_pin(tmp_path)
    store = InMemoryRuntimeStateStore()

    completed = await executor(registry, state_store=store, clock_ms=lambda: 0).execute(
        run, pin, {"question": "fixture"}
    )

    stored = await store.get_run(run.context.run_id)
    assert completed.run.status is RunStatus.COMPLETED
    assert stored is not None
    assert stored.status is RunStatus.COMPLETED
    assert stored.current_step is None
    assert stored.checkpoint_sequence == completed.run.checkpoint_sequence


@pytest.mark.asyncio
async def test_resume_rejects_cross_space_and_tampered_state(tmp_path: Path) -> None:
    registry, pin, run = registry_and_pin(tmp_path)
    store = InMemoryRuntimeStateStore()
    calls: list[str] = []
    interrupted_handlers = handlers(calls=calls)

    async def fail_generate(_context: NodeExecutionContext) -> NodeResult:
        raise RuntimeError("synthetic interruption")

    interrupted_handlers["generate"] = fail_generate
    await executor(
        registry, node_handlers=interrupted_handlers, state_store=store, clock_ms=lambda: 0
    ).execute(run, pin, {"question": "fixture"})
    stored_run = await store.get_run(run.context.run_id)
    checkpoint = await store.get_latest(run.context.run_id)
    assert stored_run is not None and checkpoint is not None

    runtime = executor(registry, state_store=store, clock_ms=lambda: 0)
    with pytest.raises(RecoveryRejectedError, match="ownership"):
        await runtime.resume(
            stored_run,
            pin,
            checkpoint,
            {"question": "fixture"},
            caller_id=run.context.caller_id,
            space_id=UUID(int=999),
        )
    with pytest.raises(RecoveryRejectedError, match="digest"):
        await runtime.resume(
            stored_run,
            pin,
            replace(checkpoint, state={"tampered": True}),
            {"question": "fixture"},
            caller_id=run.context.caller_id,
            space_id=run.context.space_id,
        )


def test_repository_template_workflow_is_declarative_and_valid() -> None:
    trusted_root = Path(__file__).parents[2] / "skills"
    package = FileSystemSkillRegistry(trusted_root).load("_template")
    workflow = load_workflow(package)
    assert workflow.start == "plan"
    assert [node.step.value for node in workflow.nodes] == [
        "planning",
        "retrieving",
        "executing",
        "verifying",
    ]
