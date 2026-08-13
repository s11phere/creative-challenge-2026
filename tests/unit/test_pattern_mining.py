from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

from application.skills import (
    ExemplarEvidence,
    PatternCandidate,
    PatternCandidateGenerator,
    PatternMiningService,
    SkillEvalCaseResult,
    SkillEvalFailureCategory,
    SkillEvalSkillReport,
    SkillEvalStatus,
    build_skill_report,
    candidate_name,
    dual_gate_passed,
)
from domain.conversation_context import ConversationSensitivity
from domain.usage_traces import UsageOutcome, UsageTrace

_BASE = datetime(2026, 8, 1, tzinfo=UTC)


class InMemoryUsageTraceRepository:
    def __init__(self, traces: tuple[UsageTrace, ...] = ()) -> None:
        self._traces = list(traces)

    async def save(self, trace: UsageTrace) -> UsageTrace:
        self._traces.append(trace)
        return trace

    async def get(self, run_id: UUID) -> UsageTrace | None:
        return next((trace for trace in self._traces if trace.run_id == run_id), None)

    async def list(
        self, *, limit: int | None = None, since: datetime | None = None
    ) -> tuple[UsageTrace, ...]:
        values = [trace for trace in self._traces if since is None or trace.created_at >= since]
        selected = tuple(values)
        return selected[:limit] if limit is not None else selected


def _trace(
    *,
    index: int,
    input_summary: str,
    tools: tuple[str, ...] = ("knowledge_search", "grounded_answer"),
    skill_name: str | None = None,
    conversation: int | None = None,
    created_at: datetime = _BASE,
) -> UsageTrace:
    return UsageTrace(
        run_id=UUID(int=10_000 + index),
        conversation_id=UUID(int=20_000 + (conversation if conversation is not None else index)),
        input_summary=input_summary,
        tools_used=tools,
        outcome=UsageOutcome.COMPLETED,
        model="fake",
        sensitivity=ConversationSensitivity.PRIVATE_LOCAL,
        skill_name=skill_name,
        command="assistant_turn" if skill_name is None else None,
        created_at=created_at,
    )


def _candidate(*, exemplar_count: int = 3) -> PatternCandidate:
    exemplars = tuple(
        ExemplarEvidence(
            run_id=UUID(int=30_000 + index),
            conversation_id=UUID(int=40_000 + index),
            input_summary=f"exemplar input {index}",
            created_at=_BASE + timedelta(days=index),
        )
        for index in range(exemplar_count)
    )
    return PatternCandidate(
        key="skill=none|category=summarize|tools=knowledge_search,grounded_answer|input=zh",
        task_category="summarize",
        tool_sequence="knowledge_search,grounded_answer",
        input_type="zh",
        frequency=exemplar_count,
        distinct_conversations=exemplar_count,
        first_seen_at=_BASE,
        last_seen_at=_BASE + timedelta(days=exemplar_count),
        exemplars=tuple(exemplars),
    )


