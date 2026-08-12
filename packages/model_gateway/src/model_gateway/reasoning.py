"""Capability registry and provider-neutral effort mapping."""

from __future__ import annotations

from dataclasses import dataclass

from domain.reasoning import (
    ReasoningDowngradeReason,
    ReasoningEffort,
    ReasoningMode,
    ReasoningProfile,
)

from .contracts import ModelProvider

_MAPPING_VERSION = "reasoning-mapping-v1"
_EXPLICIT_EFFORTS = frozenset(
    {
        ReasoningEffort.MINIMAL,
        ReasoningEffort.LOW,
        ReasoningEffort.MEDIUM,
        ReasoningEffort.HIGH,
        ReasoningEffort.XHIGH,
        ReasoningEffort.MAX,
    }
)


class ReasoningMappingError(ValueError):
    """Explicit effort requests fail closed when a capability cannot honor them."""

    code = "MODEL_REASONING_UNSUPPORTED"


@dataclass(frozen=True)
class ModelCapabilities:
    provider: ModelProvider
    model_pattern: str
    supported_efforts: frozenset[ReasoningEffort]
    default_effort: ReasoningEffort
    reasoning_mode: ReasoningMode
    supports_continuation: bool
    supports_native_tool_use: bool = False
    supports_prompt_caching: bool = False
    effort_mappings: tuple[tuple[ReasoningEffort, ReasoningEffort], ...] = ()
    mapping_version: str = _MAPPING_VERSION

    def __post_init__(self) -> None:
        if self.default_effort is ReasoningEffort.AUTO:
            raise ValueError("Model capability default effort cannot be auto")
        if not self.model_pattern.strip():
            raise ValueError("Model capability model pattern is required")
        if self.reasoning_mode is ReasoningMode.DISABLED and self.supported_efforts != frozenset(
            {ReasoningEffort.NONE}
        ):
            raise ValueError("Disabled reasoning capabilities may only support none")
        requested_efforts = tuple(item[0] for item in self.effort_mappings)
        if len(requested_efforts) != len(set(requested_efforts)):
            raise ValueError("Reasoning effort mappings must not duplicate a requested effort")
        if any(
            requested not in self.supported_efforts or effective not in self.supported_efforts
            for requested, effective in self.effort_mappings
        ):
            raise ValueError("Reasoning effort mappings must use supported efforts")
        if self.mapping_version not in {"reasoning-mapping-v1", "reasoning-mapping-v2"}:
            raise ValueError("Unsupported reasoning mapping version")

    def effective_effort_for(self, requested_effort: ReasoningEffort) -> ReasoningEffort:
        return dict(self.effort_mappings).get(requested_effort, requested_effort)

    def matches(self, provider: ModelProvider, model: str) -> bool:
        return self.provider is provider and (
            self.model_pattern == "*" or self.model_pattern == model
        )


