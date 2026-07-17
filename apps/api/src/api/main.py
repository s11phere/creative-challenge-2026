"""API entry point for the Agent Knowledge Repository."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, Response
from infrastructure.config import settings
from infrastructure.database import Database
from infrastructure.telemetry import configure_observability
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from pydantic import BaseModel

from .errors import ErrorResponse, register_error_handlers
from .observability import TraceMiddleware


class LiveResponse(BaseModel):
    status: Literal["alive"]


class DependencyCheck(BaseModel):
    healthy: bool
    code: str


class ReadinessChecks(BaseModel):
    postgresql: DependencyCheck
    redis: DependencyCheck


class ReadyResponse(BaseModel):
    status: Literal["ready", "degraded"]
    checks: ReadinessChecks


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    500: {
        "model": ErrorResponse,
        "description": "Unexpected server error",
    }
}


def create_app() -> FastAPI:
    """Application factory. Call once at process start."""

    database = Database(settings.database_url)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        settings.validate_secrets()
        observability = configure_observability(settings, service_name="api")
        database.instrument()
        try:
            yield
        finally:
            await database.dispose()
            await asyncio.to_thread(
                observability.provider.force_flush,
                int(settings.otel_export_timeout_seconds * 1000),
            )

    app = FastAPI(
        title="Agent Knowledge Repository",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.database = database

    app.add_middleware(TraceMiddleware)
    register_error_handlers(app)
    _register_routes(app)
    return app


def _register_routes(app: FastAPI) -> None:
    @app.get(
        "/api/v1/health/live",
        response_model=LiveResponse,
        responses=ERROR_RESPONSES,
    )
    async def live() -> LiveResponse:
        """Liveness probe — process event loop is responsive."""
        return LiveResponse(status="alive")

    @app.get(
        "/api/v1/health/ready",
        response_model=ReadyResponse,
        responses={
            503: {
                "model": ReadyResponse,
                "description": "One or more required dependencies are unavailable",
            },
            **ERROR_RESPONSES,
        },
    )
    async def ready(response: Response) -> ReadyResponse:
        """Readiness probe — concurrent dependency check with stable machine codes."""

        async def _check_postgres() -> DependencyCheck:
            if await app.state.database.is_available(timeout_seconds=3):
                return DependencyCheck(healthy=True, code="POSTGRESQL_OK")
            return DependencyCheck(healthy=False, code="POSTGRESQL_UNREACHABLE")

        async def _check_redis() -> DependencyCheck:
            import redis.asyncio as aioredis  # noqa: PLC0415

            tracer = trace.get_tracer("api.dependencies")
            with tracer.start_as_current_span(
                "redis.ping",
                kind=SpanKind.CLIENT,
                attributes={"db.system.name": "redis", "server.address": settings.redis_host},
            ):
                try:
                    r = aioredis.from_url(settings.redis_url, socket_timeout=3)
                    await r.ping()
                    await r.aclose()
                    return DependencyCheck(healthy=True, code="REDIS_OK")
                except Exception:
                    return DependencyCheck(healthy=False, code="REDIS_UNREACHABLE")

        pg_result, redis_result = await asyncio.gather(_check_postgres(), _check_redis())

        all_healthy = pg_result.healthy and redis_result.healthy
        if not all_healthy:
            response.status_code = 503

        return ReadyResponse(
            status="ready" if all_healthy else "degraded",
            checks=ReadinessChecks(postgresql=pg_result, redis=redis_result),
        )


app = create_app()
