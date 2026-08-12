"""CRUD + activation HTTP projection for writable personal Skills (Phase 3)."""

from __future__ import annotations

from agent_runtime import SkillRegistryError
from application.skills import PersonalSkillError, PersonalSkillView
from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, Field

from ..errors import AppError

router = APIRouter(prefix="/api/v1/skills/personal", tags=["personal-skills"])

_NOT_FOUND_CODES = frozenset({"SKILL_NOT_FOUND", "SKILL_ACTIVE_VERSION_MISSING"})
_CONFLICT_CODES = frozenset(
    {"SKILL_NAME_CONFLICT", "SKILL_VERSION_CONFLICT", "SKILL_CLEANUP_BLOCKED", "SKILL_ACTIVE"}
)


class PersonalSkillBudgetResponse(BaseModel):
    max_steps: int
    max_tool_calls: int
    max_input_tokens: int
    max_output_tokens: int
    timeout_seconds: int


class PersonalSkillInvocationResponse(BaseModel):
    command: str
    aliases: list[str]
    argument_hint: str
    input_mode: str
    execution_mode: str


class PersonalSkillResponse(BaseModel):
    name: str
    version: str
    content_sha256: str
    description: str
    active: bool
    permissions: list[str]
    required_capabilities: list[str]
    budget: PersonalSkillBudgetResponse
    invocation: PersonalSkillInvocationResponse | None = None


class PersonalSkillCreateRequest(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    files: dict[str, str] = Field(min_length=1)


class PersonalSkillUpdateRequest(BaseModel):
    files: dict[str, str] = Field(min_length=1)


def _app_error(exc: Exception) -> AppError:
    code = str(getattr(exc, "code", "SKILL_INVALID"))
    if code in _NOT_FOUND_CODES:
        return AppError(code=code, message=str(exc), status_code=status.HTTP_404_NOT_FOUND)
    if code in _CONFLICT_CODES:
        return AppError(code=code, message=str(exc), status_code=status.HTTP_409_CONFLICT)
    return AppError(code=code, message=str(exc), status_code=status.HTTP_400_BAD_REQUEST)


def _to_response(view: PersonalSkillView) -> PersonalSkillResponse:
    invocation = None
    if view.invocation is not None:
        invocation = PersonalSkillInvocationResponse(
            command=view.invocation.command,
            aliases=list(view.invocation.aliases),
            argument_hint=view.invocation.argument_hint,
            input_mode=view.invocation.input_mode,
            execution_mode=view.invocation.execution_mode,
        )
    return PersonalSkillResponse(
        name=view.name,
        version=view.version,
        content_sha256=view.content_sha256,
        description=view.description,
        active=view.active,
        permissions=list(view.permissions),
        required_capabilities=list(view.required_capabilities),
        budget=PersonalSkillBudgetResponse(**view.budget.__dict__),
        invocation=invocation,
    )


@router.post("", response_model=PersonalSkillResponse, status_code=status.HTTP_201_CREATED)
def create_personal_skill(
    request: Request, body: PersonalSkillCreateRequest
) -> PersonalSkillResponse:
    """Create one writable personal Skill; every file is validated before publishing."""
    try:
        view = request.app.state.personal_skill_store.create(body.name, body.files)
    except (PersonalSkillError, SkillRegistryError) as exc:
        raise _app_error(exc) from exc
    return _to_response(view)


@router.get("", response_model=list[PersonalSkillResponse])
def list_personal_skills(request: Request) -> list[PersonalSkillResponse]:
    """List installed personal Skills (body-free metadata)."""
    return [_to_response(view) for view in request.app.state.personal_skill_store.list()]


@router.get("/{name}", response_model=PersonalSkillResponse)
def get_personal_skill(request: Request, name: str) -> PersonalSkillResponse:
    try:
        view = request.app.state.personal_skill_store.get(name)
    except (PersonalSkillError, SkillRegistryError) as exc:
        raise _app_error(exc) from exc
    return _to_response(view)


@router.put("/{name}", response_model=PersonalSkillResponse)
def update_personal_skill(
    request: Request, name: str, body: PersonalSkillUpdateRequest
) -> PersonalSkillResponse:
    try:
        view = request.app.state.personal_skill_store.update(name, body.files)
    except (PersonalSkillError, SkillRegistryError) as exc:
        raise _app_error(exc) from exc
    return _to_response(view)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_personal_skill(request: Request, name: str) -> Response:
    try:
        request.app.state.personal_skill_store.delete(name)
    except (PersonalSkillError, SkillRegistryError) as exc:
        raise _app_error(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{name}/activate", response_model=PersonalSkillResponse)
async def activate_personal_skill(request: Request, name: str) -> PersonalSkillResponse:
    try:
        view = await request.app.state.personal_skill_store.activate(name)
    except (PersonalSkillError, SkillRegistryError) as exc:
        raise _app_error(exc) from exc
    return _to_response(view)


__all__ = ["router"]
