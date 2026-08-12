"""HTTP projection of Skill Creator drafts (Phase 4) and pattern suggestions.

Drafts are writable, incomplete packages under the personal Skill root; the
gate endpoints expose validation, the deterministic Phase 1 eval, and the
user-confirmed promotion to an active personal Skill. The suggestions endpoint
surfaces frequency-thresholded usage patterns (Phase 6 prelude) — read-only,
never auto-creating anything.
"""

from __future__ import annotations

from typing import Any

from application.skills import (
    SkillDraftError,
    SkillSuggestionView,
)
from fastapi import APIRouter, Request, status
from pydantic import BaseModel, Field

from ..errors import AppError

router = APIRouter(prefix="/api/v1/skills/personal/drafts", tags=["skill-drafts"])


class SkillDraftResponse(BaseModel):
    name: str
    description: str
    complete: bool
    valid: bool
    file_count: int
    files: list[str]


class SkillDraftValidationResponse(BaseModel):
    name: str
    valid: bool
    error: str | None = None
    description: str | None = None
    version: str | None = None
    content_sha256: str | None = None


class SkillDraftEvalCaseResponse(BaseModel):
    case_id: str
    status: str
    failure_categories: list[str]
    executed: bool
    latency_ms: float
    error: str | None = None
    evidence: list[dict[str, Any]]


class SkillDraftEvalMetricsResponse(BaseModel):
    total: int
    passed: int
    failed: int
    inconclusive: int
    errored: int
    pass_rate: float | None
    failure_category_counts: dict[str, int]
    checks_executed: int
    checks_passed: int
    check_pass_rate: float | None


class SkillDraftEvalResponse(BaseModel):
    skill_name: str
    skill_version: str
    gate_passed: bool
    metrics: SkillDraftEvalMetricsResponse
    cases: list[SkillDraftEvalCaseResponse]


