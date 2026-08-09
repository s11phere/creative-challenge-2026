"""Application-facing resolution of Conversation reasoning preferences."""

from __future__ import annotations

from domain.reasoning import ReasoningEffort, ReasoningMode, ReasoningProfile
from model_gateway import (
    ModelCapabilityRegistry,
    ModelGateway,
    default_model_capability_registry,
)


class ReasoningProfileResolver:
    """Resolve one user preference against the currently selected chat capability."""

    def __init__(
        self,
        *,
        gateway: ModelGateway,
        registry: ModelCapabilityRegistry | None = None,
    ) -> None:
        self._gateway = gateway
        self._registry = registry or default_model_capability_registry()

    def resolve(self, requested_effort: ReasoningEffort) -> ReasoningProfile:
        status = self._gateway.status
        if (
            requested_effort is ReasoningEffort.AUTO
            and status.reasoning_enabled_by_default is False
        ):
            # Preserve the existing ``fast_chat_reasoning_enabled=False`` behavior.
            return ReasoningProfile(
                requested_effort=requested_effort,
                effective_effort=ReasoningEffort.NONE,
                provider=status.provider.value,
                model=status.model_identity,
                mode=ReasoningMode.DISABLED,
            )
        return self._registry.map(
            provider=status.provider,
            model=status.model_identity,
            requested_effort=requested_effort,
        )


__all__ = ["ReasoningProfileResolver"]
