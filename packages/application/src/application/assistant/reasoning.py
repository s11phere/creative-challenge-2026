"""Application-facing resolution of Conversation reasoning preferences."""

from __future__ import annotations

from domain.reasoning import ReasoningEffort, ReasoningProfile
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
        return self._registry.map(
            provider=status.provider,
            model=status.model_identity,
            requested_effort=requested_effort,
        )


__all__ = ["ReasoningProfileResolver"]