class TestPatternMiningService:
    async def test_groups_strong_pattern_with_exemplars(self) -> None:
        traces = (
            _trace(index=1, input_summary="请总结这篇文档", conversation=1, created_at=_BASE),
            _trace(
                index=2,
                input_summary="请总结另一篇文档",
                conversation=2,
                created_at=_BASE + timedelta(days=1),
            ),
            _trace(
                index=3,
                input_summary="再总结一下",
                conversation=3,
                created_at=_BASE + timedelta(days=2),
            ),
        )
        service = PatternMiningService(
            traces=InMemoryUsageTraceRepository(traces),
            min_frequency=3,
            min_conversations=2,
        )
        candidates = await service.mine()
        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.task_category == "summarize"
        assert candidate.frequency == 3
        assert candidate.distinct_conversations == 3
        assert len(candidate.exemplars) == 3
        assert candidate.exemplars[0].run_id == traces[-1].run_id  # most recent first

    async def test_frequency_below_threshold_produces_no_candidate(self) -> None:
        traces = (
            _trace(index=1, input_summary="请总结这篇文档", conversation=1),
            _trace(index=2, input_summary="请总结另一篇文档", conversation=2),
        )
        service = PatternMiningService(
            traces=InMemoryUsageTraceRepository(traces),
            min_frequency=3,
            min_conversations=2,
        )
        assert await service.mine() == ()

    async def test_single_session_is_overfitting_and_excluded(self) -> None:
        traces = (
            _trace(index=1, input_summary="请总结这篇文档", conversation=7),
            _trace(index=2, input_summary="请总结另一篇文档", conversation=7),
            _trace(index=3, input_summary="再总结一下", conversation=7),
        )
        service = PatternMiningService(
            traces=InMemoryUsageTraceRepository(traces),
            min_frequency=3,
            min_conversations=2,
        )
        assert await service.mine() == ()

    async def test_ignores_general_category(self) -> None:
        traces = (
            _trace(index=1, input_summary="随便聊聊今天天气", conversation=1),
            _trace(index=2, input_summary="再聊点别的", conversation=2),
            _trace(index=3, input_summary="继续闲聊", conversation=3),
        )
        service = PatternMiningService(
            traces=InMemoryUsageTraceRepository(traces),
            min_frequency=3,
            min_conversations=2,
        )
        assert await service.mine() == ()

    async def test_ignores_chat_without_tools(self) -> None:
        traces = (
            _trace(index=1, input_summary="帮我翻译这句话", tools=(), conversation=1),
            _trace(index=2, input_summary="帮我翻译另一句", tools=(), conversation=2),
            _trace(index=3, input_summary="再翻译一句", tools=(), conversation=3),
        )
        service = PatternMiningService(
            traces=InMemoryUsageTraceRepository(traces),
            min_frequency=3,
            min_conversations=2,
        )
        assert await service.mine() == ()

    async def test_ignores_skill_bound_patterns(self) -> None:
        traces = (
            _trace(
                index=1,
                input_summary="请总结这篇文档",
                skill_name="summarize_document",
                conversation=1,
            ),
            _trace(
                index=2,
                input_summary="请总结另一篇文档",
                skill_name="summarize_document",
                conversation=2,
            ),
            _trace(
                index=3,
                input_summary="再总结一下",
                skill_name="summarize_document",
                conversation=3,
            ),
        )
        service = PatternMiningService(
            traces=InMemoryUsageTraceRepository(traces),
            min_frequency=3,
            min_conversations=2,
        )
        assert await service.mine() == ()

    async def test_window_filters_old_traces(self) -> None:
        old = _trace(
            index=1,
            input_summary="请总结这篇文档",
            conversation=1,
            created_at=_BASE - timedelta(days=40),
        )
        traces = (
            old,
            _trace(index=2, input_summary="请总结另一篇文档", conversation=2),
            _trace(index=3, input_summary="再总结一下", conversation=3),
        )
        service = PatternMiningService(
            traces=InMemoryUsageTraceRepository(traces),
            window_days=30,
            min_frequency=3,
            min_conversations=2,
        )
        candidates = await service.mine(now=_BASE + timedelta(days=30))
        assert candidates == ()  # only two in-window traces remain

    async def test_orders_by_frequency_and_caps(self) -> None:
        traces: tuple[UsageTrace, ...] = ()
        for index in range(8):
            traces += (
                _trace(
                    index=index * 3 + 1,
                    input_summary="请总结这篇文档" if index % 2 == 0 else "帮我写份报告",
                    conversation=index * 10 + 1,
                ),
                _trace(
                    index=index * 3 + 2,
                    input_summary="请总结另一篇文档" if index % 2 == 0 else "帮我写份方案",
                    conversation=index * 10 + 2,
                ),
                _trace(
                    index=index * 3 + 3,
                    input_summary="再总结一下" if index % 2 == 0 else "再写一份",
                    conversation=index * 10 + 3,
                ),
            )
        service = PatternMiningService(
            traces=InMemoryUsageTraceRepository(traces),
            min_frequency=3,
            min_conversations=2,
            max_candidates=2,
        )
        candidates = await service.mine()
        assert len(candidates) == 2
        assert candidates[0].frequency >= candidates[1].frequency


