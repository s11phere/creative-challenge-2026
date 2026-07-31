"""API entry point for the Agent Knowledge Repository."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal

from application.qa.persistence import InMemoryGroundedQARepository
from domain.qa_sse import QAEventLog
from fastapi import FastAPI, Response
from infrastructure.config import settings
from infrastructure.database import Database
from infrastructure.telemetry import configure_observability
from model_gateway import (
    GatewayConfig,
    ModelGateway,
    ModelProvider,
    create_model_gateway,
)
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from pydantic import BaseModel

from .errors import ErrorResponse, register_error_handlers
from .observability import TraceMiddleware
from .qa_runtime import InProcessQARuntime
from .routers import qa, search, sources


class LiveResponse(BaseModel):
    status: Literal["alive"]


class DependencyCheck(BaseModel):
    healthy: bool
    code: str


class ModelDependencyCheck(DependencyCheck):
    capabilities: dict[str, DependencyCheck]


class ReadinessChecks(BaseModel):
    postgresql: DependencyCheck
    redis: DependencyCheck
    model: ModelDependencyCheck


class ReadyResponse(BaseModel):
    status: Literal["ready", "degraded"]
    checks: ReadinessChecks


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    500: {
        "model": ErrorResponse,
        "description": "Unexpected server error",
    }
}


def create_app(
    model_gateway: ModelGateway | None = None,
    *,
    database: Database | None = None,
    enable_qa_execution: bool = True,
) -> FastAPI:
    """Application factory. Call once at process start."""

    database = database or Database(settings.database_url)
    gateway = model_gateway or _create_configured_model_gateway()
    qa_repository = InMemoryGroundedQARepository()
    qa_event_log = QAEventLog()
    qa_runtime = InProcessQARuntime(
        database=database,
        gateway=gateway,
        repository=qa_repository,
        events=qa_event_log,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        settings.validate_secrets()
        observability = configure_observability(settings, service_name="api")
        database.instrument()
        try:
            yield
        finally:
            await qa_runtime.aclose()
            await database.dispose()
            await gateway.aclose()
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
    app.state.model_gateway = gateway
    app.state.qa_repository = qa_repository
    app.state.qa_event_log = qa_event_log
    app.state.qa_runtime = qa_runtime
    app.state.qa_execution_enabled = enable_qa_execution

    app.add_middleware(TraceMiddleware)
    register_error_handlers(app)
    _register_routes(app)
    return app


def _register_routes(app: FastAPI) -> None:
    app.include_router(sources.router)
    app.include_router(search.router)
    app.include_router(qa.router)

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
        model_status = app.state.model_gateway.status
        model_result = ModelDependencyCheck(
            healthy=model_status.available,
            code=model_status.code,
            capabilities={
                capability.capability.value: DependencyCheck(
                    healthy=capability.available,
                    code=capability.code,
                )
                for capability in model_status.capability_statuses
            },
        )

        all_healthy = pg_result.healthy and redis_result.healthy
        if not all_healthy:
            response.status_code = 503

        return ReadyResponse(
            status="ready" if all_healthy else "degraded",
            checks=ReadinessChecks(
                postgresql=pg_result,
                redis=redis_result,
                model=model_result,
            ),
        )


def _create_configured_model_gateway() -> ModelGateway:
    api_key = settings.model_api_key.get_secret_value() if settings.model_api_key else None
    fast_chat_api_key = (
        settings.fast_chat_api_key.get_secret_value() if settings.fast_chat_api_key else None
    )
    embedding_api_key = (
        settings.embedding_api_key.get_secret_value() if settings.embedding_api_key else None
    )
    reranker_api_key = (
        settings.reranker_api_key.get_secret_value() if settings.reranker_api_key else None
    )
    return create_model_gateway(
        GatewayConfig(
            provider=ModelProvider(settings.model_provider),
            endpoint=settings.model_endpoint,
            api_key=api_key,
            fast_chat_endpoint=settings.fast_chat_endpoint,
            fast_chat_api_key=fast_chat_api_key,
            fast_chat_model=settings.fast_chat_model,
            embedding_endpoint=settings.embedding_endpoint,
            embedding_api_key=embedding_api_key,
            embedding_model=settings.embedding_model,
            reranker_endpoint=settings.reranker_endpoint,
            reranker_api_key=reranker_api_key,
            reranker_model=settings.reranker_model,
            embedding_protocol=settings.embedding_protocol,
            allow_external=settings.model_allow_external,
            timeout_seconds=settings.model_timeout_seconds,
            max_retries=settings.model_max_retries,
            retry_backoff_seconds=settings.model_retry_backoff_seconds,
        )
    )


app = create_app()
