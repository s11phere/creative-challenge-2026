"""Read-only projection of the trusted filesystem Skill Registry."""

from __future__ import annotations

from collections.abc import Mapping

from agent_runtime import (
    FileSystemSkillRegistry,
    NativeSkillCatalog,
    NativeSkillPin,
    NativeSkillRoute,
    NativeSkillSelection,
    SkillRegistryError,
    SkillRegistryErrorCode,
    ToolRef,
)
from application.skills.catalog import (
    SkillBudgetView,
    SkillCatalogPort,
    SkillInvocationView,
    SkillVersionView,
    SkillView,
)

INTERNAL_RUNTIME_SKILL_NAMES = frozenset({"assistant_agent"})


def user_manageable_skill_names(registry: FileSystemSkillRegistry) -> frozenset[str]:
    """Return built-in Skills that are part of the user-facing catalog."""
    return frozenset(registry.builtin_names()).difference(INTERNAL_RUNTIME_SKILL_NAMES)


def user_manageable_skill_versions(registry: FileSystemSkillRegistry) -> dict[str, str]:
    """Use the newest installed version as each user-facing Skill's default pointer."""
    return {name: registry.versions(name)[-1] for name in user_manageable_skill_names(registry)}


class FileSystemSkillCatalog(SkillCatalogPort):
    def __init__(
        self,
        registry: FileSystemSkillRegistry,
        *,
        include_manifest_v2: bool = False,
        visible_names: frozenset[str] | None = None,
        excluded_names: frozenset[str] = frozenset(),
    ) -> None:
        self._registry = registry
        self._include_manifest_v2 = include_manifest_v2
        self._visible_names = visible_names
        self._excluded_names = excluded_names
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
        if name in self._excluded_names or (
            self._visible_names is not None and name not in self._visible_names
        ):
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
        return tuple(
            name
            for name in names
            if name not in self._excluded_names
            and (self._visible_names is None or name in self._visible_names)
        )


class FileSystemNativeSkillCatalog(NativeSkillCatalog):
    """Progressive native-runtime adapter over active filesystem Skill routes.

    The adapter mapping is deployment-owned. It intentionally makes a Skill
    visible in the thin route catalog even when its runtime Tool adapter has
    not been registered, but rejects selecting that route through the model.
    """

    def __init__(
        self,
        registry: FileSystemSkillRegistry,
        catalog: SkillCatalogPort,
        *,
        tool_adapters: Mapping[ToolRef, tuple[ToolRef, ...]],
        prompt_overrides: Mapping[str, str] | None = None,
    ) -> None:
        self._registry = registry
        self._catalog = catalog
        self._tool_adapters = dict(tool_adapters)
        self._prompt_overrides = dict(prompt_overrides or {})

    def list_routes(self) -> tuple[NativeSkillRoute, ...]:
        return tuple(self._route(item) for item in self._catalog.list_active_invocations())

    def select(self, name: str) -> NativeSkillSelection:
        routes = {route.pin.name: route for route in self.list_routes()}
        route = routes.get(name)
        if route is None or not route.adapter_available:
            raise ValueError("Skill is not selectable through the native runtime")
        return self._selection(route)

    def resolve(self, pin: NativeSkillPin) -> NativeSkillSelection:
        try:
            pinned = self._registry.pin(pin.name, pin.version)
        except SkillRegistryError as exc:
            raise ValueError("Selected Skill is no longer available") from exc
        if pinned.content_sha256 != pin.content_sha256:
            raise ValueError("Selected Skill pin no longer matches its installed version")
        invocation = self._registry.validate_pin(pinned).manifest.invocation
        if invocation is None:
            raise ValueError("Selected Skill has no native invocation route")
        route = NativeSkillRoute(
            pin=pin,
            description=invocation.trigger_summary,
            command=invocation.command,
            adapter_available=ToolRef(pin.name, pin.version) in self._tool_adapters,
        )
        if not route.adapter_available:
            raise ValueError("Selected Skill no longer has a native runtime adapter")
        return self._selection(route)

    def _route(self, item: SkillInvocationView) -> NativeSkillRoute:
        return NativeSkillRoute(
            pin=NativeSkillPin(
                name=item.name,
                version=item.version,
                content_sha256=item.content_sha256,
            ),
            description=item.description,
            command=item.command,
            adapter_available=ToolRef(item.name, item.version) in self._tool_adapters,
        )

    def _selection(self, route: NativeSkillRoute) -> NativeSkillSelection:
        try:
            pinned = self._registry.pin(route.pin.name, route.pin.version)
            if pinned.content_sha256 != route.pin.content_sha256:
                raise ValueError("Selected Skill pin no longer matches its active route")
            instructions = self._prompt_overrides.get(route.pin.name)
            if instructions is None:
                instructions = self._registry.prompt_instructions(pinned)
            if not instructions.strip():
                raise ValueError("Selected Skill instructions cannot be blank")
            return NativeSkillSelection(
                route=route,
                instructions=instructions,
                allowed_tools=self._tool_adapters[ToolRef(route.pin.name, route.pin.version)],
            )
        except (KeyError, SkillRegistryError) as exc:
            raise ValueError("Selected Skill cannot be loaded through the native runtime") from exc


__all__ = [
    "INTERNAL_RUNTIME_SKILL_NAMES",
    "FileSystemNativeSkillCatalog",
    "FileSystemSkillCatalog",
    "user_manageable_skill_names",
    "user_manageable_skill_versions",
]