class TestPatternCandidateGenerator:
    def test_generates_complete_package(self) -> None:
        files = PatternCandidateGenerator().generate(
            name="summarize_workflow", candidate=_candidate()
        )
        assert {
            "skill.yaml",
            "workflow.yaml",
            "schemas/input.json",
            "schemas/output.json",
            "prompts/system.md",
            "evals/cases.jsonl",
            "evidence.json",
        } <= set(files)
        assert "summarize" in files["prompts/system.md"]
        assert "knowledge_search,grounded_answer" in files["prompts/system.md"]

    def test_eval_cases_are_anchored_to_exemplars(self) -> None:
        candidate = _candidate(exemplar_count=3)
        files = PatternCandidateGenerator().generate(name="summarize_workflow", candidate=candidate)
        cases = [
            json.loads(line) for line in files["evals/cases.jsonl"].splitlines() if line.strip()
        ]
        assert len(cases) == 3
        for case, exemplar in zip(cases, candidate.exemplars, strict=True):
            assert case["case_id"] == f"exemplar-{exemplar.run_id.hex}"
            assert case["input"]["question"] == exemplar.input_summary
            check_types = {check["type"] for check in case["checks"]}
            assert "finalized" in check_types

    def test_evidence_serializes_run_ids(self) -> None:
        candidate = _candidate(exemplar_count=2)
        files = PatternCandidateGenerator().generate(name="summarize_workflow", candidate=candidate)
        evidence = json.loads(files["evidence.json"])
        assert evidence["schema_version"] == "pattern-extraction-evidence-v1"
        assert evidence["frequency"] == 2
        assert [item["run_id"] for item in evidence["exemplars"]] == [
            str(exemplar.run_id) for exemplar in candidate.exemplars
        ]


class TestDualGate:
    def test_passes_when_all_cases_anchored_and_passed(self) -> None:
        candidate = _candidate(exemplar_count=2)
        report = _all_passed_report(
            {f"exemplar-{exemplar.run_id.hex}" for exemplar in candidate.exemplars}
        )
        assert dual_gate_passed(report, candidate)

    def test_fails_when_case_not_anchored_to_exemplar(self) -> None:
        candidate = _candidate(exemplar_count=2)
        report = _all_passed_report({"exemplar-foreigncase"})
        assert not dual_gate_passed(report, candidate)

    def test_fails_when_no_cases(self) -> None:
        candidate = _candidate(exemplar_count=2)
        report = _all_passed_report(set())
        assert not dual_gate_passed(report, candidate)

    def test_fails_when_some_case_failed(self) -> None:
        candidate = _candidate(exemplar_count=2)
        ids = {f"exemplar-{exemplar.run_id.hex}" for exemplar in candidate.exemplars}
        report = _mixed_report(ids)
        assert not dual_gate_passed(report, candidate)


class TestCandidateName:
    def test_uses_category_slug(self) -> None:
        assert candidate_name(_candidate()) == "summarize_workflow"


def _all_passed_report(case_ids: set[str]) -> SkillEvalSkillReport:
    results = tuple(
        SkillEvalCaseResult(
            case_id=case_id,
            status=SkillEvalStatus.PASSED,
            failure_categories=(),
            evidence=(),
            executed=True,
            latency_ms=1.0,
        )
        for case_id in sorted(case_ids)
    )
    return build_skill_report("candidate", "1.0.0", results)


def _mixed_report(case_ids: set[str]) -> SkillEvalSkillReport:
    results = []
    for index, case_id in enumerate(sorted(case_ids)):
        status = SkillEvalStatus.PASSED if index == 0 else SkillEvalStatus.FAILED
        results.append(
            SkillEvalCaseResult(
                case_id=case_id,
                status=status,
                failure_categories=(
                    ()
                    if status is SkillEvalStatus.PASSED
                    else (SkillEvalFailureCategory.CHECK_FAILED,)
                ),
                evidence=(),
                executed=True,
                latency_ms=1.0,
            )
        )
    return build_skill_report("candidate", "1.0.0", tuple(results))
