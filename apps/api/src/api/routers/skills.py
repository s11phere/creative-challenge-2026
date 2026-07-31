"""Read-only HTTP projection of installed and active Skills."""

from __future__ import annotations

from typing import NoReturn

from application.skills import SkillLifecycleError, SkillLifecycleErrorCode
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..errors import AppError, ErrorResponse

router = APIRouter(prefix="/api/v1/skills", tags=["skills"])


class SkillBudgetResponse(BaseModel):
    max_steps: int
    max_tool_calls: int
    max_input_tokens: int
    max_output_tokens: int
    timeout_seconds: int


class SkillSummaryResponse(BaseModel):
    name: str
    active_version: str | None
    active_revision: int | None
    versions: list[str]


class SkillVersionResponse(BaseModel):
    name: str
    version: str
    content_sha256: str
    description: str
    active: bool
    permissions: list[str]
    required_capabilities: list[str]
    budget: SkillBudgetResponse


class SkillActivationRequest(BaseModel):
    version: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=1)


class SkillActivationResponse(BaseModel):
    name: str
    version: str
    content_sha256: str
    revision: int


async def _synchronize(request: Request, name: str) -> None:
    try:
        activation = await request.app.state.skill_lifecycle.current(name)
    except SkillLifecycleError as exc:
        _raise_lifecycle_error(exc)
    setter = getattr(request.app.state.skill_catalog, "set_active_revision", None)
    if setter is not None:
        setter(name, activation.revision)


@router.get("", response_model=list[SkillSummaryResponse])
async def list_skills(request: Request) -> list[SkillSummaryResponse]:
    for name in request.app.state.skill_catalog.list_skills():
        await _synchronize(request, name.name)
    return [
        SkillSummaryResponse(
            name=skill.name,
            active_version=skill.active_version,
            active_revision=skill.active_revision,
            versions=list(skill.versions),
        )
        for skill in request.app.state.skill_catalog.list_skills()
    ]


@router.get("/{skill_name}/versions", response_model=list[SkillVersionResponse])
async def list_skill_versions(skill_name: str, request: Request) -> list[SkillVersionResponse]:
    await _synchronize(request, skill_name)
    versions = request.app.state.skill_catalog.list_versions(skill_name)
    if not versions:
        raise HTTPException(status_code=404, detail="Skill not found")
    return [
        SkillVersionResponse(
            name=version.name,
            version=version.version,
            content_sha256=version.content_sha256,
            description=version.description,
            active=version.active,
            permissions=list(version.permissions),
            required_capabilities=list(version.required_capabilities),
            budget=SkillBudgetResponse(**version.budget.__dict__),
        )
        for version in versions
    ]


@router.put(
    "/{skill_name}/active",
    response_model=SkillActivationResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def activate_skill(
    skill_name: str, body: SkillActivationRequest, request: Request
) -> SkillActivationResponse:
    return await _change_activation(skill_name, body, request, rollback=False)


@router.post(
    "/{skill_name}/rollback",
    response_model=SkillActivationResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}},
)
async def rollback_skill(
    skill_name: str, body: SkillActivationRequest, request: Request
) -> SkillActivationResponse:
    return await _change_activation(skill_name, body, request, rollback=True)


async def _change_activation(
    skill_name: str,
    body: SkillActivationRequest,
    request: Request,
    *,
    rollback: bool,
) -> SkillActivationResponse:
    try:
        activation = await request.app.state.skill_lifecycle.activate(
            skill_name,
            body.version,
            expected_revision=body.expected_revision,
            rollback=rollback,
        )
    except SkillLifecycleError as exc:
        _raise_lifecycle_error(exc)
    setter = getattr(request.app.state.skill_catalog, "set_active_revision", None)
    if setter is not None:
        setter(skill_name, activation.revision)
    return SkillActivationResponse(**activation.__dict__)


def _raise_lifecycle_error(exc: SkillLifecycleError) -> NoReturn:
    status = 404 if exc.code is SkillLifecycleErrorCode.NOT_FOUND else 409
    raise AppError(exc.code.value, str(exc), status) from exc


__all__ = ["router"]
