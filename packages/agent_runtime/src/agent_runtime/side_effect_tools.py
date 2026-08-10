"""Approval-gated filesystem writes and allowlisted local process execution."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import subprocess
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import cast
from uuid import UUID

from domain.agent_runtime import ApprovalPort, ToolPermission

from .tools import (
    InMemoryToolRegistry,
    JSONValue,
    ToolDefinition,
    ToolExecutionContext,
    ToolHandler,
    ToolRegistryError,
    ToolRegistryErrorCode,
)

type CancellationProbe = Callable[[ToolExecutionContext], Awaitable[bool]]

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_ARGS = 128
_MAX_ARG_BYTES = 16 * 1024
_PROTECTED_PARTS = frozenset({".codex-plugin", "skill", "skills"})


@dataclass(frozen=True)
class WritableFile:
    root_name: str
    relative_path: str

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.root_name):
            raise ValueError("Writable file root name is invalid")
        _relative_parts(self.relative_path)

    @property
    def tool_path(self) -> str:
        return f"{self.root_name}/{PurePosixPath(self.relative_path).as_posix()}"


@dataclass(frozen=True)
class FileWritePolicy:
    roots: Mapping[str, Path]
    allowed_paths_by_space: Mapping[UUID, tuple[WritableFile, ...]]
    workspace_root_by_space: Mapping[UUID, str] = MappingProxyType({})
    max_write_bytes: int = 256 * 1024

    def __post_init__(self) -> None:
        if self.max_write_bytes < 1:
            raise ValueError("File write limit must be positive")
        checked_roots: dict[str, Path] = {}
        for name, root in self.roots.items():
            if not _NAME.fullmatch(name):
                raise ValueError("Trusted root name is invalid")
            checked_roots[name] = _validated_root(root)
        checked: dict[UUID, tuple[WritableFile, ...]] = {}
        for space_id, entries in self.allowed_paths_by_space.items():
            if not isinstance(space_id, UUID):
                raise ValueError("Writable path Space identity is invalid")
            paths: set[str] = set()
            for entry in entries:
                if entry.root_name not in checked_roots:
                    raise ValueError("Writable path references an unknown root")
                if entry.tool_path in paths:
                    raise ValueError("Writable paths must be unique per Space")
                _validate_relative_target(checked_roots[entry.root_name], entry.relative_path)
                paths.add(entry.tool_path)
            checked[space_id] = tuple(entries)
        checked_workspace_roots: dict[UUID, str] = {}
        for space_id, root_name in self.workspace_root_by_space.items():
            if not isinstance(space_id, UUID) or root_name not in checked_roots:
                raise ValueError("Workspace write root is invalid")
            checked_workspace_roots[space_id] = root_name
        object.__setattr__(self, "roots", MappingProxyType(checked_roots))
        object.__setattr__(self, "allowed_paths_by_space", MappingProxyType(checked))
        object.__setattr__(
            self, "workspace_root_by_space", MappingProxyType(checked_workspace_roots)
        )

    def resolve(self, *, space_id: UUID, path: str) -> Path:
        root_name, relative = self._resolve_identity(space_id=space_id, path=path)
        if _is_protected(relative):
            raise _error(ToolRegistryErrorCode.PATH_DENIED, "Protected target cannot be written")
        root = self.roots[root_name]
        return _validate_relative_target(root, relative)

    def _resolve_identity(self, *, space_id: UUID, path: str) -> tuple[str, str]:
        workspace_root = self.workspace_root_by_space.get(space_id)
        if workspace_root is not None:
            return workspace_root, _workspace_relative_path(workspace_root, path)
        root_name, relative = _tool_path(path)
        entries = self.allowed_paths_by_space.get(space_id, ())
        if not any(entry.tool_path == f"{root_name}/{relative}" for entry in entries):
            raise _error(ToolRegistryErrorCode.PATH_DENIED, "Path is not write-authorized")
        return root_name, relative

    def write(
        self,
        *,
        space_id: UUID,
        path: str,
        content: str,
        expected_sha256: str | None,
    ) -> dict[str, JSONValue]:
        raw = content.encode("utf-8")
        if len(raw) > self.max_write_bytes:
            raise _error(ToolRegistryErrorCode.FILE_TOO_LARGE, "Content exceeds the write limit")
        root_name, relative = self._resolve_identity(space_id=space_id, path=path)
        if _is_protected(relative):
            raise _error(ToolRegistryErrorCode.PATH_DENIED, "Protected target cannot be written")
        target = _validate_relative_target(self.roots[root_name], relative)
        _check_expected(target, expected_sha256)
        parent = target.parent
        _validate_directory(parent)
        temp_name: str | None = None
        try:
            fd, temp_name = tempfile.mkstemp(prefix=".agent-write-", dir=parent)
            with os.fdopen(fd, "wb") as temporary:
                temporary.write(raw)
                temporary.flush()
                os.fsync(temporary.fileno())
            # Re-check the target and parent immediately before replacement.
            _validate_relative_target(self.roots[root_name], relative)
            _check_expected(target, expected_sha256)
            os.replace(temp_name, target)
            temp_name = None
            _fsync_directory(parent)
        except ToolRegistryError:
            raise
        except OSError as exc:
            raise _error(
                ToolRegistryErrorCode.PATH_DENIED, "Atomic file replacement failed"
            ) from exc
        finally:
            if temp_name is not None:
                with suppress(OSError):
                    os.unlink(temp_name)
        digest = hashlib.sha256(raw).hexdigest()
        return {
            "trust": "untrusted",
            "path": path,
            "bytes_written": len(raw),
            "content_sha256": digest,
        }


@dataclass(frozen=True)
class ShellExecutionPolicy:
    executables: Mapping[str, Path]
    cwd_roots: Mapping[str, Path]
    allowed_cwds_by_space: Mapping[UUID, tuple[str, ...]]
    workspace_root_by_space: Mapping[UUID, str] = MappingProxyType({})
    environment: Mapping[str, str] = MappingProxyType({})
    max_output_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        if self.max_output_bytes < 1:
            raise ValueError("Shell output limit must be positive")
        checked_executables: dict[str, Path] = {}
        for name, executable in self.executables.items():
            if not name or "/" in name or "\\" in name:
                raise ValueError("Executable aliases must be simple names")
            checked_executables[name] = _validated_executable(executable)
        checked_roots = {name: _validated_root(root) for name, root in self.cwd_roots.items()}
        if any(not _NAME.fullmatch(name) for name in checked_roots):
            raise ValueError("Shell cwd root name is invalid")
        checked_cwds: dict[UUID, tuple[str, ...]] = {}
        for space_id, paths in self.allowed_cwds_by_space.items():
            if not isinstance(space_id, UUID):
                raise ValueError("Shell cwd Space identity is invalid")
            for path in paths:
                root_name, relative = _tool_path(path, allow_root=True)
                if root_name not in checked_roots:
                    raise ValueError("Shell cwd references an unknown root")
                _validate_relative_directory(checked_roots[root_name], relative)
            checked_cwds[space_id] = tuple(paths)
        checked_workspace_roots: dict[UUID, str] = {}
        for space_id, root_name in self.workspace_root_by_space.items():
            if not isinstance(space_id, UUID) or root_name not in checked_roots:
                raise ValueError("Workspace command root is invalid")
            checked_workspace_roots[space_id] = root_name
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in self.environment.items()
        ):
            raise ValueError("Shell environment must contain strings")
        object.__setattr__(self, "executables", MappingProxyType(checked_executables))
        object.__setattr__(self, "cwd_roots", MappingProxyType(checked_roots))
        object.__setattr__(self, "allowed_cwds_by_space", MappingProxyType(checked_cwds))
        object.__setattr__(
            self, "workspace_root_by_space", MappingProxyType(checked_workspace_roots)
        )
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))

    def resolve_executable(self, name: str) -> Path:
        try:
            return _validated_executable(self.executables[name])
        except KeyError as exc:
            raise _error(
                ToolRegistryErrorCode.PATH_DENIED, "Executable is not allowlisted"
            ) from exc

    def resolve_cwd(self, *, space_id: UUID, path: str) -> Path:
        workspace_root = self.workspace_root_by_space.get(space_id)
        if workspace_root is not None:
            relative = _workspace_relative_path(workspace_root, path, allow_root=True)
            return _validate_relative_directory(self.cwd_roots[workspace_root], relative)
        allowed = self.allowed_cwds_by_space.get(space_id, ())
        if path not in allowed:
            raise _error(ToolRegistryErrorCode.PATH_DENIED, "Working directory is not allowlisted")
        root_name, relative = _tool_path(path, allow_root=True)
        return _validate_relative_directory(self.cwd_roots[root_name], relative)


class SideEffectTools:
    """Handlers whose definitions are always approval-gated by the Registry."""

    def __init__(
        self,
        file_policy: FileWritePolicy,
        shell_policy: ShellExecutionPolicy,
        *,
        cancellation_probe: CancellationProbe | None = None,
    ) -> None:
        self._file_policy = file_policy
        self._shell_policy = shell_policy
        self._cancellation_probe = cancellation_probe

    def handlers(self) -> Mapping[str, ToolHandler]:
        return {"fs_write": self.fs_write, "shell_exec": self.shell_exec}

    async def fs_write(
        self, arguments: dict[str, JSONValue], context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        await self._raise_if_cancelled(context)
        path = _required_string(arguments, "path")
        content = _required_string(arguments, "content", allow_empty=True)
        expected = arguments.get("expected_sha256")
        if expected is not None and not isinstance(expected, str):
            raise _error(ToolRegistryErrorCode.INPUT_INVALID, "Expected digest is invalid")
        result = self._file_policy.write(
            space_id=context.run.space_id,
            path=path,
            content=content,
            expected_sha256=expected,
        )
        await self._raise_if_cancelled(context)
        return result

    async def shell_exec(
        self, arguments: dict[str, JSONValue], context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        await self._raise_if_cancelled(context)
        executable_name = _required_string(arguments, "executable")
        raw_argv = arguments.get("argv", [])
        cwd_name = _required_string(arguments, "cwd")
        if (
            not isinstance(raw_argv, list)
            or len(raw_argv) > _MAX_ARGS
            or any(
                not isinstance(item, str) or len(item.encode("utf-8")) > _MAX_ARG_BYTES
                for item in raw_argv
            )
        ):
            raise _error(ToolRegistryErrorCode.INPUT_INVALID, "Command arguments are invalid")
        executable = self._shell_policy.resolve_executable(executable_name)
        cwd = self._shell_policy.resolve_cwd(space_id=context.run.space_id, path=cwd_name)
        process: asyncio.subprocess.Process | None = None
        result_task: asyncio.Future[tuple[bytes, bytes, int]] | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                str(executable),
                *cast(list[str], raw_argv),
                cwd=str(cwd),
                env=dict(self._shell_policy.environment),
                stdin=subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                shell=False,
                start_new_session=(os.name != "nt"),
            )
            assert process.stdout is not None and process.stderr is not None
            result_task = asyncio.ensure_future(
                asyncio.gather(
                    _read_limited(process.stdout, self._shell_policy.max_output_bytes),
                    _read_limited(process.stderr, self._shell_policy.max_output_bytes),
                    process.wait(),
                )
            )
            assert result_task is not None
            while not result_task.done():
                await asyncio.wait({result_task}, timeout=0.05)
                await self._raise_if_cancelled(context)
            stdout, stderr, return_code = await result_task
        except ToolRegistryError:
            raise
        except FileNotFoundError as exc:
            raise _error(
                ToolRegistryErrorCode.PATH_DENIED, "Allowlisted executable is unavailable"
            ) from exc
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=1.0)
                except (TimeoutError, asyncio.CancelledError):
                    process.kill()
            if result_task is not None and not result_task.done():
                result_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await result_task
        return {
            "trust": "untrusted",
            "executable": executable_name,
            "cwd": cwd_name,
            "exit_code": int(return_code),
            "stdout": _decode_output(stdout),
            "stderr": _decode_output(stderr),
            "truncated": len(stdout) >= self._shell_policy.max_output_bytes
            or len(stderr) >= self._shell_policy.max_output_bytes,
        }

    async def _raise_if_cancelled(self, context: ToolExecutionContext) -> None:
        if self._cancellation_probe is not None and await self._cancellation_probe(context):
            raise _error(ToolRegistryErrorCode.CANCELLED, "Tool execution was cancelled")


def register_side_effect_tools(
    registry: InMemoryToolRegistry,
    *,
    timeout_seconds: float = 30.0,
) -> tuple[ToolDefinition, ToolDefinition]:
    return (
        registry.register(_fs_write_definition(timeout_seconds=timeout_seconds)),
        registry.register(_shell_exec_definition(timeout_seconds=timeout_seconds)),
    )


def create_side_effect_registry(
    file_policy: FileWritePolicy,
    shell_policy: ShellExecutionPolicy,
    *,
    approval_port: ApprovalPort | None = None,
    cancellation_probe: CancellationProbe | None = None,
    timeout_seconds: float = 30.0,
) -> InMemoryToolRegistry:
    tools = SideEffectTools(file_policy, shell_policy, cancellation_probe=cancellation_probe)
    registry = InMemoryToolRegistry(
        handlers=tools.handlers(),
        approval_port=approval_port,
    )
    register_side_effect_tools(registry, timeout_seconds=timeout_seconds)
    return registry


def _fs_write_definition(*, timeout_seconds: float) -> ToolDefinition:
    return ToolDefinition(
        name="fs_write",
        version="1.0.0",
        description="Write approved UTF-8 content to an allowlisted file atomically.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["path", "content"],
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "content": {"type": "string", "maxLength": 256 * 1024},
                "expected_sha256": {"type": ["string", "null"], "pattern": "^[0-9a-f]{64}$"},
            },
        },
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["trust", "path", "bytes_written", "content_sha256"],
            "properties": {
                "trust": {"const": "untrusted"},
                "path": {"type": "string"},
                "bytes_written": {"type": "integer", "minimum": 0},
                "content_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            },
        },
        permissions=frozenset({ToolPermission.WRITE_KNOWLEDGE}),
        handler_name="fs_write",
        timeout_seconds=timeout_seconds,
        audit_event="fs_write",
        model_visible=True,
    )


def _shell_exec_definition(*, timeout_seconds: float) -> ToolDefinition:
    return ToolDefinition(
        name="shell_exec",
        version="1.0.0",
        description="Run one allowlisted local executable without a shell.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["executable", "cwd"],
            "properties": {
                "executable": {"type": "string", "minLength": 1},
                "argv": {"type": "array", "maxItems": _MAX_ARGS, "items": {"type": "string"}},
                "cwd": {"type": "string", "minLength": 1},
            },
        },
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": [
                "trust",
                "executable",
                "cwd",
                "exit_code",
                "stdout",
                "stderr",
                "truncated",
            ],
            "properties": {
                "trust": {"const": "untrusted"},
                "executable": {"type": "string"},
                "cwd": {"type": "string"},
                "exit_code": {"type": "integer"},
                "stdout": {"type": "string"},
                "stderr": {"type": "string"},
                "truncated": {"type": "boolean"},
            },
        },
        permissions=frozenset({ToolPermission.EXECUTE_PROCESS}),
        handler_name="shell_exec",
        timeout_seconds=timeout_seconds,
        audit_event="shell_exec",
        model_visible=True,
    )


async def _read_limited(stream: asyncio.StreamReader, limit: int) -> bytes:
    chunks: list[bytes] = []
    remaining = limit
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        if remaining > 0:
            chunks.append(chunk[:remaining])
            remaining -= len(chunk[:remaining])
    return b"".join(chunks)


def _decode_output(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace").replace("\r\n", "\n")


def _check_expected(target: Path, expected_sha256: str | None) -> None:
    if expected_sha256 is None:
        return
    if _SHA256.fullmatch(expected_sha256) is None:
        raise _error(ToolRegistryErrorCode.INPUT_INVALID, "Expected digest is invalid")
    if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected_sha256:
        raise _error(ToolRegistryErrorCode.SOURCE_CHANGED, "Write target changed")


def _tool_path(path: str, *, allow_root: bool = False) -> tuple[str, str]:
    if not isinstance(path, str) or not path or "\\" in path or ":" in path:
        raise _error(ToolRegistryErrorCode.PATH_DENIED, "Path is not allowed")
    parts = tuple(PurePosixPath(path).parts)
    if (
        (len(parts) < 2 and not allow_root)
        or parts[0] in {".", ".."}
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise _error(ToolRegistryErrorCode.PATH_DENIED, "Path is not allowed")
    if not _NAME.fullmatch(parts[0]):
        raise _error(ToolRegistryErrorCode.PATH_DENIED, "Path is not allowed")
    return parts[0], "/".join(parts[1:])


def _workspace_relative_path(root_name: str, path: str, *, allow_root: bool = False) -> str:
    if (
        not isinstance(path, str)
        or not path
        or path.startswith("/")
        or "\\" in path
        or ":" in path
        or "\x00" in path
    ):
        raise _error(ToolRegistryErrorCode.PATH_DENIED, "Path is not allowed")
    normalized = path.strip()
    if normalized in {"", "."}:
        if allow_root:
            return ""
        raise _error(ToolRegistryErrorCode.PATH_DENIED, "Path is not a file")
    parts = tuple(normalized.split("/"))
    if parts and parts[0] == root_name:
        parts = parts[1:]
    if not parts and allow_root:
        return ""
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise _error(ToolRegistryErrorCode.PATH_DENIED, "Path is not allowed")
    relative = "/".join(parts)
    try:
        _relative_parts(relative)
    except ValueError as exc:
        raise _error(ToolRegistryErrorCode.PATH_DENIED, "Path is not allowed") from exc
    return relative


def _relative_parts(relative: str) -> tuple[str, ...]:
    parts = tuple(PurePosixPath(relative).parts)
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Relative path is unsafe")
    if any("\\" in part or ":" in part for part in parts):
        raise ValueError("Relative path is unsafe")
    return parts


def _validated_root(root: Path) -> Path:
    if not root.is_absolute() or _is_link_or_junction(root):
        raise ValueError("Trusted root must be an absolute non-link directory")
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("Trusted root must be a directory")
    return resolved


def _validate_relative_target(root: Path, relative: str) -> Path:
    parts = _relative_parts(relative)
    trusted_root = _validate_directory(root)
    current = trusted_root
    for part in parts[:-1]:
        current = current / part
        _validate_directory(current)
    target = trusted_root.joinpath(*parts)
    if target.exists() and _is_link_or_junction(target):
        raise _error(ToolRegistryErrorCode.PATH_DENIED, "Links and junctions are not writable")
    if target.exists() and not target.is_file():
        raise _error(ToolRegistryErrorCode.PATH_DENIED, "Write target is not a regular file")
    return target


def _validate_directory(path: Path) -> Path:
    if _is_link_or_junction(path) or not path.is_dir():
        raise _error(ToolRegistryErrorCode.PATH_DENIED, "Working directory is not trusted")
    return path.resolve(strict=True)


def _validate_relative_directory(root: Path, relative: str) -> Path:
    """Return a real directory below ``root`` while rejecting link components."""
    current = _validate_directory(root)
    if not relative:
        return current
    for part in _relative_parts(relative):
        current = current / part
        current = _validate_directory(current)
    try:
        current.relative_to(root)
    except ValueError as exc:
        raise _error(
            ToolRegistryErrorCode.PATH_DENIED, "Working directory is outside its trusted root"
        ) from exc
    return current


def _validated_executable(path: Path) -> Path:
    if not path.is_absolute() or _is_link_or_junction(path) or not path.is_file():
        raise ValueError("Executable must be an absolute regular file")
    return path.resolve(strict=True)


def _is_link_or_junction(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    try:
        return path.is_symlink() or (callable(junction) and bool(junction()))
    except OSError:
        return True


def _is_protected(relative: str) -> bool:
    parts = tuple(part.lower() for part in _relative_parts(relative))
    basename = parts[-1]
    return (
        any(part in _PROTECTED_PARTS for part in parts)
        or basename.startswith(".env")
        or any(token in basename for token in ("credential", "secret"))
        or basename.endswith((".pem", ".key", ".p12", ".pfx"))
        or basename.startswith(("system-prompt", "system_prompt", "prompt"))
    )


def _required_string(
    arguments: Mapping[str, JSONValue], name: str, *, allow_empty: bool = False
) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise _error(ToolRegistryErrorCode.INPUT_INVALID, "Tool argument is invalid")
    return value


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _error(code: ToolRegistryErrorCode, message: str) -> ToolRegistryError:
    return ToolRegistryError(code, message)


__all__ = [
    "FileWritePolicy",
    "SideEffectTools",
    "ShellExecutionPolicy",
    "WritableFile",
    "create_side_effect_registry",
    "register_side_effect_tools",
]
