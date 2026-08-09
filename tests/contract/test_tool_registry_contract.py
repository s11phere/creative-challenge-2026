from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from typing import cast
from uuid import uuid4

import pytest
from agent_runtime.tools import (
    InMemoryToolRegistry,
    JSONValue,
    ToolDefinition,
    ToolExecutionContext,
    ToolInvocation,
    ToolRegistryError,
    ToolRegistryErrorCode,
)
from domain.agent_runtime import (
    AgentRun,
    AgentRunContext,
    ApprovalPort,
    RunBudget,
    ToolCallRecord,
    ToolPermission,
)

OBJECT_SCHEMA: dict[str, JSONValue] = {
    "type": "object",
    "properties": {"query": {"type": "string", "minLength": 1}},
    "required": ["query"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA: dict[str, JSONValue] = {
    "type": "object",
    "properties": {"result": {"type": "string"}},
    "required": ["result"],
    "additionalProperties": False,
}


async def read_handler(
    arguments: dict[str, JSONValue], context: ToolExecutionContext
) -> dict[str, JSONValue]:
    assert context.idempotency_key
    return {"result": f"found:{arguments['query']}"}


async def invalid_output_handler(
    arguments: dict[str, JSONValue], _context: ToolExecutionContext
) -> dict[str, JSONValue]:
    return {"unexpected": arguments["query"]}


async def slow_handler(
    arguments: dict[str, JSONValue], _context: ToolExecutionContext
) -> dict[str, JSONValue]:
    await asyncio.sleep(0.02)
    return {"result": str(arguments["query"])}


class FakeApprovalPort(ApprovalPort):
    def __init__(self, approved: bool) -> None:
        self.approved = approved

    async def request(self, _context: AgentRunContext, _tool: ToolCallRecord) -> str:
        return "approval-1"

    async def is_approved(self, approval_id: str, _context: AgentRunContext) -> bool:
        return self.approved and approval_id == "approval-1"

    async def is_approved_for_tool(
        self,
        approval_id: str,
        context: AgentRunContext,
        *,
        tool_name: str,
        tool_version: str,
    ) -> bool:
        _ = tool_name
        return await self.is_approved(approval_id, context) and tool_version == "1.0.0"


def make_run(*, max_tool_calls: int = 2) -> AgentRun:
    return AgentRun(
        context=AgentRunContext(
            run_id=uuid4(),
            space_id=uuid4(),
            skill_name="test_skill",
            skill_version="1.0.0",
            skill_content_sha256="a" * 64,
            trace_id="trace-1",
            caller_id="user-1",
        ),
        budget=RunBudget(max_tool_calls=max_tool_calls),
    )


def definition(
    *,
    name: str = "search_knowledge",
    version: str = "1.0.0",
    handler_name: str = "read",
    permissions: frozenset[ToolPermission] = frozenset({ToolPermission.READ_KNOWLEDGE}),
    timeout_seconds: float = 1.0,
    max_retries: int = 0,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        version=version,
        description="Deterministic contract fixture.",
        input_schema=OBJECT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        permissions=permissions,
        handler_name=handler_name,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
    )


def invocation(run: AgentRun, tool: ToolDefinition) -> ToolInvocation:
    return ToolInvocation(
        ref=tool.ref,
        arguments={"query": "safe test"},
        allowed_tools=frozenset({tool.ref}),
        granted_permissions=tool.permissions,
        resource_space_id=run.context.space_id,
        idempotency_key="run-1:tool-1",
    )


def test_registration_is_idempotent_and_rejects_conflicts() -> None:
    registry = InMemoryToolRegistry(handlers={"read": read_handler})
    tool = definition()
    assert registry.register(tool) is tool
    assert registry.register(tool) is tool
    with pytest.raises(ToolRegistryError) as captured:
        registry.register(replace(tool, description="changed"))
    assert captured.value.code == ToolRegistryErrorCode.VERSION_CONFLICT


@pytest.mark.parametrize(
    ("tool", "code"),
    [
        (definition(handler_name="missing"), ToolRegistryErrorCode.HANDLER_UNREGISTERED),
        (
            replace(definition(), input_schema={"$ref": "https://example.test/schema"}),
            ToolRegistryErrorCode.INVALID_SCHEMA,
        ),
        (
            replace(definition(), input_schema={"type": "unknown"}),
            ToolRegistryErrorCode.INVALID_SCHEMA,
        ),
    ],
)
def test_registration_rejects_untrusted_handler_and_invalid_schema(
    tool: ToolDefinition, code: ToolRegistryErrorCode
) -> None:
    registry = InMemoryToolRegistry(handlers={"read": read_handler})
    with pytest.raises(ToolRegistryError) as captured:
        registry.register(tool)
    assert captured.value.code == code


@pytest.mark.asyncio
async def test_valid_invocation_updates_budget_and_records_only_digests() -> None:
    registry = InMemoryToolRegistry(handlers={"read": read_handler})
    tool = registry.register(definition())
    run = make_run()
    result = await registry.invoke(run, invocation(run, tool))
    assert result.output == {"result": "found:safe test"}
    assert result.run.usage.tool_calls == 1
    assert result.record.input_summary.startswith("sha256:")
    assert "safe test" not in result.record.input_summary
    assert result.record.output_summary.startswith("sha256:")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"allowed_tools": frozenset()}, ToolRegistryErrorCode.NOT_ALLOWED),
        ({"granted_permissions": frozenset()}, ToolRegistryErrorCode.PERMISSION_DENIED),
        ({"resource_space_id": uuid4()}, ToolRegistryErrorCode.SPACE_MISMATCH),
        ({"arguments": {}}, ToolRegistryErrorCode.INPUT_INVALID),
    ],
)
async def test_preconditions_reject_before_handler(
    change: Mapping[str, object], code: ToolRegistryErrorCode
) -> None:
    called = False

    async def handler(
        _arguments: dict[str, JSONValue], _context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        nonlocal called
        called = True
        return {"result": "unexpected"}

    registry = InMemoryToolRegistry(handlers={"read": handler})
    tool = registry.register(definition())
    run = make_run()
    request = replace(invocation(run, tool), **cast(dict[str, object], change))
    with pytest.raises(ToolRegistryError) as captured:
        await registry.invoke(run, request)
    assert captured.value.code == code
    assert not called


@pytest.mark.asyncio
async def test_budget_is_checked_before_handler() -> None:
    called = False

    async def handler(
        _arguments: dict[str, JSONValue], _context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        nonlocal called
        called = True
        return {"result": "unexpected"}

    registry = InMemoryToolRegistry(handlers={"read": handler})
    tool = registry.register(definition())
    run = make_run(max_tool_calls=1).consume(tool_calls=1)
    with pytest.raises(ToolRegistryError) as captured:
        await registry.invoke(run, invocation(run, tool))
    assert captured.value.code == ToolRegistryErrorCode.BUDGET_EXCEEDED
    assert not called


@pytest.mark.asyncio
async def test_output_schema_and_timeout_have_stable_errors() -> None:
    invalid_registry = InMemoryToolRegistry(handlers={"invalid": invalid_output_handler})
    invalid_tool = invalid_registry.register(definition(handler_name="invalid"))
    run = make_run()
    with pytest.raises(ToolRegistryError) as invalid:
        await invalid_registry.invoke(run, invocation(run, invalid_tool))
    assert invalid.value.code == ToolRegistryErrorCode.OUTPUT_INVALID
    assert invalid.value.record is not None
    assert invalid.value.record.error_code == ToolRegistryErrorCode.OUTPUT_INVALID

    timeout_registry = InMemoryToolRegistry(handlers={"slow": slow_handler})
    timeout_tool = timeout_registry.register(
        definition(handler_name="slow", timeout_seconds=0.001, max_retries=1)
    )
    with pytest.raises(ToolRegistryError) as timed_out:
        await timeout_registry.invoke(run, invocation(run, timeout_tool))
    assert timed_out.value.code == ToolRegistryErrorCode.TIMEOUT
    assert timed_out.value.retryable
    assert timed_out.value.record is not None


@pytest.mark.asyncio
async def test_unknown_name_or_version_is_rejected_before_handler() -> None:
    called = False

    async def handler(
        _arguments: dict[str, JSONValue], _context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        nonlocal called
        called = True
        return {"result": "unexpected"}

    registry = InMemoryToolRegistry(handlers={"read": handler})
    tool = registry.register(definition())
    run = make_run()
    unknown = replace(invocation(run, tool), ref=replace(tool.ref, version="2.0.0"))
    with pytest.raises(ToolRegistryError) as captured:
        await registry.invoke(run, unknown)
    assert captured.value.code == ToolRegistryErrorCode.NOT_FOUND
    assert not called


def test_registration_validates_permissions_and_capabilities() -> None:
    with pytest.raises(ToolRegistryError) as invalid_permission:
        replace(definition(), permissions=cast(frozenset[ToolPermission], frozenset({"invalid"})))
    assert invalid_permission.value.code == ToolRegistryErrorCode.INVALID_DEFINITION

    registry = InMemoryToolRegistry(handlers={"read": read_handler})
    with pytest.raises(ToolRegistryError) as unavailable:
        registry.register(replace(definition(), required_capabilities=frozenset({"retrieval"})))
    assert unavailable.value.code == ToolRegistryErrorCode.CAPABILITY_UNAVAILABLE

    with pytest.raises(ToolRegistryError) as non_idempotent_process:
        replace(
            definition(permissions=frozenset({ToolPermission.EXECUTE_PROCESS})),
            idempotent=False,
        )
    assert non_idempotent_process.value.code == ToolRegistryErrorCode.INVALID_DEFINITION


@pytest.mark.asyncio
async def test_write_tool_requires_durable_approval_and_idempotency() -> None:
    tool = definition(
        name="save_knowledge",
        permissions=frozenset({ToolPermission.WRITE_KNOWLEDGE}),
    )
    run = make_run()
    denied = InMemoryToolRegistry(
        handlers={"read": read_handler}, approval_port=FakeApprovalPort(False)
    )
    denied.register(tool)
    with pytest.raises(ToolRegistryError) as captured:
        await denied.invoke(run, replace(invocation(run, tool), approval_id="approval-1"))
    assert captured.value.code == ToolRegistryErrorCode.APPROVAL_REQUIRED

    approved = InMemoryToolRegistry(
        handlers={"read": read_handler}, approval_port=FakeApprovalPort(True)
    )
    approved.register(tool)
    result = await approved.invoke(run, replace(invocation(run, tool), approval_id="approval-1"))
    assert result.run.usage.tool_calls == 1
