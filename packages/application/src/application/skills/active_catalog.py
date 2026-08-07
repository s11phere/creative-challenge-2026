"""Application service for resolving the active, safe Assistant Skill catalog."""

from __future__ import annotations

from agent_runtime import SkillRegistryError

from .catalog import SkillCatalogPort, SkillInvocationView
from .lifecycle import SkillLifecycleError, SkillLifecycleErrorCode, SkillLifecycleService


class ActiveSkillCatalogService:
    """Synchronize durable active pointers before exposing invocation metadata."""

    def __init__(self, *, catalog: SkillCatalogPort, lifecycle: SkillLifecycleService) -> None:
        self._catalog = catalog
        self._lifecycle = lifecycle

    async def list(self) -> tuple[SkillInvocationView, ...]:
        for skill in self._catalog.list_skills():
            try:
                await self._lifecycle.current(skill.name)
            except SkillLifecycleError:
                continue
        try:
            return self._catalog.list_active_invocations()
        except SkillRegistryError as exc:
            raise SkillLifecycleError(
                SkillLifecycleErrorCode.INVALID,
                "Active Skill catalog is invalid.",
            ) from exc

    async def find(self, name: str) -> SkillInvocationView | None:
        entries = await self.list()
        return next((entry for entry in entries if entry.name == name), None)


__all__ = ["ActiveSkillCatalogService"]
