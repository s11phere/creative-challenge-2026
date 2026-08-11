"""Persisted Skill activation orchestration over the trusted registry."""

from __future__ import annotations

import asyncio
from enum import StrEnum

from agent_runtime import FileSystemSkillRegistry, SkillRegistryError

from .catalog import SkillActivation, SkillActivationStore


class SkillLifecycleErrorCode(StrEnum):
    NOT_FOUND = "SKILL_NOT_FOUND"
    INVALID = "SKILL_INVALID"


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
            default = self._defaults.get(name)
            if default is None:
                raise SkillLifecycleError(
                    SkillLifecycleErrorCode.NOT_FOUND, "Skill is not installed."
                )
            desired = self._activation(name, default, 1)
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

    def _apply(self, activation: SkillActivation) -> None:
        try:
            pin = self._registry.pin(activation.name, activation.version)
            if pin.content_sha256 != activation.content_sha256:
                raise SkillLifecycleError(
                    SkillLifecycleErrorCode.INVALID,
                    "Persisted Skill identity does not match the trusted package.",
                )
            self._registry.activate(activation.name, activation.version)
        except SkillRegistryError as exc:
            raise SkillLifecycleError(
                SkillLifecycleErrorCode.INVALID,
                "Persisted Skill activation cannot be loaded from the trusted root.",
            ) from exc


__all__ = ["SkillLifecycleError", "SkillLifecycleErrorCode", "SkillLifecycleService"]
