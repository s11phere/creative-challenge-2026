"""Read-only HTTP projection of the fixed, installed Skill set."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

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
    permissions: list[str]
    required_capabilities: list[str]
    budget: SkillBudgetResponse


@router.get("", response_model=list[SkillResponse])
async def list_skills(request: Request) -> list[SkillResponse]:
    """Expose the single installed version of every current Skill."""
    skills: list[SkillResponse] = []
    for summary in request.app.state.skill_catalog.list_skills():
        versions = request.app.state.skill_catalog.list_versions(summary.name)
        if len(versions) != 1:
            raise RuntimeError(f"Skill {summary.name} must have exactly one installed version")
        version = versions[0]
        skills.append(
            SkillResponse(
                name=version.name,
                version=version.version,
                content_sha256=version.content_sha256,
                description=version.description,
                permissions=list(version.permissions),
                required_capabilities=list(version.required_capabilities),
                budget=SkillBudgetResponse(**version.budget.__dict__),
            )
        )
    return skills


__all__ = ["router"]
