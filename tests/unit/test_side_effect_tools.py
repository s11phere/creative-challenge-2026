from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import agent_runtime.side_effect_tools as side_effect_tools
import pytest
from agent_runtime import (
    FileWritePolicy,
    InMemoryToolRegistry,
    ShellExecutionPolicy,
    SideEffectTools,
    ToolInvocation,
    ToolRef,
    ToolRegistryError,
    ToolRegistryErrorCode,
    WritableFile,
    register_side_effect_tools,
)
from agent_runtime.tools import JSONValue
from domain.agent_runtime import AgentRun, AgentRunContext, RunBudget, ToolPermission

SPACE_ID = UUID("00000000-0000-4000-8000-000000000501")


class ApprovalFixture:
    def __init__(self, approved: bool = True) -> None:
        self.approved = approved

    async def is_approved_for_invocation(
        self,
        approval_id: str,
        _context: AgentRunContext,
        *,
        tool_name: str,
        tool_version: str,
        idempotency_key: str,
        input_summary: str,
    ) -> bool:
        return (
            self.approved
            and approval_id == "approval-1"
            and tool_name in {"fs_write", "shell_exec"}
            and tool_version == "1.0.0"
            and bool(idempotency_key)
            and input_summary.startswith("sha256:")
        )


def _run(permission: ToolPermission) -> AgentRun:
    return AgentRun(
        context=AgentRunContext(
            run_id=uuid4(),
            space_id=SPACE_ID,
            skill_name="side_effect_fixture",
            skill_version="1.0.0",
            skill_content_sha256="a" * 64,
            trace_id="side-effect-trace",
            caller_id="side-effect-user",
            granted_permissions=frozenset({permission}),
        ),
        budget=RunBudget(max_tool_calls=10),
    )


def _invocation(
    run: AgentRun,
    ref: ToolRef,
    arguments: dict[str, JSONValue],
    *,
    key: str,
    approval_id: str | None = "approval-1",
) -> ToolInvocation:
    return ToolInvocation(
        ref=ref,
        arguments=arguments,
        allowed_tools=frozenset({ToolRef("fs_write", "1.0.0"), ToolRef("shell_exec", "1.0.0")}),
        granted_permissions=run.context.granted_permissions,
        resource_space_id=run.context.space_id,
        idempotency_key=key,
        approval_id=approval_id,
    )


def _registry(root: Path, *, approval: ApprovalFixture | None = None) -> InMemoryToolRegistry:
    (root / "skills").mkdir(exist_ok=True)
    file_policy = FileWritePolicy(
        roots={"workspace": root},
        allowed_paths_by_space={
            SPACE_ID: (
                WritableFile("workspace", "note.txt"),
                WritableFile("workspace", ".env"),
                WritableFile("workspace", "skills/package.txt"),
            )
        },
    )
    shell_policy = ShellExecutionPolicy(
        executables={"python": Path(sys.executable)},
        cwd_roots={"workspace": root},
        allowed_cwds_by_space={SPACE_ID: ("workspace",)},
        environment={"PYTHONIOENCODING": "utf-8"},
        max_output_bytes=16,
    )
    tools = SideEffectTools(file_policy, shell_policy)
    registry = InMemoryToolRegistry(handlers=tools.handlers(), approval_port=approval)
    register_side_effect_tools(registry, timeout_seconds=0.25)
    return registry


def _workspace_registry(root: Path) -> InMemoryToolRegistry:
    tools = SideEffectTools(
        FileWritePolicy(
            roots={"workspace": root},
            allowed_paths_by_space={},
            workspace_root_by_space={SPACE_ID: "workspace"},
        ),
        ShellExecutionPolicy(
            executables={"python": Path(sys.executable)},
            cwd_roots={"workspace": root},
            allowed_cwds_by_space={},
            workspace_root_by_space={SPACE_ID: "workspace"},
            environment={"PYTHONIOENCODING": "utf-8"},
        ),
    )
    registry = InMemoryToolRegistry(handlers=tools.handlers(), approval_port=ApprovalFixture())
    register_side_effect_tools(registry)
    return registry


@pytest.mark.asyncio
async def test_fs_write_requires_approval_and_atomically_replays_idempotently(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "skills").mkdir()
    registry = _registry(root, approval=ApprovalFixture())
    run = _run(ToolPermission.WRITE_KNOWLEDGE)
    request = _invocation(
        run,
        ToolRef("fs_write", "1.0.0"),
        {"path": "workspace/note.txt", "content": "atomic content"},
        key="write-1",
    )

    first, second = await asyncio.gather(
        registry.invoke(run, request), registry.invoke(run, request)
    )
    assert first == second
    assert (root / "note.txt").read_text(encoding="utf-8") == "atomic content"
    assert cast(dict[str, JSONValue], first.output)["trust"] == "untrusted"

    with pytest.raises(ToolRegistryError) as conflict:
        await registry.invoke(
            run,
            _invocation(
                run,
                ToolRef("fs_write", "1.0.0"),
                {"path": "workspace/note.txt", "content": "different"},
                key="write-1",
            ),
        )
    assert conflict.value.code is ToolRegistryErrorCode.IDEMPOTENCY_CONFLICT


