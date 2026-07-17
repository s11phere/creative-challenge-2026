"""Async SQLAlchemy engine, sessions, and transaction boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


class Database:
    """Own an async engine without connecting during construction."""

    def __init__(self, url: str | URL) -> None:
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self.session_factory = async_sessionmaker(
            bind=self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

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
        try:
            async with asyncio.timeout(timeout_seconds):
                async with self.engine.connect() as connection:
                    await connection.execute(text("SELECT 1"))
        except Exception:
            return False
        return True

    async def dispose(self) -> None:
        """Release pooled connections during application shutdown."""
        await self.engine.dispose()
