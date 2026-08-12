"""Draft lifecycle use cases for the Skill Creator (Phase 4, Path A).

A draft is a writable, incomplete package under the personal Skill root
(``_drafts/<name>``). It may fail full validation mid-creation; the creator
gate is explicit: ``validate`` runs the full trusted suite, ``run_eval`` reuses
the Phase 1 deterministic judge, and ``activate`` promotes a validated draft
whose eval gate passed into an installed, activated personal Skill. Activation
is the durable, user-confirmed transition and is persisted through the shared
``skill_activations`` pointer.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

import yaml
from agent_runtime import PersonalSkillRegistry, SkillRegistryError

from .creator_eval import DraftSkillEvalRunner
from .evaluation import SkillEvalSkillReport
from .personal import PersonalSkillError, PersonalSkillStore, PersonalSkillView


class SkillDraftErrorCode(StrEnum):
    NOT_FOUND = "SKILL_DRAFT_NOT_FOUND"
    INVALID = "SKILL_DRAFT_INVALID"
    NAME_CONFLICT = "SKILL_DRAFT_NAME_CONFLICT"
    EVAL_FAILED = "SKILL_DRAFT_EVAL_FAILED"


class SkillDraftError(Exception):
    def __init__(self, code: SkillDraftErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SkillDraftView:
    """Body-free projection of one draft for the transport layer."""

    name: str
    description: str
    complete: bool
    valid: bool
    file_count: int
    files: tuple[str, ...]


@dataclass(frozen=True)
class SkillDraftValidationResult:
    name: str
    valid: bool
    error: str | None = None
    description: str | None = None
    version: str | None = None
    content_sha256: str | None = None


class SkillDraftStore:
    """CRUD, validation gate, deterministic eval, and activation over drafts."""

    def __init__(
        self,
        *,
        registry: PersonalSkillRegistry,
        personal_store: PersonalSkillStore,
        eval_runner: DraftSkillEvalRunner,
    ) -> None:
        self._registry = registry
        self._personal_store = personal_store
        self._eval_runner = eval_runner

    def create(self, name: str, files: Mapping[str, str]) -> SkillDraftView:
        try:
            self._registry.create_draft(name, dict(files))
        except SkillRegistryError as exc:
            raise _draft_error(exc) from exc
        return self.get(name)

    def update(self, name: str, files: Mapping[str, str]) -> SkillDraftView:
        try:
            self._registry.update_draft(name, dict(files))
        except SkillRegistryError as exc:
            raise _draft_error(exc) from exc
        return self.get(name)

    def list(self) -> tuple[SkillDraftView, ...]:
        return tuple(self.get(name) for name in self._registry.draft_names())

    def get(self, name: str) -> SkillDraftView:
        try:
            files = self._registry.read_draft_files(name)
        except SkillRegistryError as exc:
            raise _draft_error(exc) from exc
        return self._view(name, files)

    def read_files(self, name: str) -> dict[str, str]:
        """Return the draft package files (path → content) for editing."""
        try:
            return self._registry.read_draft_files(name)
        except SkillRegistryError as exc:
            raise _draft_error(exc) from exc

    def delete(self, name: str) -> None:
        try:
            self._registry.delete_draft(name)
        except SkillRegistryError as exc:
            raise _draft_error(exc) from exc

    def validate(self, name: str) -> SkillDraftValidationResult:
        try:
            package = self._registry.validate_draft(name)
        except SkillRegistryError as exc:
            if str(getattr(exc.code, "value", exc.code)) == "SKILL_NOT_FOUND":
                raise _draft_error(exc) from exc
            return SkillDraftValidationResult(name=name, valid=False, error=str(exc))
        return SkillDraftValidationResult(
            name=name,
            valid=True,
            description=package.manifest.description,
            version=package.manifest.version,
            content_sha256=package.content_sha256,
        )

    async def run_eval(self, name: str) -> SkillEvalSkillReport:
        try:
            return await self._eval_runner.evaluate(name)
        except SkillRegistryError as exc:
            raise _draft_error(exc) from exc

    async def activate(self, name: str) -> PersonalSkillView:
        """Promote a valid draft whose eval gate passed into an active Skill.

        The eval gate is deterministic and re-run at activation time: every
        declared case must pass with no errors. Promotion re-runs the full
        validation suite and the durable activation pointer is persisted.
        """
        result = self.validate(name)
        if not result.valid:
            raise SkillDraftError(
                SkillDraftErrorCode.INVALID,
                result.error or "Skill draft failed validation.",
            )
        report = await self.run_eval(name)
        if not _eval_gate_passed(report):
            raise SkillDraftError(
                SkillDraftErrorCode.EVAL_FAILED,
                _gate_message(report),
            )
        try:
            self._registry.promote_draft(name)
        except SkillRegistryError as exc:
            raise _draft_error(exc) from exc
        try:
            return await self._personal_store.activate(name)
        except PersonalSkillError as exc:
            raise SkillDraftError(_map_personal_code(exc), str(exc)) from exc

    def _view(self, name: str, files: Mapping[str, str]) -> SkillDraftView:
        complete = "skill.yaml" in files and "workflow.yaml" in files
        return SkillDraftView(
            name=name,
            description=_description(files),
            complete=complete,
            valid=_is_valid_draft(self._registry, name),
            file_count=len(files),
            files=tuple(sorted(files)),
        )


def _eval_gate_passed(report: SkillEvalSkillReport) -> bool:
    metrics = report.metrics
    return metrics.total > 0 and metrics.passed == metrics.total and metrics.errored == 0


def _gate_message(report: SkillEvalSkillReport) -> str:
    metrics = report.metrics
    return (
        "Skill draft eval gate is not satisfied: "
        f"{metrics.passed}/{metrics.total} cases passed, {metrics.failed} failed, "
        f"{metrics.errored} errored."
    )


def _is_valid_draft(registry: PersonalSkillRegistry, name: str) -> bool:
    try:
        registry.validate_draft(name)
        return True
    except SkillRegistryError:
        return False


def _description(files: Mapping[str, str]) -> str:
    raw = files.get("skill.yaml")
    if not raw:
        return ""
    try:
        data = yaml.safe_load(raw)
    except (yaml.YAMLError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    description = data.get("description")
    return description if isinstance(description, str) else ""


def _draft_error(exc: SkillRegistryError) -> SkillDraftError:
    code = str(getattr(exc.code, "value", exc.code))
    if code == "SKILL_NOT_FOUND":
        return SkillDraftError(SkillDraftErrorCode.NOT_FOUND, str(exc))
    if code in {"SKILL_NAME_CONFLICT", "SKILL_VERSION_CONFLICT"}:
        return SkillDraftError(SkillDraftErrorCode.NAME_CONFLICT, str(exc))
    return SkillDraftError(SkillDraftErrorCode.INVALID, str(exc))


def _map_personal_code(exc: PersonalSkillError) -> SkillDraftErrorCode:
    if exc.code.value == "SKILL_NOT_FOUND":
        return SkillDraftErrorCode.NOT_FOUND
    if exc.code.value == "SKILL_NAME_CONFLICT":
        return SkillDraftErrorCode.NAME_CONFLICT
    return SkillDraftErrorCode.INVALID


__all__ = [
    "SkillDraftError",
    "SkillDraftErrorCode",
    "SkillDraftStore",
    "SkillDraftValidationResult",
    "SkillDraftView",
]
