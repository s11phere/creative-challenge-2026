"""Tests for async database infrastructure."""

from infrastructure import database as database_module
from infrastructure.config import Settings
from infrastructure.database import Database
from infrastructure.telemetry_context import trace_parent_context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pytest import MonkeyPatch


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


async def test_database_span_keeps_parent_trace(monkeypatch: MonkeyPatch) -> None:
    trace_id = "1234567890abcdef1234567890abcdef"
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        database_module,
        "tracer",
        provider.get_tracer("test.database"),
    )
    database = Database("postgresql+asyncpg://app:password@127.0.0.1:1/agent_knowledge")

    with provider.get_tracer("test.request").start_as_current_span(
        "request",
        context=trace_parent_context(trace_id),
    ):
        assert await database.is_available(timeout_seconds=0.1) is False

    spans = exporter.get_finished_spans()
    assert [span.name for span in spans] == ["postgresql.ready", "request"]
    assert {format(span.context.trace_id, "032x") for span in spans} == {trace_id}
    assert spans[0].attributes["dependency.available"] is False
    await database.dispose()
