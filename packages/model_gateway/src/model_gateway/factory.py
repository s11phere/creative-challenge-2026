"""Policy-aware ModelGateway construction."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .contracts import CapabilityAlias, ModelErrorCode, ModelGateway, ModelProvider
from .fake import FakeModelGateway, FakeScenario
from .openai_compatible import OpenAICompatibleGateway
from .routed import CapabilityRoutedModelGateway
from .unavailable import UnavailableModelGateway


@dataclass(frozen=True)
class GatewayConfig:
    provider: ModelProvider = ModelProvider.FAKE
    endpoint: str | None = None
    api_key: str | None = None
    fast_chat_endpoint: str | None = None
    fast_chat_api_key: str | None = None
    fast_chat_model: str | None = None
    embedding_endpoint: str | None = None
    embedding_api_key: str | None = None
    embedding_model: str | None = None
    reranker_endpoint: str | None = None
    reranker_api_key: str | None = None
    reranker_model: str | None = None
    allow_external: bool = False
    timeout_seconds: float = 15.0
    fast_chat_timeout_seconds: float = 120.0
    fast_chat_reasoning_enabled: bool = False
    max_retries: int = 2
    retry_backoff_seconds: float = 0.1
    fake_scenario: FakeScenario = FakeScenario.NORMAL
    embedding_protocol: str = "openai-compatible"
    embedding_provider: ModelProvider | None = None
    fake_embedding: bool = False
    fake_reranker: bool = False


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
    if config.provider is ModelProvider.TEXT_EMBEDDINGS_INFERENCE:
        endpoint = config.embedding_endpoint or config.endpoint
        status, error = _capability_configuration_status(
            endpoint,
            config.embedding_model,
            allow_external=config.allow_external,
        )
        reranker_endpoint = config.reranker_endpoint
        reranker_status, reranker_error = _capability_configuration_status(
            reranker_endpoint,
            config.reranker_model,
            allow_external=config.allow_external,
        )
        gateway = _create_tei_gateway(
            config,
            client=client,
            embedding_status=status,
            embedding_error=error,
            reranker_status=reranker_status,
            reranker_error=reranker_error,
        )
        return _with_fake_fallback(gateway, config)
    chat_endpoint = config.fast_chat_endpoint or config.endpoint
    embedding_endpoint = config.embedding_endpoint or config.endpoint
    chat_status, chat_error = _capability_configuration_status(
        chat_endpoint,
        config.fast_chat_model,
        allow_external=config.allow_external,
    )
    embedding_status, embedding_error = _capability_configuration_status(
        embedding_endpoint,
        config.embedding_model,
        allow_external=config.allow_external,
    )
    reranker_endpoint = config.reranker_endpoint or config.endpoint
    reranker_status, reranker_error = _capability_configuration_status(
        reranker_endpoint,
        config.reranker_model,
        allow_external=config.allow_external,
    )
    gateway = OpenAICompatibleGateway(
        endpoint=None,
        fast_chat_endpoint=chat_endpoint if chat_error is None else None,
        embedding_endpoint=embedding_endpoint if embedding_error is None else None,
        fast_chat_model=config.fast_chat_model,
        embedding_model=config.embedding_model,
        api_key=config.api_key,
        fast_chat_api_key=config.fast_chat_api_key,
        embedding_api_key=config.embedding_api_key,
        reranker_endpoint=reranker_endpoint if reranker_error is None else None,
        reranker_api_key=config.reranker_api_key or config.api_key,
        reranker_model=config.reranker_model,
        fast_chat_status_code=chat_status,
        embedding_status_code=embedding_status,
        fast_chat_error_code=chat_error or ModelErrorCode.UNAVAILABLE,
        embedding_error_code=embedding_error or ModelErrorCode.UNAVAILABLE,
        reranker_status_code=reranker_status,
        reranker_error_code=reranker_error or ModelErrorCode.UNAVAILABLE,
        provider=config.provider,
        embedding_protocol=config.embedding_protocol,
        timeout_seconds=config.timeout_seconds,
        fast_chat_timeout_seconds=config.fast_chat_timeout_seconds,
        fast_chat_reasoning_enabled=config.fast_chat_reasoning_enabled,
        max_retries=config.max_retries,
        retry_backoff_seconds=config.retry_backoff_seconds,
        client=client,
    )
    routed: ModelGateway = gateway
    if config.embedding_provider is ModelProvider.TEXT_EMBEDDINGS_INFERENCE:
        status, error = _capability_configuration_status(
            config.embedding_endpoint,
            config.embedding_model,
            allow_external=config.allow_external,
        )
        embedding_gateway = _create_tei_gateway(
            config,
            client=client,
            embedding_status=status,
            embedding_error=error,
            reranker_status="MODEL_CONFIGURATION_MISSING",
            reranker_error=ModelErrorCode.UNAVAILABLE,
        )
        routed = CapabilityRoutedModelGateway(
            primary=routed,
            fallback=embedding_gateway,
            fallback_capabilities=frozenset({CapabilityAlias.EMBEDDING_ZH}),
        )
    return _with_fake_fallback(routed, config)


def _create_tei_gateway(
    config: GatewayConfig,
    *,
    client: httpx.AsyncClient | None,
    embedding_status: str,
    embedding_error: ModelErrorCode | None,
    reranker_status: str,
    reranker_error: ModelErrorCode | None,
) -> ModelGateway:
    return OpenAICompatibleGateway(
        embedding_endpoint=(
            config.embedding_endpoint or config.endpoint if embedding_error is None else None
        ),
        embedding_model=config.embedding_model,
        reranker_endpoint=config.reranker_endpoint if reranker_error is None else None,
        reranker_api_key=config.reranker_api_key or config.api_key,
        reranker_model=config.reranker_model,
        embedding_api_key=config.embedding_api_key or config.api_key,
        embedding_status_code=embedding_status,
        embedding_error_code=embedding_error or ModelErrorCode.UNAVAILABLE,
        reranker_status_code=reranker_status,
        reranker_error_code=reranker_error or ModelErrorCode.UNAVAILABLE,
        timeout_seconds=config.timeout_seconds,
        fast_chat_timeout_seconds=config.fast_chat_timeout_seconds,
        fast_chat_reasoning_enabled=config.fast_chat_reasoning_enabled,
        max_retries=config.max_retries,
        retry_backoff_seconds=config.retry_backoff_seconds,
        client=client,
        provider=ModelProvider.TEXT_EMBEDDINGS_INFERENCE,
        embedding_protocol="tei",
    )


def _with_fake_fallback(gateway: ModelGateway, config: GatewayConfig) -> ModelGateway:
    capabilities = frozenset(
        capability
        for enabled, capability in (
            (config.fake_embedding, CapabilityAlias.EMBEDDING_ZH),
            (config.fake_reranker, CapabilityAlias.RERANKER_MULTILINGUAL),
        )
        if enabled
    )
    if not capabilities:
        return gateway
    return CapabilityRoutedModelGateway(
        primary=gateway,
        fallback=FakeModelGateway(scenario=config.fake_scenario),
        fallback_capabilities=capabilities,
    )


def _capability_configuration_status(
    endpoint: str | None,
    model: str | None,
    *,
    allow_external: bool,
) -> tuple[str, ModelErrorCode | None]:
    if not endpoint or not model:
        return "MODEL_CONFIGURATION_MISSING", ModelErrorCode.UNAVAILABLE
    if not _endpoint_allowed(endpoint, allow_external=allow_external):
        return "MODEL_POLICY_DENIED", ModelErrorCode.POLICY_DENIED
    return "MODEL_CAPABILITY_CONFIGURED", None


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
        if host in {"localhost", "host.docker.internal", "tei", "reranker"}:
            return True
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return allow_external
        return address.is_loopback or address.is_private or allow_external
    except ValueError:
        return False
