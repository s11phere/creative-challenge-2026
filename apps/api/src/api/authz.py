"""Small authorization helpers shared by the service routers.

The website gateway has already authenticated the member.  These helpers make
the second, equally important check explicit: every Space, conversation and
run must belong to the app-scoped user supplied by that gateway.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException, Request
from infrastructure.repositories import SpaceRepository

from .service_auth import authenticated_app_user_id


def request_owner_id(request: Request, *, fallback: str = "local") -> str:
    """Resolve the tenant id, preserving local-client compatibility in dev."""

    return authenticated_app_user_id(request.scope) or fallback


async def require_space_access(request: Request, space_id: UUID) -> str:
    """Verify that the authenticated tenant owns ``space_id``.

    In local development (service authentication disabled), the historical
    behavior is retained.  Once the website profile is enabled, a missing or
    cross-tenant Space is deliberately reported as 404 to avoid enumeration.
    """

    owner_id = authenticated_app_user_id(request.scope)
    if owner_id is None:
        return "local"
    async with request.app.state.database.session() as session:
        space = await SpaceRepository(session).get(space_id)
    if space is None or space.owner_id != owner_id:
        raise HTTPException(status_code=404, detail="Space not found")
    return owner_id


def require_owner(request: Request, owner_id: str) -> None:
    """Reject an object that is not owned by the current app-scoped user."""

    expected = authenticated_app_user_id(request.scope)
    if expected is not None and owner_id != expected:
        raise HTTPException(status_code=404, detail="Resource not found")