@pytest.mark.asyncio
async def test_fs_write_rejects_missing_expired_approval_protected_targets_and_races(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    run = _run(ToolPermission.WRITE_KNOWLEDGE)
    without_approval = _registry(root, approval=ApprovalFixture())
    with pytest.raises(ToolRegistryError) as absent:
        await without_approval.invoke(
            run,
            _invocation(
                run,
                ToolRef("fs_write", "1.0.0"),
                {"path": "workspace/note.txt", "content": "absent"},
                key="write-absent",
                approval_id=None,
            ),
        )
    assert absent.value.code is ToolRegistryErrorCode.APPROVAL_REQUIRED

    denied = _registry(root, approval=ApprovalFixture(False))
    with pytest.raises(ToolRegistryError) as missing:
        await denied.invoke(
            run,
            _invocation(
                run,
                ToolRef("fs_write", "1.0.0"),
                {"path": "workspace/note.txt", "content": "denied"},
                key="write-denied",
            ),
        )
    assert missing.value.code is ToolRegistryErrorCode.APPROVAL_REQUIRED

    registry = _registry(root, approval=ApprovalFixture())
    with pytest.raises(ToolRegistryError) as protected:
        await registry.invoke(
            run,
            _invocation(
                run,
                ToolRef("fs_write", "1.0.0"),
                {"path": "workspace/.env", "content": "SECRET=never"},
                key="write-protected",
            ),
        )
    assert protected.value.code is ToolRegistryErrorCode.PATH_DENIED

    with pytest.raises(ToolRegistryError) as skill:
        await registry.invoke(
            run,
            _invocation(
                run,
                ToolRef("fs_write", "1.0.0"),
                {"path": "workspace/skills/package.txt", "content": "no"},
                key="write-skill",
            ),
        )
    assert skill.value.code is ToolRegistryErrorCode.PATH_DENIED

    target = root / "note.txt"
    target.write_text("old", encoding="utf-8")
    with pytest.raises(ToolRegistryError) as changed:
        await registry.invoke(
            run,
            _invocation(
                run,
                ToolRef("fs_write", "1.0.0"),
                {
                    "path": "workspace/note.txt",
                    "content": "new",
                    "expected_sha256": hashlib.sha256(b"other").hexdigest(),
                },
                key="write-race",
            ),
        )
    assert changed.value.code is ToolRegistryErrorCode.SOURCE_CHANGED


@pytest.mark.asyncio
async def test_shell_exec_is_allowlisted_bounded_and_approval_gated(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    registry = _registry(root, approval=ApprovalFixture())
    run = _run(ToolPermission.EXECUTE_PROCESS)

    result = await registry.invoke(
        run,
        _invocation(
            run,
            ToolRef("shell_exec", "1.0.0"),
            {"executable": "python", "cwd": "workspace", "argv": ["-c", "print('ok')"]},
            key="shell-1",
        ),
    )
    output = cast(dict[str, JSONValue], result.output)
    assert output["trust"] == "untrusted"
    assert output["exit_code"] == 0
    assert output["stdout"] == "ok\n"

    with pytest.raises(ToolRegistryError) as unknown:
        await registry.invoke(
            run,
            _invocation(
                run,
                ToolRef("shell_exec", "1.0.0"),
                {"executable": "cmd", "cwd": "workspace", "argv": ["/c", "echo unsafe"]},
                key="shell-unknown",
            ),
        )
    assert unknown.value.code is ToolRegistryErrorCode.PATH_DENIED

    truncated = await registry.invoke(
        run,
        _invocation(
            run,
            ToolRef("shell_exec", "1.0.0"),
            {"executable": "python", "cwd": "workspace", "argv": ["-c", "print('x' * 200)"]},
            key="shell-truncated",
        ),
    )
    truncated_output = cast(dict[str, JSONValue], truncated.output)
    assert truncated_output["truncated"] is True
    assert len(cast(str, truncated_output["stdout"]).encode("utf-8")) <= 16


@pytest.mark.asyncio
async def test_side_effect_tools_are_scoped_to_relative_workspace_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "workspace"
    subdirectory = root / "subdir"
    subdirectory.mkdir(parents=True)
    linked = root / "linked"
    linked.mkdir()
    registry = _workspace_registry(root)

    write_run = _run(ToolPermission.WRITE_KNOWLEDGE)
    await registry.invoke(
        write_run,
        _invocation(
            write_run,
            ToolRef("fs_write", "1.0.0"),
            {"path": "subdir/note.txt", "content": "workspace only"},
            key="workspace-write",
        ),
    )
    assert (subdirectory / "note.txt").read_text(encoding="utf-8") == "workspace only"
    with pytest.raises(ToolRegistryError) as protected:
        await registry.invoke(
            write_run,
            _invocation(
                write_run,
                ToolRef("fs_write", "1.0.0"),
                {"path": ".env", "content": "TOKEN=never"},
                key="workspace-protected",
            ),
        )
    assert protected.value.code is ToolRegistryErrorCode.PATH_DENIED
    with pytest.raises(ToolRegistryError) as dotted_path:
        await registry.invoke(
            write_run,
            _invocation(
                write_run,
                ToolRef("fs_write", "1.0.0"),
                {"path": "./subdir/other.txt", "content": "no"},
                key="workspace-dotted-path",
            ),
        )
    assert dotted_path.value.code is ToolRegistryErrorCode.PATH_DENIED

    command_run = _run(ToolPermission.EXECUTE_PROCESS)
    command = await registry.invoke(
        command_run,
        _invocation(
            command_run,
            ToolRef("shell_exec", "1.0.0"),
            {"executable": "python", "cwd": ".", "argv": ["-c", "print('workspace')"]},
            key="workspace-command-root",
        ),
    )
    assert cast(dict[str, JSONValue], command.output)["stdout"] == "workspace\n"
    nested = await registry.invoke(
        command_run,
        _invocation(
            command_run,
            ToolRef("shell_exec", "1.0.0"),
            {"executable": "python", "cwd": "subdir", "argv": ["-c", "print('nested')"]},
            key="workspace-command-nested",
        ),
    )
    assert cast(dict[str, JSONValue], nested.output)["stdout"] == "nested\n"

    monkeypatch.setattr(
        side_effect_tools, "_is_link_or_junction", lambda path: path.name == "linked"
    )
    with pytest.raises(ToolRegistryError) as linked_cwd:
        await registry.invoke(
            command_run,
            _invocation(
                command_run,
                ToolRef("shell_exec", "1.0.0"),
                {"executable": "python", "cwd": "linked", "argv": ["-c", "print('no')"]},
                key="workspace-command-linked",
            ),
        )
    assert linked_cwd.value.code is ToolRegistryErrorCode.PATH_DENIED


@pytest.mark.asyncio
async def test_shell_exec_timeout_and_cancellation_do_not_leave_a_process(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    registry = _registry(root, approval=ApprovalFixture())
    run = _run(ToolPermission.EXECUTE_PROCESS)
    with pytest.raises(ToolRegistryError) as timed_out:
        await registry.invoke(
            run,
            _invocation(
                run,
                ToolRef("shell_exec", "1.0.0"),
                {
                    "executable": "python",
                    "cwd": "workspace",
                    "argv": ["-c", "import time; time.sleep(2)"],
                },
                key="shell-timeout",
            ),
        )
    assert timed_out.value.code is ToolRegistryErrorCode.TIMEOUT

    tools = SideEffectTools(
        FileWritePolicy(
            roots={"workspace": root},
            allowed_paths_by_space={SPACE_ID: (WritableFile("workspace", "note.txt"),)},
        ),
        ShellExecutionPolicy(
            executables={"python": Path(sys.executable)},
            cwd_roots={"workspace": root},
            allowed_cwds_by_space={SPACE_ID: ("workspace",)},
        ),
        cancellation_probe=lambda _context: asyncio.sleep(0, result=True),
    )
    cancelled_registry = InMemoryToolRegistry(
        handlers=tools.handlers(), approval_port=ApprovalFixture()
    )
    register_side_effect_tools(cancelled_registry)
    with pytest.raises(ToolRegistryError) as cancelled:
        await cancelled_registry.invoke(
            run,
            _invocation(
                run,
                ToolRef("shell_exec", "1.0.0"),
                {"executable": "python", "cwd": "workspace", "argv": ["-c", "print('no')"]},
                key="shell-cancelled",
            ),
        )
    assert cancelled.value.code is ToolRegistryErrorCode.CANCELLED


def test_write_policy_rejects_linked_roots_and_tool_paths(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    with pytest.raises(ValueError):
        FileWritePolicy(
            roots={"workspace": root},
            allowed_paths_by_space={SPACE_ID: (WritableFile("workspace", "../outside.txt"),)},
        )
