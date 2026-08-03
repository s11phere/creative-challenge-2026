"""PostgreSQL and in-memory stores for durable Skill active pointers."""

from __future__ import annotations

import asyncio

from application.skills import SkillActivation
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from .database import Database
from .orm import SkillActivationModel


class PostgresSkillActivationStore:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def initialize(self, activation: SkillActivation) -> SkillActivation:
        async with self._database.transaction() as session:
            await session.execute(
                insert(SkillActivationModel)
                .values(
                    skill_name=activation.name,
                    active_version=activation.version,
                    content_sha256=activation.content_sha256,
                    revision=1,
                )
                .on_conflict_do_nothing(index_elements=[SkillActivationModel.skill_name])
            )
            model = await session.scalar(
                select(SkillActivationModel).where(
                    SkillActivationModel.skill_name == activation.name
                )
            )
            if model is None:
                raise RuntimeError("Skill activation initialization did not persist a row")
            return _record(model)

    async def get(self, name: str) -> SkillActivation | None:
        async with self._database.session() as session:
            model = await session.scalar(
                select(SkillActivationModel).where(SkillActivationModel.skill_name == name)
            )
            return _record(model) if model is not None else None

    async def compare_and_set(
        self, activation: SkillActivation, *, expected_revision: int
    ) -> SkillActivation | None:
        async with self._database.transaction() as session:
            model = await session.scalar(
                update(SkillActivationModel)
                .where(
                    SkillActivationModel.skill_name == activation.name,
                    SkillActivationModel.revision == expected_revision,
                )
                .values(
                    active_version=activation.version,
                    content_sha256=activation.content_sha256,
                    revision=SkillActivationModel.revision + 1,
                )
                .returning(SkillActivationModel)
            )
            return _record(model) if model is not None else None


class InMemorySkillActivationStore:
    def __init__(self) -> None:
        self._records: dict[str, SkillActivation] = {}
        self._lock = asyncio.Lock()

    async def initialize(self, activation: SkillActivation) -> SkillActivation:
        async with self._lock:
            return self._records.setdefault(activation.name, activation)

    async def get(self, name: str) -> SkillActivation | None:
        async with self._lock:
            return self._records.get(name)

    async def compare_and_set(
        self, activation: SkillActivation, *, expected_revision: int
    ) -> SkillActivation | None:
        async with self._lock:
            current = self._records.get(activation.name)
            if current is None or current.revision != expected_revision:
                return None
            updated = SkillActivation(
                name=activation.name,
                version=activation.version,
                content_sha256=activation.content_sha256,
                revision=expected_revision + 1,
            )
            self._records[activation.name] = updated
            return updated


def _record(model: SkillActivationModel) -> SkillActivation:
    return SkillActivation(
        name=model.skill_name,
        version=model.active_version,
        content_sha256=model.content_sha256,
        revision=model.revision,
    )


__all__ = ["InMemorySkillActivationStore", "PostgresSkillActivationStore"]
