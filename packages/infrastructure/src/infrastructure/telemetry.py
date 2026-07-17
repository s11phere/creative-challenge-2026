"""OpenTelemetry provider and instrumentation setup."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from threading import Lock

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .config import Settings
from .logging_config import configure_logging

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ObservabilityRuntime:
    service_name: str
    provider: TracerProvider
    exporter_enabled: bool


_runtime: ObservabilityRuntime | None = None
_runtime_lock = Lock()
_clients_instrumented = False


def _trace_export_endpoint(endpoint: str) -> str:
    endpoint = endpoint.rstrip("/")
    if endpoint.endswith("/v1/traces"):
        return endpoint
    return endpoint + "/v1/traces"


def configure_observability(settings: Settings, *, service_name: str) -> ObservabilityRuntime:
    """Configure logging and tracing without connecting to the Collector."""
    global _clients_instrumented, _runtime

    configure_logging(
        service=service_name,
        environment=settings.app_env,
        level=settings.log_level,
        log_format=settings.log_format,
    )
    with _runtime_lock:
        if _runtime is None:
            provider = TracerProvider(
                resource=Resource.create(
                    {
                        "service.name": service_name,
                        "deployment.environment.name": settings.app_env,
                    }
                )
            )
            if settings.otlp_endpoint:
                exporter = OTLPSpanExporter(
                    endpoint=_trace_export_endpoint(settings.otlp_endpoint),
                    timeout=settings.otel_export_timeout_seconds,
                )
                provider.add_span_processor(
                    BatchSpanProcessor(
                        exporter,
                        export_timeout_millis=settings.otel_export_timeout_seconds * 1000,
                    )
                )
            trace.set_tracer_provider(provider)
            _runtime = ObservabilityRuntime(
                service_name=service_name,
                provider=provider,
                exporter_enabled=bool(settings.otlp_endpoint),
            )
        if not _clients_instrumented:
            RedisInstrumentor().instrument()
            HTTPXClientInstrumentor().instrument()
            _clients_instrumented = True
    logger.info(
        "observability_configured",
        extra={
            "operation": "otlp_export",
            "exporter_enabled": _runtime.exporter_enabled,
        },
    )
    return _runtime
