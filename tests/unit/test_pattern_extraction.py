from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from agent_runtime import PersonalSkillRegistry
from application.skills import (
    DraftSkillEvalRunner,
    PatternExtractionService,
    PersonalSkillStore,
    SkillDraftStore,
    SkillEvalCaseResult,
    SkillEvalSkillReport,
    SkillEvalStatus,
    build_skill_report,
)
from domain.conversation_context import ConversationSensitivity
from domain.usage_traces import UsageOutcome, UsageTrace
from infrastructure.skill_lifecycle import InMemorySkillActivationStore

from tests.unit.test_pattern_mining import InMemoryUsageTraceRepository
from tests.unit.test_skill_drafts import write_builtin_package

_BASE = datetime(2026, 8, 1, tzinfo=UTC)
_NOW = _BASE + timedelta(days=4)


def _trace(*, index: int, conversation: int) -> UsageTrace:
    return UsageTrace(
        run_id=UUID(int=50_000 + index),
        conversation_id=UUID(int=60_000 + conversation),
        input_summary="请总结这篇文档的重点",
        tools_used=("knowledge_search", "grounded_answer"),
        outcome=UsageOutcome.COMPLETED,
        model="fake",
        sensitivity=ConversationSensitivity.PRIVATE_LOCAL,
        skill_name=None,
        command="assistant_turn",
        created_at=_BASE + timedelta(days=index),
    )


class StubEvalRunner:
    """Replace the deterministic gate with a controlled report."""

    def __init__(self, report: SkillEvalSkillReport) -> None:
        self._report = report

    async def evaluate(self, name: str) -> SkillEvalSkillReport:
        del name
        return self._report


def _all_passed_report(case_ids: tuple[str, ...]) -> SkillEvalSkillReport:
    results = tuple(
        SkillEvalCaseResult(
            case_id=case_id,
            status=SkillEvalStatus.PASSED,
            failure_categories=(),
            evidence=(),
            executed=True,
            latency_ms=1.0,
        )
        for case_id in case_ids
    )
    return build_skill_report("candidate", "1.0.0", results)


@pytest.fixture
def draft_store(tmp_path: Path) -> SkillDraftStore:
    builtin_root = tmp_path / "builtin"
    builtin_root.mkdir()
    write_builtin_package(builtin_root, "builtin_skill")
    registry = PersonalSkillRegistry(builtin_root, personal_root=tmp_path / "personal")
    registry.reload()
    personal_store = PersonalSkillStore(registry=registry, store=InMemorySkillActivationStore())
    return SkillDraftStore(
        registry=registry,
        personal_store=personal_store,
        eval_runner=DraftSkillEvalRunner(registry=registry),
    )


def _service(draft_store: SkillDraftStore) -> PatternExtractionService:
    registry = draft_store._registry
    return PatternExtractionService(
        traces=InMemoryUsageTraceRepository(
            tuple(_trace(index=index, conversation=index) for index in (1, 2, 3))
        ),
        drafts=draft_store,
        existing_names=lambda: frozenset(registry.names()).union(frozenset(registry.draft_names())),
    )


class TestPatternExtractionService:
    async def test_creates_draft_when_dual_gate_passes(self, draft_store: SkillDraftStore) -> None:
        service = _service(draft_store)
        result = await service.extract(now=_NOW)

        assert result.created_drafts == ("summarize_workflow",)
        assert result.rejected == ()
        draft = draft_store.get("summarize_workflow")
        assert draft.valid is True
        files = draft_store.read_files("summarize_workflow")
        assert "evidence.json" in files
        assert "summarize" in files["prompts/system.md"]

    async def test_never_activates_automatically(self, draft_store: SkillDraftStore) -> None:
        service = _service(draft_store)
        await service.extract(now=_NOW)

        # The candidate stays a draft: never promoted to a personal Skill,
        # so the user approval (activate) step is still required.
        assert "summarize_workflow" in draft_store._registry.draft_names()
        assert "summarize_workflow" not in draft_store._registry.personal_names()

    async def test_skips_candidate_when_name_already_covered(
        self, draft_store: SkillDraftStore
    ) -> None:
        registry = draft_store._registry
        service = PatternExtractionService(
            traces=InMemoryUsageTraceRepository(
                tuple(_trace(index=index, conversation=index) for index in (1, 2, 3))
            ),
            drafts=draft_store,
            existing_names=lambda: frozenset({"summarize_workflow"}).union(
                frozenset(registry.names())
            ),
        )
        result = await service.extract(now=_NOW)
        assert result.skipped == ("summarize_workflow",)
        assert result.created_drafts == ()

    async def test_rejects_and_deletes_draft_when_gate_fails(
        self, draft_store: SkillDraftStore
    ) -> None:
        registry = draft_store._registry
        personal_store = draft_store._personal_store
        store = SkillDraftStore(
            registry=registry,
            personal_store=personal_store,
            eval_runner=StubEvalRunner(
                _all_passed_report(("exemplar-foreignrun",))  # not anchored to an exemplar
            ),
        )
        service = _service(store)
        result = await service.extract(now=_NOW)

        assert result.created_drafts == ()
        assert len(result.rejected) == 1
        assert result.rejected[0].name == "summarize_workflow"
        assert result.rejected[0].reason == "gate_failed"
        # The failed draft was removed; nothing surfaces to the user.
        assert store.list() == ()
