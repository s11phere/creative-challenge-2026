"""Persisted Skill activation orchestration over the trusted registry."""

from __future__ import annotations

import asyncio
from enum import StrEnum

from agent_runtime import FileSystemSkillRegistry, SkillRegistryError

from .catalog import SkillActivation, SkillActivationStore


class SkillLifecycleErrorCode(StrEnum):
    NOT_FOUND = "SKILL_NOT_FOUND"
    INVALID = "SKILL_INVALID"
    INACTIVE = "SKILL_NOT_ACTIVE"


class SkillLifecycleError(Exception):
    def __init__(self, code: SkillLifecycleErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class SkillLifecycleService:
    """Keep a process-local registry synchronized with the durable active pointer."""

    def __init__(
        self,
        *,
        registry: FileSystemSkillRegistry,
        store: SkillActivationStore,
        defaults: dict[str, str],
    ) -> None:
        self._registry = registry
        self._store = store
        self._defaults = defaults
        self._lock = asyncio.Lock()

    async def current(self, name: str) -> SkillActivation:
        activation = await self.status(name)
        if not activation.active:
            raise SkillLifecycleError(SkillLifecycleErrorCode.INACTIVE, "Skill is not active.")
        return activation

    async def status(self, name: str) -> SkillActivation:
        """Return and apply the durable state, including a disabled Skill."""
        async with self._lock:
            desired = self._default_activation(name)
            activation = await self._store.get(name)
            if activation is None:
                activation = await self._store.initialize(desired)
            elif (
                activation.version != desired.version
                or activation.content_sha256 != desired.content_sha256
            ):
                updated = await self._store.compare_and_set(
                    SkillActivation(
                        name=desired.name,
                        version=desired.version,
                        content_sha256=desired.content_sha256,
                        revision=activation.revision + 1,
                        active=activation.active,
                    ),
                    expected_revision=activation.revision,
                )
                if updated is None:
                    activation = await self._store.get(name)
                    if activation is None:
                        raise SkillLifecycleError(
                            SkillLifecycleErrorCode.INVALID,
                            "Skill activation disappeared during update.",
                        )
                else:
                    activation = updated
            self._apply(activation)
            return activation

    async def set_active(self, name: str, active: bool) -> SkillActivation:
        """Persist and apply one activation toggle without restarting a process."""
        async with self._lock:
            desired = self._default_activation(name)
            current = await self._store.get(name)
            if current is None:
                current = await self._store.initialize(
                    SkillActivation(
                        name=desired.name,
                        version=desired.version,
                        content_sha256=desired.content_sha256,
                        revision=1,
                        active=active,
                    )
                )
            if (
                current.version == desired.version
                and current.content_sha256 == desired.content_sha256
                and current.active == active
            ):
                activation = current
            else:
                updated = await self._store.compare_and_set(
                    SkillActivation(
                        name=desired.name,
                        version=desired.version,
                        content_sha256=desired.content_sha256,
                        revision=current.revision + 1,
                        active=active,
                    ),
                    expected_revision=current.revision,
                )
                if updated is None:
                    raise SkillLifecycleError(
                        SkillLifecycleErrorCode.INVALID,
                        "Skill activation changed concurrently.",
                    )
                activation = updated
            self._apply(activation)
            return activation

    def apply_to(self, registry: FileSystemSkillRegistry, activation: SkillActivation) -> None:
        """Apply a durable toggle to a second in-process Skill registry."""
        self._apply(activation, registry=registry)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._defaults)

    def _default_activation(self, name: str) -> SkillActivation:
        default = self._defaults.get(name)
        if default is None:
            raise SkillLifecycleError(SkillLifecycleErrorCode.NOT_FOUND, "Skill is not installed.")
        return self._activation(name, default, 1)

    def _activation(self, name: str, version: str, revision: int) -> SkillActivation:
        try:
            pin = self._registry.pin(name, version)
        except SkillRegistryError as exc:
            raise SkillLifecycleError(
                SkillLifecycleErrorCode.NOT_FOUND, "Requested Skill version is not installed."
            ) from exc
        return SkillActivation(
            name=pin.name,
            version=pin.version,
            content_sha256=pin.content_sha256,
            revision=revision,
        )

    def _apply(
        self, activation: SkillActivation, *, registry: FileSystemSkillRegistry | None = None
    ) -> None:
        target = registry or self._registry
        try:
            pin = target.pin(activation.name, activation.version)
            if pin.content_sha256 != activation.content_sha256:
                raise SkillLifecycleError(
                    SkillLifecycleErrorCode.INVALID,
                    "Persisted Skill identity does not match the trusted package.",
                )
            if activation.active:
                target.activate(activation.name, activation.version)
            else:
                target.deactivate(activation.name)
        except SkillRegistryError as exc:
            raise SkillLifecycleError(
                SkillLifecycleErrorCode.INVALID,
                "Persisted Skill activation cannot be loaded from the trusted root.",
            ) from exc


class SkillActivationService:
    """Keep QA and Assistant registries synchronized with one live toggle."""

    def __init__(
        self,
        *,
        lifecycle: SkillLifecycleService,
        assistant_registry: FileSystemSkillRegistry,
    ) -> None:
        self._lifecycle = lifecycle
        self._assistant_registry = assistant_registry

    async def synchronize(self) -> tuple[SkillActivation, ...]:
        activations: list[SkillActivation] = []
        for name in self._lifecycle.names:
            activation = await self._lifecycle.status(name)
            self._lifecycle.apply_to(self._assistant_registry, activation)
            activations.append(activation)
        return tuple(activations)

    async def set_active(self, name: str, active: bool) -> SkillActivation:
        activation = await self._lifecycle.set_active(name, active)
        self._lifecycle.apply_to(self._assistant_registry, activation)
        return activation


__all__ = [
    "SkillActivationService",
    "SkillLifecycleError",
    "SkillLifecycleErrorCode",
    "SkillLifecycleService",
]
