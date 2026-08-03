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
    active_revision: int | None
    versions: tuple[str, ...]


class SkillCatalogPort(Protocol):
    def list_skills(self) -> tuple[SkillView, ...]: ...

    def list_versions(self, name: str) -> tuple[SkillVersionView, ...]: ...


@dataclass(frozen=True)
class SkillActivation:
    name: str
    version: str
    content_sha256: str
    revision: int


class SkillActivationStore(Protocol):
    async def initialize(self, activation: SkillActivation) -> SkillActivation: ...

    async def get(self, name: str) -> SkillActivation | None: ...

    async def compare_and_set(
        self, activation: SkillActivation, *, expected_revision: int
    ) -> SkillActivation | None: ...


__all__ = [
    "SkillActivation",
    "SkillActivationStore",
    "SkillBudgetView",
    "SkillCatalogPort",
    "SkillVersionView",
    "SkillView",
]
