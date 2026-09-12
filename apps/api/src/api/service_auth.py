"""Authentication and tenant context for the private website gateway.

The public website authenticates members itself.  Requests forwarded to this
service therefore carry two short-lived, service-to-service headers:

``X-Internal-Service-Token``
    A deployment secret shared by the website gateway and this service.
``X-App-Scoped-User-Id``
    An opaque, non-PII tenant identifier minted by the website.

Local development keeps the historical unauthenticated mode unless
``SERVICE_AUTH_REQUIRED=true`` is configured.  Production deployments should
always enable it; browser session tokens must never be sent to this service.
"""

from __future__ import annotations

import json
import re
from hashlib import sha256
from hmac import compare_digest
from typing import Final

from infrastructure.config import settings
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

SERVICE_TOKEN_HEADER: Final[str] = "X-Internal-Service-Token"
APP_USER_HEADER: Final[str] = "X-App-Scoped-User-Id"
APP_USER_SCOPE_KEY: Final[str] = "app_user_id"
_USER_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def _secret_value(value: object) -> str:
    getter = getattr(value, "get_secret_value", None)
    return str(getter()) if callable(getter) else str(value or "")


def app_user_id_from_scope(scope: Scope, *, fallback: str | None = None) -> str:
    """Return the authenticated app-scoped user id for a request.

    ``fallback`` exists solely for backwards-compatible local development. In
    service-auth mode the middleware always populates the scope and a missing
    value is treated as an authentication error by callers.
    """

    value = scope.get(APP_USER_SCOPE_KEY)
    if isinstance(value, str) and value:
        return value
    if settings.service_auth_required:
        raise PermissionError("Missing app-scoped user context")
    return fallback or "local"


def authenticated_app_user_id(scope: Scope) -> str | None:
    """Return a tenant id only when the gateway actually authenticated it."""

    value = scope.get(APP_USER_SCOPE_KEY)
    return value if isinstance(value, str) and value else None


def valid_app_user_id(value: str | None) -> bool:
    """Validate an opaque tenant id without accepting PII or header injection."""

    return bool(value and _USER_ID_PATTERN.fullmatch(value))


class ServiceAuthMiddleware:
    """Authenticate website-to-service calls and attach tenant context.

    Health probes remain unauthenticated so Docker can determine liveness and
    readiness.  All other API requests require both headers when enabled.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", "/"))
        if path in {"/api/v1/health/live", "/api/v1/health/ready"}:
            await self.app(scope, receive, send)
            return

        if settings.public_mode and _is_restricted_public_path(
            path, str(scope.get("method", "GET"))
        ):
            await _not_available(send)
            return

        if not settings.service_auth_required:
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        expected = (
            _secret_value(settings.internal_service_token)
            if settings.internal_service_token is not None
            else ""
        )
        supplied = headers.get(SERVICE_TOKEN_HEADER, "")
        user_id = headers.get(APP_USER_HEADER)
        if not expected or not compare_digest(supplied, expected) or not valid_app_user_id(user_id):
            await _unauthorized(send)
            return

        # Scope values are internal and never echoed to the client or logs.
        scope[APP_USER_SCOPE_KEY] = user_id
        await self.app(scope, receive, send)


async def _unauthorized(send: Send) -> None:
    payload = json.dumps(
        {
            "code": "SERVICE_AUTH_REQUIRED",
            "message": "A valid website service credential is required.",
        },
        separators=(",", ":"),
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


def _is_restricted_public_path(path: str, method: str) -> bool:
    """Keep the website's first release limited to knowledge workflows.

    Local workspaces, personal Skill authoring and activation are intentionally
    left available in the standalone developer mode but are not exposed by the
    production website profile.
    """

    if path.startswith("/api/v1/skills/personal"):
        return True
    if path.startswith("/api/v3/runs/"):
        return True
    if path in {"/api/v2/commands"}:
        return True
    if path.startswith("/api/v2/conversations/") and path.endswith("/workspace"):
        return True
    return path.startswith("/api/v1/skills/") and method != "GET"


async def _not_available(send: Send) -> None:
    payload = (
        b'{"code":"CAPABILITY_NOT_EXPOSED",'
        b'"message":"This capability is disabled in the website deployment."}'
    )
    await send(
        {
            "type": "http.response.start",
            "status": 404,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})


def service_token_fingerprint() -> str | None:
    """Return a non-secret fingerprint for startup diagnostics."""

    if settings.internal_service_token is None:
        return None
    return sha256(_secret_value(settings.internal_service_token).encode()).hexdigest()[:12]
