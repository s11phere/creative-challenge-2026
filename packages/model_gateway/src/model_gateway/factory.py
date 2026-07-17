"""Policy-aware ModelGateway construction."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .contracts import ModelErrorCode, ModelGateway, ModelProvider
from .fake import FakeModelGateway, FakeScenario
from .openai_compatible import OpenAICompatibleGateway
from .unavailable import UnavailableModelGateway


@dataclass(frozen=True)
class GatewayConfig:
    provider: ModelProvider = ModelProvider.FAKE
    endpoint: str | None = None
    api_key: str | None = None
    fast_chat_model: str | None = None
    embedding_model: str | None = None
    allow_external: bool = False
    timeout_seconds: float = 15.0
    max_retries: int = 2
    retry_backoff_seconds: float = 0.1
    fake_scenario: FakeScenario = FakeScenario.NORMAL


def create_model_gateway(
    config: GatewayConfig,
    *,
    client: httpx.AsyncClient | None = None,
) -> ModelGateway:
    if config.provider is ModelProvider.FAKE:
        return FakeModelGateway(scenario=config.fake_scenario)
    if config.provider is ModelProvider.DISABLED:
        return UnavailableModelGateway(
            provider=config.provider,
            status_code="MODEL_DISABLED",
            error_code=ModelErrorCode.UNAVAILABLE,
            message="The model provider is disabled.",
        )
    if not config.endpoint or not config.fast_chat_model or not config.embedding_model:
        return UnavailableModelGateway(
            provider=config.provider,
            status_code="MODEL_CONFIGURATION_MISSING",
            error_code=ModelErrorCode.UNAVAILABLE,
            message="The model provider is not configured.",
        )
    if not _endpoint_allowed(config.endpoint, allow_external=config.allow_external):
        return UnavailableModelGateway(
            provider=config.provider,
            status_code="MODEL_POLICY_DENIED",
            error_code=ModelErrorCode.POLICY_DENIED,
            message="The model provider is blocked by the data policy.",
        )
    return OpenAICompatibleGateway(
        endpoint=config.endpoint,
        fast_chat_model=config.fast_chat_model,
        embedding_model=config.embedding_model,
        api_key=config.api_key,
        timeout_seconds=config.timeout_seconds,
        max_retries=config.max_retries,
        retry_backoff_seconds=config.retry_backoff_seconds,
        client=client,
    )


def _endpoint_allowed(endpoint: str, *, allow_external: bool) -> bool:
    try:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            return False
        host = parsed.hostname.lower()
        if host in {"localhost", "host.docker.internal"}:
            return True
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return allow_external
        return address.is_loopback or address.is_private or allow_external
    except ValueError:
        return False
