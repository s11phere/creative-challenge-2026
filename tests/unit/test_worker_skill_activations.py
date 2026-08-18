from __future__ import annotations

import pytest
from application.skills import SkillActivation
from infrastructure.qa_execution import assistant_skill_registry
from infrastructure.skill_catalog import (
    INTERNAL_RUNTIME_SKILL_NAMES,
    FileSystemSkillCatalog,
)
from infrastructure.skill_lifecycle import InMemorySkillActivationStore
from worker.assistant_tasks import _apply_skill_activations


@pytest.mark.asyncio
async def test_worker_excludes_disabled_skill_from_next_native_catalog() -> None:
    registry = assistant_skill_registry()
    store = InMemorySkillActivationStore()
    research_pin = registry.pin("research_reading_workflow", "2.0.0")
    await store.initialize(
        SkillActivation(
            name=research_pin.name,
            version=research_pin.version,
            content_sha256=research_pin.content_sha256,
            revision=1,
            active=False,
        )
    )

    await _apply_skill_activations(registry, store)

    catalog = FileSystemSkillCatalog(
        registry, include_manifest_v2=True, excluded_names=INTERNAL_RUNTIME_SKILL_NAMES
    )
    route_names = {route.name for route in catalog.list_active_invocations()}

    assert "research_reading_workflow" not in route_names
    assert "assistant_agent" not in route_names
    assert {"course_project_workflow", "exam_preparation_workflow"}.issubset(route_names)
