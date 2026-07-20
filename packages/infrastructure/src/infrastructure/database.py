"""Async SQLAlchemy engine, sessions, and transaction boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from opentelemetry import trace
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.trace import SpanKind, Status, StatusCode
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import Pool

tracer = trace.get_tracer("infrastructure.database")


class Database:
    """Own an async engine without connecting during construction."""

    def __init__(self, url: str | URL, *, poolclass: type[Pool] | None = None) -> None:
        engine_options: dict[str, object] = {"pool_pre_ping": True}
        if poolclass is not None:
            engine_options["poolclass"] = poolclass
        self.engine: AsyncEngine = create_async_engine(url, **engine_options)
        self._instrumented = False
        self.session_factory = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    def instrument(self) -> None:
        """Attach OTel SQLAlchemy hooks without recording SQL parameters."""
        if self._instrumented:
            return
        SQLAlchemyInstrumentor().instrument(
            engine=self.engine.sync_engine,
            enable_commenter=False,
        )
        self._instrumented = True

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session whose lifetime is bounded by the context."""
        async with self.session_factory() as session:
            yield session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """Commit on success and roll back on failure."""
        async with self.session_factory() as session, session.begin():
            yield session

    async def is_available(self, *, timeout_seconds: float = 3.0) -> bool:
        """Run a bounded connection check without exposing connection details."""
        with tracer.start_as_current_span(
            "postgresql.ready",
            kind=SpanKind.CLIENT,
            attributes={
                "db.system.name": "postgresql",
                "server.address": self.engine.url.host or "unknown",
            },
        ) as span:
            try:
                async with asyncio.timeout(timeout_seconds):
                    async with self.engine.connect() as connection:
                        await connection.execute(text("SELECT 1"))
            except Exception:
                span.set_attribute("dependency.available", False)
                span.set_status(Status(StatusCode.ERROR))
                return False
            span.set_attribute("dependency.available", True)
            return True

    async def dispose(self) -> None:
        """Release pooled connections during application shutdown."""
        await self.engine.dispose()
