"""Read-only projection of the trusted filesystem Skill Registry."""

from __future__ import annotations

from agent_runtime import FileSystemSkillRegistry, SkillRegistryError
from application.skills import SkillBudgetView, SkillCatalogPort, SkillVersionView, SkillView


class FileSystemSkillCatalog(SkillCatalogPort):
    def __init__(self, registry: FileSystemSkillRegistry) -> None:
        self._registry = registry

    def list_skills(self) -> tuple[SkillView, ...]:
        return tuple(
            SkillView(
                name=name,
                active_version=self._active_version(name),
                versions=self._registry.versions(name),
            )
            for name in self._registry.names()
        )

    def list_versions(self, name: str) -> tuple[SkillVersionView, ...]:
        active_version = self._active_version(name)
        return tuple(
            self._version(name, version, active=version == active_version)
            for version in self._registry.versions(name)
        )

    def _version(self, name: str, version: str, *, active: bool) -> SkillVersionView:
        package = self._registry.get(name, version)
        manifest = package.manifest
        budget = manifest.budgets
        return SkillVersionView(
            name=name,
            version=version,
            content_sha256=package.content_sha256,
            description=manifest.description,
            active=active,
            permissions=tuple(sorted(permission.value for permission in manifest.permissions)),
            required_capabilities=tuple(sorted(manifest.required_capabilities)),
            budget=SkillBudgetView(
                max_steps=budget.max_steps,
                max_tool_calls=budget.max_tool_calls,
                max_input_tokens=budget.max_input_tokens,
                max_output_tokens=budget.max_output_tokens,
                timeout_seconds=budget.timeout_seconds,
            ),
        )

    def _active_version(self, name: str) -> str | None:
        try:
            return self._registry.active_version(name)
        except SkillRegistryError:
            return None


__all__ = ["FileSystemSkillCatalog"]