class ModelCapabilityRegistry:
    """Ordered provider/model capability registry with explicit wildcard fallback."""

    def __init__(self, capabilities: tuple[ModelCapabilities, ...]) -> None:
        if not capabilities:
            raise ValueError("Model capability registry cannot be empty")
        self._capabilities = capabilities

    def resolve(self, provider: ModelProvider, model: str) -> ModelCapabilities:
        exact = next(
            (
                capability
                for capability in self._capabilities
                if capability.provider is provider and capability.model_pattern == model
            ),
            None,
        )
        if exact is not None:
            return exact
        wildcard = next(
            (
                capability
                for capability in self._capabilities
                if capability.matches(provider, model) and capability.model_pattern == "*"
            ),
            None,
        )
        if wildcard is None:
            raise ReasoningMappingError(
                f"No reasoning capability is registered for {provider.value}/{model}."
            )
        return wildcard

    def map(
        self,
        *,
        provider: ModelProvider,
        model: str,
        requested_effort: ReasoningEffort,
    ) -> ReasoningProfile:
        capability = self.resolve(provider, model)
        if requested_effort is ReasoningEffort.NONE:
            return ReasoningProfile(
                requested_effort=requested_effort,
                effective_effort=ReasoningEffort.NONE,
                provider=provider.value,
                model=model,
                mapping_version=capability.mapping_version,
                mode=ReasoningMode.DISABLED,
            )

        if requested_effort is ReasoningEffort.AUTO:
            effective = capability.effective_effort_for(capability.default_effort)
            if capability.reasoning_mode is ReasoningMode.DISABLED:
                return ReasoningProfile(
                    requested_effort=requested_effort,
                    effective_effort=ReasoningEffort.NONE,
                    provider=provider.value,
                    model=model,
                    mapping_version=capability.mapping_version,
                    mode=ReasoningMode.DISABLED,
                    downgrade_reason=ReasoningDowngradeReason.PROVIDER_UNSUPPORTED,
                )
            if effective not in capability.supported_efforts:
                return ReasoningProfile(
                    requested_effort=requested_effort,
                    effective_effort=ReasoningEffort.NONE,
                    provider=provider.value,
                    model=model,
                    mapping_version=capability.mapping_version,
                    mode=ReasoningMode.DISABLED,
                    downgrade_reason=ReasoningDowngradeReason.CAPABILITY_UNAVAILABLE,
                )
            return ReasoningProfile(
                requested_effort=requested_effort,
                effective_effort=effective,
                provider=provider.value,
                model=model,
                mapping_version=capability.mapping_version,
                mode=capability.reasoning_mode,
            )

        if requested_effort not in _EXPLICIT_EFFORTS:
            raise ReasoningMappingError("Unsupported explicit reasoning effort.")
        if requested_effort not in capability.supported_efforts:
            raise ReasoningMappingError(
                f"Provider {provider.value} cannot honor explicit {requested_effort.value} effort."
            )
        effective = capability.effective_effort_for(requested_effort)
        return ReasoningProfile(
            requested_effort=requested_effort,
            effective_effort=effective,
            provider=provider.value,
            model=model,
            mapping_version=capability.mapping_version,
            mode=capability.reasoning_mode,
        )


def default_model_capability_registry() -> ModelCapabilityRegistry:
    """Safe defaults for fake/local development and the legacy chat adapter."""

    all_efforts = frozenset({ReasoningEffort.NONE, *_EXPLICIT_EFFORTS})
    return ModelCapabilityRegistry(
        (
            ModelCapabilities(
                provider=ModelProvider.FAKE,
                model_pattern="*",
                supported_efforts=all_efforts,
                default_effort=ReasoningEffort.LOW,
                reasoning_mode=ReasoningMode.NATIVE,
                supports_continuation=True,
                supports_native_tool_use=True,
            ),
            ModelCapabilities(
                provider=ModelProvider.OPENAI_COMPATIBLE,
                model_pattern="deepseek-v4-flash",
                supported_efforts=all_efforts,
                default_effort=ReasoningEffort.MEDIUM,
                reasoning_mode=ReasoningMode.NATIVE,
                supports_continuation=False,
                supports_native_tool_use=True,
                effort_mappings=(
                    (ReasoningEffort.MINIMAL, ReasoningEffort.LOW),
                    (ReasoningEffort.XHIGH, ReasoningEffort.HIGH),
                ),
                mapping_version="reasoning-mapping-v2",
            ),
            ModelCapabilities(
                provider=ModelProvider.OPENAI_COMPATIBLE,
                model_pattern="*",
                supported_efforts=all_efforts,
                default_effort=ReasoningEffort.MEDIUM,
                reasoning_mode=ReasoningMode.COARSE,
                supports_continuation=False,
                supports_native_tool_use=True,
            ),
            ModelCapabilities(
                provider=ModelProvider.TEXT_EMBEDDINGS_INFERENCE,
                model_pattern="*",
                supported_efforts=frozenset({ReasoningEffort.NONE}),
                default_effort=ReasoningEffort.NONE,
                reasoning_mode=ReasoningMode.DISABLED,
                supports_continuation=False,
            ),
            ModelCapabilities(
                provider=ModelProvider.DISABLED,
                model_pattern="*",
                supported_efforts=frozenset({ReasoningEffort.NONE}),
                default_effort=ReasoningEffort.NONE,
                reasoning_mode=ReasoningMode.DISABLED,
                supports_continuation=False,
            ),
        )
    )


__all__ = [
    "ModelCapabilities",
    "ModelCapabilityRegistry",
    "ReasoningMappingError",
    "default_model_capability_registry",
]