class SkillDraftCreateRequest(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    files: dict[str, str] = Field(min_length=1)


class SkillDraftUpdateRequest(BaseModel):
    files: dict[str, str] = Field(min_length=1)


class SkillDraftFilesResponse(BaseModel):
    name: str
    files: dict[str, str]


class SkillSuggestionResponse(BaseModel):
    name: str
    category: str
    frequency: int
    last_seen_at: str
    description: str
    hint: str


def _app_error(exc: SkillDraftError) -> AppError:
    code = exc.code.value
    if code == "SKILL_DRAFT_NOT_FOUND":
        return AppError(code=code, message=str(exc), status_code=status.HTTP_404_NOT_FOUND)
    if code in {"SKILL_DRAFT_NAME_CONFLICT", "SKILL_DRAFT_EVAL_FAILED"}:
        return AppError(code=code, message=str(exc), status_code=status.HTTP_409_CONFLICT)
    return AppError(code=code, message=str(exc), status_code=status.HTTP_400_BAD_REQUEST)


def _to_response(view: Any) -> SkillDraftResponse:
    return SkillDraftResponse(
        name=view.name,
        description=view.description,
        complete=view.complete,
        valid=view.valid,
        file_count=view.file_count,
        files=list(view.files),
    )


def _eval_report_dict(report: Any) -> SkillDraftEvalResponse:
    metrics = report.metrics
    gate_passed = metrics.total > 0 and metrics.passed == metrics.total and metrics.errored == 0
    return SkillDraftEvalResponse(
        skill_name=report.skill_name,
        skill_version=report.skill_version,
        gate_passed=gate_passed,
        metrics=SkillDraftEvalMetricsResponse(
            total=metrics.total,
            passed=metrics.passed,
            failed=metrics.failed,
            inconclusive=metrics.inconclusive,
            errored=metrics.errored,
            pass_rate=metrics.pass_rate,
            failure_category_counts=dict(metrics.failure_category_counts),
            checks_executed=metrics.checks_executed,
            checks_passed=metrics.checks_passed,
            check_pass_rate=metrics.check_pass_rate,
        ),
        cases=[
            SkillDraftEvalCaseResponse(
                case_id=result.case_id,
                status=result.status.value,
                failure_categories=[category.value for category in result.failure_categories],
                executed=result.executed,
                latency_ms=round(result.latency_ms, 3),
                error=result.error,
                evidence=[
                    {"check": item.check, "passed": item.passed, "detail": item.detail}
                    for item in result.evidence
                ],
            )
            for result in report.cases
        ],
    )


def _suggestion_response(view: SkillSuggestionView) -> SkillSuggestionResponse:
    return SkillSuggestionResponse(
        name=view.name,
        category=view.category,
        frequency=view.frequency,
        last_seen_at=view.last_seen_at,
        description=view.description,
        hint=view.hint,
    )


@router.post("", response_model=SkillDraftResponse, status_code=status.HTTP_201_CREATED)
def create_draft(request: Request, body: SkillDraftCreateRequest) -> SkillDraftResponse:
    try:
        view = request.app.state.skill_draft_store.create(body.name, body.files)
    except SkillDraftError as exc:
        raise _app_error(exc) from exc
    return _to_response(view)


@router.get("", response_model=list[SkillDraftResponse])
def list_drafts(request: Request) -> list[SkillDraftResponse]:
    try:
        views = request.app.state.skill_draft_store.list()
    except SkillDraftError as exc:
        raise _app_error(exc) from exc
    return [_to_response(view) for view in views]


@router.get("/suggestions", response_model=list[SkillSuggestionResponse])
async def skill_suggestions(request: Request) -> list[SkillSuggestionResponse]:
    """Frequency-thresholded usage-pattern candidates (read-only, human-confirmed)."""
    existing = set(request.app.state.assistant_registry.names())
    suggestions = await request.app.state.skill_suggestion_service.suggest(
        existing_names=frozenset(existing)
    )
    return [_suggestion_response(view) for view in suggestions]


@router.get("/{name}", response_model=SkillDraftResponse)
def get_draft(request: Request, name: str) -> SkillDraftResponse:
    try:
        view = request.app.state.skill_draft_store.get(name)
    except SkillDraftError as exc:
        raise _app_error(exc) from exc
    return _to_response(view)


@router.get("/{name}/files", response_model=SkillDraftFilesResponse)
def get_draft_files(request: Request, name: str) -> SkillDraftFilesResponse:
    try:
        files = request.app.state.skill_draft_store.read_files(name)
    except SkillDraftError as exc:
        raise _app_error(exc) from exc
    return SkillDraftFilesResponse(name=name, files=files)


@router.put("/{name}", response_model=SkillDraftResponse)
def update_draft(request: Request, name: str, body: SkillDraftUpdateRequest) -> SkillDraftResponse:
    try:
        view = request.app.state.skill_draft_store.update(name, body.files)
    except SkillDraftError as exc:
        raise _app_error(exc) from exc
    return _to_response(view)


@router.delete("/{name}", response_model=dict[str, str])
def reject_draft(request: Request, name: str) -> dict[str, str]:
    try:
        request.app.state.skill_draft_store.delete(name)
    except SkillDraftError as exc:
        raise _app_error(exc) from exc
    return {"status": "rejected"}


@router.post("/{name}/validate", response_model=SkillDraftValidationResponse)
def validate_draft(request: Request, name: str) -> SkillDraftValidationResponse:
    try:
        result = request.app.state.skill_draft_store.validate(name)
    except SkillDraftError as exc:
        raise _app_error(exc) from exc
    return SkillDraftValidationResponse(
        name=result.name,
        valid=result.valid,
        error=result.error,
        description=result.description,
        version=result.version,
        content_sha256=result.content_sha256,
    )


@router.post("/{name}/eval", response_model=SkillDraftEvalResponse)
async def run_draft_eval(request: Request, name: str) -> SkillDraftEvalResponse:
    try:
        report = await request.app.state.skill_draft_store.run_eval(name)
    except SkillDraftError as exc:
        raise _app_error(exc) from exc
    return _eval_report_dict(report)


@router.post("/{name}/activate", response_model=dict[str, Any])
async def activate_draft(request: Request, name: str) -> dict[str, Any]:
    try:
        view = await request.app.state.skill_draft_store.activate(name)
    except SkillDraftError as exc:
        raise _app_error(exc) from exc
    return {
        "name": view.name,
        "version": view.version,
        "content_sha256": view.content_sha256,
        "description": view.description,
        "active": view.active,
    }


__all__ = ["router"]
