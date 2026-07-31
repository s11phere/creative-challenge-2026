"""Read-only Skill catalog contracts for transport and Web projections."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SkillBudgetView:
    max_steps: int
    max_tool_calls: int
    max_input_tokens: int
    max_output_tokens: int
    timeout_seconds: int


@dataclass(frozen=True)
class SkillVersionView:
    name: str
    version: str
    content_sha256: str
    description: str
    active: bool
    permissions: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    budget: SkillBudgetView


@dataclass(frozen=True)
class SkillView:
    name: str
    active_version: str | None
    versions: tuple[str, ...]


class SkillCatalogPort(Protocol):
    def list_skills(self) -> tuple[SkillView, ...]: ...

    def list_versions(self, name: str) -> tuple[SkillVersionView, ...]: ...


__all__ = ["SkillBudgetView", "SkillCatalogPort", "SkillVersionView", "SkillView"]
