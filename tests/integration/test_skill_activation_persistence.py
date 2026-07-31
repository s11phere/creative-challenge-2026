from __future__ import annotations

import os

import pytest
from application.skills import SkillActivation
from infrastructure.config import settings
from infrastructure.database import Database
from infrastructure.orm import SkillActivationModel
from infrastructure.skill_lifecycle import PostgresSkillActivationStore
from sqlalchemy import delete

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_INTEGRATION") != "1",
        reason="set RUN_INTEGRATION=1 with an isolated migrated PostgreSQL database",
    ),
]


@pytest.mark.asyncio
async def test_skill_activation_pointer_is_persistent_and_compare_and_set() -> None:
    database = Database(settings.database_url)
    store = PostgresSkillActivationStore(database)
    initial = SkillActivation("integration_skill", "1.0.0", "a" * 64, 1)
    try:
        persisted = await store.initialize(initial)
        current = await PostgresSkillActivationStore(database).get(initial.name)
        assert current == persisted

        updated = await store.compare_and_set(
            SkillActivation(initial.name, "2.0.0", "b" * 64, persisted.revision + 1),
            expected_revision=persisted.revision,
        )
        assert updated is not None
        assert (updated.version, updated.revision) == ("2.0.0", persisted.revision + 1)
        assert (await store.compare_and_set(initial, expected_revision=persisted.revision)) is None
    finally:
        async with database.transaction() as session:
            await session.execute(
                delete(SkillActivationModel).where(SkillActivationModel.skill_name == initial.name)
            )
        await database.dispose()
