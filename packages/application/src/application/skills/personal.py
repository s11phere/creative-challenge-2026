"""Personal (user-owned, low-trust) Skill CRUD and activation use cases.

Personal Skills only compose declarative workflows and already-registered
handlers/tools — never new Python behavior (ADR-018). All writes are validated
by the ``PersonalSkillRegistry`` through the shared manifest/schema/eval checks;
this store adds the durable activation pointer via ``skill_activations``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from agent_runtime import PersonalSkillRegistry, SkillPackage, SkillRegistryError

from .catalog import SkillActivation, SkillActivationStore, SkillBudgetView, SkillInvocationView


class PersonalSkillErrorCode(StrEnum):
    NOT_FOUND = "SKILL_NOT_FOUND"
    INVALID = "SKILL_INVALID"
    NAME_CONFLICT = "SKILL_NAME_CONFLICT"
    ACTIVE = "SKILL_ACTIVE"


class PersonalSkillError(Exception):
    def __init__(self, code: PersonalSkillErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PersonalSkillView:
    """Safe, body-free projection of one personal Skill for the transport layer."""

    name: str
    version: str
    content_sha256: str
    description: str
    active: bool
    permissions: tuple[str, ...]
    required_capabilities: tuple[str, ...]
    budget: SkillBudgetView
    invocation: SkillInvocationView | None = None


class PersonalSkillStore:
    """CRUD + activation over the writable personal-Skill registry."""

    def __init__(
        self,
        *,
        registry: PersonalSkillRegistry,
        store: SkillActivationStore,
    ) -> None:
        self._registry = registry
        self._store = store

    async def persist_activation(self, package: SkillPackage, *, active: bool = True) -> None:
        """Durably record the activation state for one package."""
        activation = SkillActivation(
            name=package.manifest.name,
            version=package.manifest.version,
            content_sha256=package.content_sha256,
            revision=1,
            active=active,
        )
        existing = await self._store.get(activation.name)
        if existing is None:
            await self._store.initialize(activation)
            return
        updated = await self._store.compare_and_set(
            SkillActivation(
                name=activation.name,
                version=activation.version,
                content_sha256=activation.content_sha256,
                revision=existing.revision + 1,
                active=active,
            ),
            expected_revision=existing.revision,
        )
        if updated is None:
            raise PersonalSkillError(
                PersonalSkillErrorCode.INVALID,
                "Personal Skill activation changed concurrently.",
            )

    async def create(self, name: str, files: Mapping[str, str]) -> PersonalSkillView:
        package = self._registry.create_personal(name, dict(files))
        self._registry.activate(package.manifest.name, package.manifest.version)
        await self.persist_activation(package)
        return self._view(package)

    async def update(self, name: str, files: Mapping[str, str]) -> PersonalSkillView:
        """Update an installed personal Skill; refresh the durable activation
        pointer when the Skill is active so the pinned content stays consistent."""
        package = self._registry.update_personal(name, dict(files))
        if self._is_active(name):
            await self.persist_activation(package)
        return self._view(package)

    async def delete(self, name: str) -> None:
        """Remove an installed personal Skill and its durable activation pointer."""
        self._registry.delete_personal(name)
        await self._store.remove(name)

    def list(self) -> tuple[PersonalSkillView, ...]:
        return tuple(
            sorted(
                (
                    self._view(self._active_or_only_package(name))
                    for name in self._registry.personal_names()
                ),
                key=lambda view: view.name,
            )
        )

    def get(self, name: str) -> PersonalSkillView:
        if not self._registry.is_personal(name):
            raise PersonalSkillError(
                PersonalSkillErrorCode.NOT_FOUND, "Personal Skill is not installed."
            )
        return self._view(self._active_or_only_package(name))

    async def activate(self, name: str) -> PersonalSkillView:
        if not self._registry.is_personal(name):
            raise PersonalSkillError(
                PersonalSkillErrorCode.NOT_FOUND, "Personal Skill is not installed."
            )
        versions = self._registry.versions(name)
        if not versions:
            raise PersonalSkillError(
                PersonalSkillErrorCode.NOT_FOUND, "Personal Skill is not installed."
            )
        version = versions[-1]
        package = self._registry.get(name, version)
        try:
            self._registry.activate(name, version)
        except SkillRegistryError as exc:
            raise PersonalSkillError(PersonalSkillErrorCode.NAME_CONFLICT, str(exc)) from exc
        await self.persist_activation(package)
        return self._view(package)

    async def set_active(self, name: str, active: bool) -> PersonalSkillView:
        """Toggle one personal Skill without restarting the Assistant process."""
        if active:
            return await self.activate(name)
        if not self._registry.is_personal(name):
            raise PersonalSkillError(
                PersonalSkillErrorCode.NOT_FOUND, "Personal Skill is not installed."
            )
        package = self._active_or_only_package(name)
        self._registry.deactivate(name)
        await self.persist_activation(package, active=False)
        return self._view(package)

    def _active_or_only_package(self, name: str) -> SkillPackage:
        try:
            return self._registry.get(name)
        except SkillRegistryError:
            versions = self._registry.versions(name)
            if not versions:
                raise PersonalSkillError(
                    PersonalSkillErrorCode.NOT_FOUND, "Personal Skill is not installed."
                ) from None
            return self._registry.get(name, versions[-1])

    def _view(self, package: SkillPackage) -> PersonalSkillView:
        manifest = package.manifest
        invocation = None
        if manifest.invocation is not None:
            invocation = SkillInvocationView(
                name=manifest.name,
                version=manifest.version,
                content_sha256=package.content_sha256,
                command=manifest.invocation.command,
                aliases=manifest.invocation.aliases,
                description=manifest.invocation.trigger_summary,
                argument_hint=manifest.invocation.argument_hint,
                input_mode=manifest.invocation.input_mode,
                execution_mode=manifest.invocation.execution_mode,
            )
        budget = manifest.budgets
        return PersonalSkillView(
            name=manifest.name,
            version=manifest.version,
            content_sha256=package.content_sha256,
            description=manifest.description,
            active=self._is_active(manifest.name),
            permissions=tuple(sorted(permission.value for permission in manifest.permissions)),
            required_capabilities=tuple(sorted(manifest.required_capabilities)),
            budget=SkillBudgetView(
                max_steps=budget.max_steps,
                max_tool_calls=budget.max_tool_calls,
                max_input_tokens=budget.max_input_tokens,
                max_output_tokens=budget.max_output_tokens,
                timeout_seconds=budget.timeout_seconds,
            ),
            invocation=invocation,
        )

    def _is_active(self, name: str) -> bool:
        try:
            self._registry.active_version(name)
            return True
        except SkillRegistryError:
            return False


__all__ = [
    "PersonalSkillError",
    "PersonalSkillErrorCode",
    "PersonalSkillStore",
    "PersonalSkillView",
]
