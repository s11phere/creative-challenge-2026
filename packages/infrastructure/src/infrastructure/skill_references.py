"""Persistent Skill-version reference checks used by controlled cleanup."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select

from .database import Database
from .orm import QARunModel, RuntimeCheckpointModel, RuntimeRunModel


@dataclass(frozen=True)
class SkillReferenceReport:
    skill_name: str
    skill_version: str
    content_sha256: str
    qa_runs: int
    runtime_runs: int
    checkpoints: int

    @property
    def total(self) -> int:
        return self.qa_runs + self.runtime_runs + self.checkpoints


class PostgresSkillReferenceChecker:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def references(
        self, *, skill_name: str, skill_version: str, content_sha256: str
    ) -> SkillReferenceReport:
        async with self._database.session() as session:
            qa_count = await session.scalar(
                select(func.count())
                .select_from(QARunModel)
                .where(
                    QARunModel.versions["skill_name"].as_string() == skill_name,
                    QARunModel.versions["skill_version"].as_string() == skill_version,
                    QARunModel.versions["skill_content_sha256"].as_string() == content_sha256,
                )
            )
            runtime_count = await session.scalar(
                select(func.count())
                .select_from(RuntimeRunModel)
                .where(
                    RuntimeRunModel.skill_name == skill_name,
                    RuntimeRunModel.skill_version == skill_version,
                    RuntimeRunModel.skill_content_sha256 == content_sha256,
                )
            )
            checkpoint_count = await session.scalar(
                select(func.count())
                .select_from(RuntimeCheckpointModel)
                .where(
                    RuntimeCheckpointModel.skill_name == skill_name,
                    RuntimeCheckpointModel.skill_version == skill_version,
                    RuntimeCheckpointModel.skill_content_sha256 == content_sha256,
                )
            )
        return SkillReferenceReport(
            skill_name=skill_name,
            skill_version=skill_version,
            content_sha256=content_sha256,
            qa_runs=int(qa_count or 0),
            runtime_runs=int(runtime_count or 0),
            checkpoints=int(checkpoint_count or 0),
        )


__all__ = ["PostgresSkillReferenceChecker", "SkillReferenceReport"]
