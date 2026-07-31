"""Persisted Skill activation orchestration over the trusted registry."""

from __future__ import annotations

import asyncio
from enum import StrEnum

from agent_runtime import FileSystemSkillRegistry, SkillRegistryError

from .catalog import SkillActivation, SkillActivationStore


class SkillLifecycleErrorCode(StrEnum):
    NOT_FOUND = "SKILL_NOT_FOUND"
    INVALID = "SKILL_INVALID"
    ACTIVATION_CONFLICT = "SKILL_ACTIVATION_CONFLICT"


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
        async with self._lock:
            activation = await self._store.get(name)
            if activation is None:
                default = self._defaults.get(name)
                if default is None:
                    raise SkillLifecycleError(
                        SkillLifecycleErrorCode.NOT_FOUND, "Skill is not installed."
                    )
                activation = await self._store.initialize(self._activation(name, default, 1))
            self._apply(activation, rollback=False)
            return activation

    async def activate(
        self,
        name: str,
        version: str,
        *,
        expected_revision: int,
        rollback: bool = False,
    ) -> SkillActivation:
        async with self._lock:
            current = await self._store.get(name)
            if current is None:
                default = self._defaults.get(name)
                if default is None:
                    raise SkillLifecycleError(
                        SkillLifecycleErrorCode.NOT_FOUND, "Skill is not installed."
                    )
                current = await self._store.initialize(self._activation(name, default, 1))
                self._apply(current, rollback=False)
            candidate = self._activation(name, version, expected_revision + 1)
            updated = await self._store.compare_and_set(
                candidate, expected_revision=expected_revision
            )
            if updated is None:
                current = await self._store.get(name)
                if current is not None:
                    self._apply(current, rollback=False)
                raise SkillLifecycleError(
                    SkillLifecycleErrorCode.ACTIVATION_CONFLICT,
                    "Skill active revision changed; refresh the catalog and retry.",
                )
            self._apply(updated, rollback=rollback)
            return updated

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

    def _apply(self, activation: SkillActivation, *, rollback: bool) -> None:
        try:
            pin = self._registry.pin(activation.name, activation.version)
            if pin.content_sha256 != activation.content_sha256:
                raise SkillLifecycleError(
                    SkillLifecycleErrorCode.INVALID,
                    "Persisted Skill identity does not match the trusted package.",
                )
            if rollback:
                self._registry.rollback(activation.name, activation.version)
            else:
                self._registry.activate(activation.name, activation.version)
        except SkillRegistryError as exc:
            raise SkillLifecycleError(
                SkillLifecycleErrorCode.INVALID,
                "Persisted Skill activation cannot be loaded from the trusted root.",
            ) from exc


__all__ = ["SkillLifecycleError", "SkillLifecycleErrorCode", "SkillLifecycleService"]
