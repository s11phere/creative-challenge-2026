"""HTTP projection and live activation controls for installed Skills."""

from __future__ import annotations

from typing import cast

from agent_runtime import FileSystemSkillRegistry
from application.skills import (
    SkillCatalogPort,
    SkillLifecycleError,
    SkillLifecycleErrorCode,
    SkillVersionView,
)
from fastapi import APIRouter, Request, status
from pydantic import BaseModel

from ..errors import AppError

router = APIRouter(prefix="/api/v1/skills", tags=["skills"])


class SkillBudgetResponse(BaseModel):
    max_steps: int
    max_tool_calls: int
    max_input_tokens: int
    max_output_tokens: int
    timeout_seconds: int


class SkillResponse(BaseModel):
    name: str
    version: str
    content_sha256: str
    description: str
    active: bool
    permissions: list[str]
    required_capabilities: list[str]
    budget: SkillBudgetResponse


class SkillActivationRequest(BaseModel):
    active: bool


def _active(request: Request, name: str, version: str) -> bool:
    registry = cast(FileSystemSkillRegistry, request.app.state.assistant_registry)
    try:
        return registry.active_version(name) == version
    except Exception:
        return False


def _to_response(request: Request, version: SkillVersionView) -> SkillResponse:
    name = version.name
    version_name = version.version
    budget = version.budget
    return SkillResponse(
        name=name,
        version=version_name,
        content_sha256=version.content_sha256,
        description=version.description,
        active=_active(request, name, version_name),
        permissions=list(version.permissions),
        required_capabilities=list(version.required_capabilities),
        budget=SkillBudgetResponse(**budget.__dict__),
    )


def _version(request: Request, name: str) -> SkillVersionView:
    catalog = cast(SkillCatalogPort, request.app.state.skill_catalog)
    versions = catalog.list_versions(name)
    if len(versions) != 1:
        raise RuntimeError(f"Skill {name} must have exactly one installed version")
    return versions[0]


@router.get("", response_model=list[SkillResponse])
async def list_skills(request: Request) -> list[SkillResponse]:
    """Expose installed Skills with their current Assistant activation state."""
    return [
        _to_response(request, _version(request, summary.name))
        for summary in request.app.state.skill_catalog.list_skills()
    ]


@router.patch("/{name}/activation", response_model=SkillResponse)
async def set_skill_activation(
    request: Request, name: str, body: SkillActivationRequest
) -> SkillResponse:
    """Apply a durable activation toggle to the next Assistant turn immediately."""
    try:
        await request.app.state.skill_activation_service.set_active(name, body.active)
        return _to_response(request, _version(request, name))
    except SkillLifecycleError as exc:
        status_code = (
            status.HTTP_404_NOT_FOUND
            if exc.code is SkillLifecycleErrorCode.NOT_FOUND
            else status.HTTP_400_BAD_REQUEST
        )
        raise AppError(code=exc.code.value, message=str(exc), status_code=status_code) from exc


__all__ = ["router"]
