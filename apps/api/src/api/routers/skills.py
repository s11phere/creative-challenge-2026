"""Read-only HTTP projection of installed and active Skills."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

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


@router.get("", response_model=list[SkillSummaryResponse])
async def list_skills(request: Request) -> list[SkillSummaryResponse]:
    return [
        SkillSummaryResponse(
            name=skill.name,
            active_version=skill.active_version,
            versions=list(skill.versions),
        )
        for skill in request.app.state.skill_catalog.list_skills()
    ]


@router.get("/{skill_name}/versions", response_model=list[SkillVersionResponse])
async def list_skill_versions(skill_name: str, request: Request) -> list[SkillVersionResponse]:
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


__all__ = ["router"]
