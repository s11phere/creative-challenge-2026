"""Trusted resolution for user-selected Agent workspaces."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class WorkspacePathError(ValueError):
    """A requested directory is outside the configured workspace root."""


@dataclass(frozen=True)
class WorkspaceSelection:
    """A durable logical path and its revalidated local directory."""

    path: str
    root: Path


class WorkspaceRoot:
    """Resolve workspace selections below one configured non-link directory."""

    def __init__(self, root: Path) -> None:
        candidate = root.absolute()
        candidate.mkdir(parents=True, exist_ok=True)
        self._root = _validated_directory(candidate, error="Workspace root is unavailable")

    @property
    def root(self) -> Path:
        return self._root

    def select(self, requested_path: str) -> WorkspaceSelection:
        if (
            not isinstance(requested_path, str)
            or not requested_path.strip()
            or "\x00" in requested_path
        ):
            raise WorkspacePathError("Workspace path is required")
        try:
            requested = Path(requested_path.strip())
            candidate = requested if requested.is_absolute() else self._root / requested
            relative = candidate.absolute().relative_to(self._root)
        except ValueError as exc:
            raise WorkspacePathError(
                "Workspace must be inside the configured workspace root"
            ) from exc
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise WorkspacePathError("Workspace path is invalid")
        resolved = _validated_descendant(
            self._root,
            relative,
            error="Workspace must be an existing directory",
        )
        return WorkspaceSelection(path=relative.as_posix() or ".", root=resolved)

    def resolve(self, stored_path: str) -> WorkspaceSelection:
        """Revalidate the logical selection before every Worker execution."""
        if not isinstance(stored_path, str) or not stored_path:
            raise WorkspacePathError("Conversation has no selected workspace")
        if stored_path == ".":
            return WorkspaceSelection(path=".", root=self._root)
        if (
            "\\" in stored_path
            or ":" in stored_path
            or stored_path.startswith("/")
            or any(part in {"", ".", ".."} for part in stored_path.split("/"))
        ):
            raise WorkspacePathError("Stored workspace path is invalid")
        return self.select(stored_path)


def _validated_directory(path: Path, *, error: str) -> Path:
    if _is_link_or_junction(path) or not path.is_dir():
        raise WorkspacePathError(error)
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise WorkspacePathError(error) from exc
    if not resolved.is_dir():
        raise WorkspacePathError(error)
    return resolved


def _validated_descendant(root: Path, relative: Path, *, error: str) -> Path:
    """Resolve a workspace selection without accepting link or junction components."""
    current = root
    for part in relative.parts:
        current = current / part
        if _is_link_or_junction(current) or not current.is_dir():
            raise WorkspacePathError(error)
    try:
        resolved = current.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise WorkspacePathError(error) from exc
    if not resolved.is_dir():
        raise WorkspacePathError(error)
    return resolved


def _is_link_or_junction(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    try:
        return path.is_symlink() or (callable(junction) and bool(junction()))
    except OSError:
        return True


__all__ = ["WorkspacePathError", "WorkspaceRoot", "WorkspaceSelection"]
