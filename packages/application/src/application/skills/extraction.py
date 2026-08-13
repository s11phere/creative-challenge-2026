"""Pattern mining and candidate Skill extraction (Phase 6, Path B).

Phase 6 turns repeated usage traces into candidate personal Skills. Mining groups
completed, unbound assistant-turn traces along the Phase 2 dimensions, keeps only
strong patterns (frequency and session-spread thresholds, tool-using workflows),
and the generator drafts a personal Skill package whose prompt and eval cases are
anchored to the observed exemplar runs. Nothing is activated automatically: every
candidate must pass the dual gate — the Phase 1 structural eval gate AND the
historical-reproduction gate (every eval case traces back to a mined exemplar
run) — before it is kept as a user-facing draft.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from domain.usage_traces import (
    UsageOutcome,
    UsageTrace,
    UsageTraceRepository,
    pattern_key,
)

from ..usage_traces import classify_input_type, classify_task_category, tool_sequence
from .creator_tools import scaffold_skill_files
from .drafts import SkillDraftError, SkillDraftStore
from .evaluation import SkillEvalSkillReport

_IGNORED_CATEGORIES = frozenset({"general"})
_EXEMPLAR_LIMIT = 5
_CASE_SUMMARY_LIMIT = 280
_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


@dataclass(frozen=True)
class ExemplarEvidence:
    """One sanitized source run that contributed to a mined pattern."""

    run_id: UUID
    conversation_id: UUID
    input_summary: str
    created_at: datetime


@dataclass(frozen=True)
class PatternCandidate:
    """One strong, evidence-backed candidate extracted from usage traces."""

    key: str
    task_category: str
    tool_sequence: str
    input_type: str
    frequency: int
    distinct_conversations: int
    first_seen_at: datetime
    last_seen_at: datetime
    exemplars: tuple[ExemplarEvidence, ...]


class PatternMiningService:
    """Group completed usage traces into strong, overfitting-guarded candidates."""

    def __init__(
        self,
        *,
        traces: UsageTraceRepository,
        window_days: int = 30,
        min_frequency: int = 3,
        min_conversations: int = 2,
        max_candidates: int = 5,
    ) -> None:
        if window_days < 1 or min_frequency < 1 or min_conversations < 1 or max_candidates < 1:
            raise ValueError("Pattern mining bounds must be positive")
        self._traces = traces
        self._window_days = window_days
        self._min_frequency = min_frequency
        self._min_conversations = min_conversations
        self._max_candidates = max_candidates

    async def mine(self, *, now: datetime | None = None) -> tuple[PatternCandidate, ...]:
        since = (now or datetime.now(UTC)) - timedelta(days=self._window_days)
        traces = await self._traces.list(since=since)
        groups: dict[str, list[UsageTrace]] = defaultdict(list)
        for trace in traces:
            if not _mines_to_pattern(trace):
                continue
            groups[_cluster_key(trace)].append(trace)
        candidates: list[PatternCandidate] = []
        for key, members in groups.items():
            if len(members) < self._min_frequency:
                continue
            conversations = {trace.conversation_id for trace in members}
            if len(conversations) < self._min_conversations:
                continue
            category = classify_task_category(members[0].input_summary, skill_name=None)
            candidates.append(
                PatternCandidate(
                    key=key,
                    task_category=category,
                    tool_sequence=tool_sequence(members[0].tools_used),
                    input_type=classify_input_type(members[0].input_summary),
                    frequency=len(members),
                    distinct_conversations=len(conversations),
                    first_seen_at=min(trace.created_at for trace in members),
                    last_seen_at=max(trace.created_at for trace in members),
                    exemplars=_exemplars(members),
                )
            )
        return tuple(
            sorted(candidates, key=lambda item: (-item.frequency, item.key))[: self._max_candidates]
        )


class PatternCandidateGenerator:
    """Draft a personal Skill package from one mined pattern (creator mechanism)."""

    def generate(self, *, name: str, candidate: PatternCandidate) -> dict[str, str]:
        files = scaffold_skill_files(name=name, description=_description(candidate))
        files["prompts/system.md"] = _system_prompt(candidate)
        files["evals/cases.jsonl"] = _eval_cases(candidate)
        files["evidence.json"] = json.dumps(_evidence(candidate), ensure_ascii=False, indent=2)
        files["evidence.json"] += "\n"
        return files


class PatternExtractionService:
    """Mine, draft, and dual-gate candidate Skills; never activates automatically."""

    def __init__(
        self,
        *,
        traces: UsageTraceRepository,
        drafts: SkillDraftStore,
        window_days: int = 30,
        min_frequency: int = 3,
        min_conversations: int = 2,
        max_candidates: int = 5,
        existing_names: Callable[[], frozenset[str]] | None = None,
    ) -> None:
        self._miner = PatternMiningService(
            traces=traces,
            window_days=window_days,
            min_frequency=min_frequency,
            min_conversations=min_conversations,
            max_candidates=max_candidates,
        )
        self._drafts = drafts
        self._generator = PatternCandidateGenerator()
        self._existing_names = existing_names

    async def extract(self, *, now: datetime | None = None) -> PatternExtractionResult:
        candidates = await self._miner.mine(now=now)
        existing = set(self._existing_names()) if self._existing_names is not None else set()
        created: list[str] = []
        rejected: list[PatternRejection] = []
        skipped: list[str] = []
        for candidate in candidates:
            name = candidate_name(candidate)
            if not _NAME_PATTERN.match(name) or name in existing:
                skipped.append(name)
                continue
            files = self._generator.generate(name=name, candidate=candidate)
            try:
                self._drafts.create(name, files)
            except SkillDraftError:
                skipped.append(name)
                continue
            existing.add(name)
            validation = self._drafts.validate(name)
            if not validation.valid:
                self._drafts.delete(name)
                rejected.append(PatternRejection(name, "validation_failed", validation.error))
                continue
            report = await self._drafts.run_eval(name)
            if not dual_gate_passed(report, candidate):
                self._drafts.delete(name)
                rejected.append(PatternRejection(name, "gate_failed", _gate_failure_detail(report)))
                continue
            created.append(name)
        return PatternExtractionResult(
            candidates=len(candidates),
            created_drafts=tuple(created),
            rejected=tuple(rejected),
            skipped=tuple(skipped),
        )


@dataclass(frozen=True)
class PatternRejection:
    """A candidate that failed validation/eval and produced no draft."""

    name: str
    reason: str
    detail: str | None = None


@dataclass(frozen=True)
class PatternExtractionResult:
    candidates: int
    created_drafts: tuple[str, ...]
    rejected: tuple[PatternRejection, ...]
    skipped: tuple[str, ...]


def candidate_name(candidate: PatternCandidate) -> str:
    base = re.sub(r"[^a-z0-9_]", "_", candidate.task_category)
    return f"{base}_workflow"


def dual_gate_passed(report: SkillEvalSkillReport, candidate: PatternCandidate) -> bool:
    """Phase 1 structural gate plus historical anchoring to observed exemplars."""
    if report.metrics.total < 1:
        return False
    if not (report.metrics.passed == report.metrics.total and report.metrics.errored == 0):
        return False
    expected = {_case_id(exemplar.run_id) for exemplar in candidate.exemplars}
    case_ids = {case.case_id for case in report.cases}
    return bool(case_ids) and case_ids.issubset(expected)


def _mines_to_pattern(trace: UsageTrace) -> bool:
    if trace.outcome is not UsageOutcome.COMPLETED or trace.skill_name is not None:
        return False
    if not trace.tools_used:
        return False
    category = classify_task_category(trace.input_summary, skill_name=None)
    return category not in _IGNORED_CATEGORIES


def _cluster_key(trace: UsageTrace) -> str:
    return pattern_key(
        skill_name=None,
        task_category=classify_task_category(trace.input_summary, skill_name=None),
        tool_sequence=tool_sequence(trace.tools_used),
        input_type=classify_input_type(trace.input_summary),
    )


def _exemplars(members: Sequence[UsageTrace]) -> tuple[ExemplarEvidence, ...]:
    recent = sorted(members, key=lambda trace: trace.created_at, reverse=True)[:_EXEMPLAR_LIMIT]
    return tuple(
        ExemplarEvidence(
            run_id=trace.run_id,
            conversation_id=trace.conversation_id,
            input_summary=trace.input_summary,
            created_at=trace.created_at,
        )
        for trace in recent
    )


def _description(candidate: PatternCandidate) -> str:
    return (
        f"固化的「{candidate.task_category}」工作模式：{candidate.frequency} 次 / "
        f"{candidate.distinct_conversations} 个会话，常用工具 {candidate.tool_sequence}。"
    )


def _system_prompt(candidate: PatternCandidate) -> str:
    return (
        "You are a personal Skill distilled from the user's repeated "
        f"「{candidate.task_category}」 work. This pattern was observed "
        f"{candidate.frequency} times across {candidate.distinct_conversations} sessions, "
        f"typically using the tools: {candidate.tool_sequence}. "
        "Delegate to grounded retrieval when the request depends on the Space, "
        "ground every statement in the returned sources, and return exactly the "
        "declared output structure.\n"
    )


def _eval_cases(candidate: PatternCandidate) -> str:
    lines: list[str] = []
    for exemplar in candidate.exemplars:
        case: dict[str, Any] = {
            "case_id": _case_id(exemplar.run_id),
            "input": {"question": exemplar.input_summary[:_CASE_SUMMARY_LIMIT]},
            "expected": candidate.task_category,
            "fixture": "synthetic_only",
            "checks": [
                {"type": "output_has_key", "key": "status"},
                {"type": "output_has_key", "key": "result"},
                {"type": "finalized"},
            ],
        }
        lines.append(json.dumps(case, ensure_ascii=False))
    return "\n".join(lines) + "\n"


def _evidence(candidate: PatternCandidate) -> dict[str, Any]:
    return {
        "schema_version": "pattern-extraction-evidence-v1",
        "pattern": candidate.key,
        "task_category": candidate.task_category,
        "tool_sequence": candidate.tool_sequence,
        "input_type": candidate.input_type,
        "frequency": candidate.frequency,
        "distinct_conversations": candidate.distinct_conversations,
        "first_seen_at": candidate.first_seen_at.isoformat(),
        "last_seen_at": candidate.last_seen_at.isoformat(),
        "exemplars": [
            {
                "run_id": str(exemplar.run_id),
                "conversation_id": str(exemplar.conversation_id),
                "input_summary": exemplar.input_summary,
                "created_at": exemplar.created_at.isoformat(),
            }
            for exemplar in candidate.exemplars
        ],
    }


def _case_id(run_id: UUID) -> str:
    return f"exemplar-{run_id.hex}"


def _gate_failure_detail(report: SkillEvalSkillReport) -> str:
    metrics = report.metrics
    return (
        f"{metrics.passed}/{metrics.total} cases passed, {metrics.failed} failed, "
        f"{metrics.errored} errored"
    )


__all__ = [
    "ExemplarEvidence",
    "PatternCandidate",
    "PatternCandidateGenerator",
    "PatternExtractionResult",
    "PatternExtractionService",
    "PatternMiningService",
    "PatternRejection",
    "candidate_name",
    "dual_gate_passed",
]
