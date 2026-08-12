"""Read-only projection of the trusted filesystem Skill Registry."""

from __future__ import annotations

from agent_runtime import FileSystemSkillRegistry, SkillRegistryError, SkillRegistryErrorCode
from application.skills import (
    SkillBudgetView,
    SkillCatalogPort,
    SkillInvocationView,
    SkillVersionView,
    SkillView,
)


class FileSystemSkillCatalog(SkillCatalogPort):
    def __init__(
        self,
        registry: FileSystemSkillRegistry,
        *,
        include_manifest_v2: bool = False,
        visible_names: frozenset[str] | None = None,
    ) -> None:
        self._registry = registry
        self._include_manifest_v2 = include_manifest_v2
        self._visible_names = visible_names
        self._active_revisions: dict[str, int] = {}

    def set_active_revision(self, name: str, revision: int) -> None:
        self._active_revisions[name] = revision

    def list_skills(self) -> tuple[SkillView, ...]:
        return tuple(
            SkillView(
                name=name,
                active_version=self._active_version(name),
                active_revision=self._active_revisions.get(name),
                versions=tuple(
                    version
                    for version in self._registry.versions(name)
                    if self._include_manifest_v2
                    or self._registry.get(name, version).manifest.manifest_version == "1"
                ),
            )
            for name in self._names()
        )

    def list_versions(self, name: str) -> tuple[SkillVersionView, ...]:
        if self._visible_names is not None and name not in self._visible_names:
            return ()
        active_version = self._active_version(name)
        return tuple(
            self._version(name, version, active=version == active_version)
            for version in self._registry.versions(name)
            if self._include_manifest_v2
            or self._registry.get(name, version).manifest.manifest_version == "1"
        )

    def list_active_invocations(self) -> tuple[SkillInvocationView, ...]:
        invocations: list[SkillInvocationView] = []
        commands: set[str] = set()
        for name in self._names():
            try:
                package = self._registry.get(name)
            except SkillRegistryError:
                continue
            invocation = package.manifest.invocation
            if invocation is None or (
                not self._include_manifest_v2 and package.manifest.manifest_version == "2"
            ):
                continue
            command_names = invocation.commands
            if any(command in commands for command in command_names):
                raise SkillRegistryError(
                    SkillRegistryErrorCode.INVALID_MANIFEST,
                    "Active Skill invocation command or alias is not unique.",
                )
            commands.update(command_names)
            invocations.append(
                SkillInvocationView(
                    name=package.manifest.name,
                    version=package.manifest.version,
                    content_sha256=package.content_sha256,
                    command=invocation.command,
                    aliases=invocation.aliases,
                    description=invocation.trigger_summary,
                    argument_hint=invocation.argument_hint,
                    input_mode=invocation.input_mode,
                    execution_mode=invocation.execution_mode,
                    trigger_when=invocation.trigger_when,
                    trigger_avoid_when=invocation.trigger_avoid_when,
                    trigger_examples=invocation.trigger_examples,
                )
            )
        return tuple(sorted(invocations, key=lambda item: (item.command, item.name)))

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

    def _names(self) -> tuple[str, ...]:
        names = self._registry.names()
        if self._visible_names is None:
            return names
        return tuple(name for name in names if name in self._visible_names)


__all__ = ["FileSystemSkillCatalog"]
