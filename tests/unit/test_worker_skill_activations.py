from __future__ import annotations

import pytest
from application.skills import SkillActivation
from infrastructure.qa_execution import assistant_skill_registry
from infrastructure.skill_catalog import (
    INTERNAL_RUNTIME_SKILL_NAMES,
    FileSystemNativeSkillCatalog,
    FileSystemSkillCatalog,
)
from infrastructure.skill_lifecycle import InMemorySkillActivationStore
from worker.assistant_tasks import _apply_skill_activations


@pytest.mark.asyncio
async def test_worker_excludes_disabled_skill_from_next_native_catalog() -> None:
    registry = assistant_skill_registry()
    store = InMemorySkillActivationStore()
    compare_pin = registry.pin("compare_sources", "1.0.0")
    await store.initialize(
        SkillActivation(
            name=compare_pin.name,
            version=compare_pin.version,
            content_sha256=compare_pin.content_sha256,
            revision=1,
            active=False,
        )
    )

    await _apply_skill_activations(registry, store)

    catalog = FileSystemNativeSkillCatalog(
        registry,
        FileSystemSkillCatalog(
            registry,
            include_manifest_v2=True,
            excluded_names=INTERNAL_RUNTIME_SKILL_NAMES,
        ),
        tool_adapters={},
    )
    route_names = {route.pin.name for route in catalog.list_routes()}

    assert "compare_sources" not in route_names
    assert "assistant_agent" not in route_names
    assert {"course_project_workflow", "exam_preparation_workflow"}.issubset(route_names)
