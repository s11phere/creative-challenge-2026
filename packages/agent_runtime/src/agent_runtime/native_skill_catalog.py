"""Trusted progressive Skill routes for the native Tool-use harness."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from .tools import ToolRef

_MAX_ROUTE_DESCRIPTION_CHARS = 280
_MAX_COMMAND_CHARS = 32
_SKILL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_SEMVER_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")


@dataclass(frozen=True)
class NativeSkillPin:
    """Minimal immutable Skill identity persisted by a v2 loop."""

    name: str
    version: str
    content_sha256: str

    def __post_init__(self) -> None:
        if (
            not _SKILL_NAME_PATTERN.fullmatch(self.name)
            or not _SEMVER_PATTERN.fullmatch(self.version)
            or not _SHA256_PATTERN.fullmatch(self.content_sha256)
        ):
            raise ValueError("Native Skill pin is incomplete")


@dataclass(frozen=True)
class NativeSkillRoute:
    """Body-free route metadata that is safe for the initial model context."""

    pin: NativeSkillPin
    description: str
    command: str
    adapter_available: bool

    def __post_init__(self) -> None:
        if not self.description.strip() or len(self.description) > _MAX_ROUTE_DESCRIPTION_CHARS:
            raise ValueError("Native Skill route description is invalid")
        if not self.command.strip() or len(self.command) > _MAX_COMMAND_CHARS:
            raise ValueError("Native Skill route command is invalid")


@dataclass(frozen=True)
class NativeSkillSelection:
    """One server-validated selected Skill and its now-visible Tool surface."""

    route: NativeSkillRoute
    instructions: str
    allowed_tools: tuple[ToolRef, ...]

    def __post_init__(self) -> None:
        if not self.route.adapter_available:
            raise ValueError("Native Skill selection requires a Runtime adapter")
        if not self.instructions.strip():
            raise ValueError("Native Skill instructions must not be blank")
        names = tuple(ref.name for ref in self.allowed_tools)
        if len(names) != len(set(names)):
            raise ValueError("Native Skill Tools must have unique names")

    @property
    def pin(self) -> NativeSkillPin:
        return self.route.pin


class NativeSkillCatalog(Protocol):
    """Server-owned Skill routing and pin validation for one v2 Run."""

    def list_routes(self) -> tuple[NativeSkillRoute, ...]: ...

    def select(self, name: str) -> NativeSkillSelection: ...

    def resolve(self, pin: NativeSkillPin) -> NativeSkillSelection: ...


__all__ = [
    "NativeSkillCatalog",
    "NativeSkillPin",
    "NativeSkillRoute",
    "NativeSkillSelection",
]
