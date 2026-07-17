"""Tests for async database infrastructure."""

from infrastructure.config import Settings
from infrastructure.database import Database


async def test_database_engine_is_lazy_and_disposable() -> None:
    settings = Settings(
        postgres_user="user@example.com",
        postgres_password="p/a:ss word",
        postgres_db="knowledge db",
    )

    database = Database(settings.database_url)

    assert database.engine.url.username == "user@example.com"
    assert database.engine.url.password == "p/a:ss word"
    assert database.engine.url.database == "knowledge db"
    await database.dispose()


async def test_database_availability_returns_false_without_connection_details() -> None:
    database = Database("postgresql+asyncpg://app:password@127.0.0.1:1/agent_knowledge")

    assert await database.is_available(timeout_seconds=0.1) is False
    await database.dispose()
