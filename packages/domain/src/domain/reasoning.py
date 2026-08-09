"""Provider-neutral reasoning effort contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ReasoningEffort(StrEnum):
    """User-facing effort choices shared by conversations and model adapters."""

    AUTO = "auto"
    NONE = "none"
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


class ReasoningMode(StrEnum):
    """How a selected Provider receives the resolved effort."""

    DISABLED = "disabled"
    NATIVE = "native"
    COARSE = "coarse"


class ReasoningDowngradeReason(StrEnum):
    """Stable, non-sensitive explanation for an automatic capability downgrade."""

    NONE = "none"
    PROVIDER_UNSUPPORTED = "provider_unsupported"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    POLICY_DENIED = "policy_denied"


@dataclass(frozen=True)
class ReasoningProfile:
    """Versioned audit value passed through the ModelGateway boundary."""

    requested_effort: ReasoningEffort
    effective_effort: ReasoningEffort
    provider: str = "unresolved"
    model: str = "unresolved"
    mapping_version: str = "reasoning-mapping-v1"
    mode: ReasoningMode = ReasoningMode.DISABLED
    downgrade_reason: ReasoningDowngradeReason = ReasoningDowngradeReason.NONE

    schema_version: str = "reasoning-profile-v1"

    def __post_init__(self) -> None:
        if self.effective_effort is ReasoningEffort.AUTO:
            raise ValueError("Reasoning effective effort cannot be auto")
        if not self.provider.strip() or not self.model.strip():
            raise ValueError("Reasoning provider and model are required")
        if self.mapping_version != "reasoning-mapping-v1":
            raise ValueError("Unsupported reasoning mapping version")
        if (
            self.mode is ReasoningMode.DISABLED
            and self.effective_effort is not ReasoningEffort.NONE
        ):
            raise ValueError("Disabled reasoning mode requires none effort")
        if self.downgrade_reason is not ReasoningDowngradeReason.NONE:
            if self.requested_effort is not ReasoningEffort.AUTO:
                raise ValueError("Only auto effort may be downgraded")
            if self.effective_effort is not ReasoningEffort.NONE:
                raise ValueError("Downgraded reasoning effort must be none")

    @classmethod
    def unresolved(
        cls, requested_effort: ReasoningEffort = ReasoningEffort.AUTO
    ) -> ReasoningProfile:
        """Compatibility value for callers that do not yet have a configured gateway."""

        return cls(
            requested_effort=requested_effort,
            effective_effort=ReasoningEffort.NONE,
            provider="unresolved",
            model="unresolved",
            mode=ReasoningMode.DISABLED,
            downgrade_reason=(
                ReasoningDowngradeReason.CAPABILITY_UNAVAILABLE
                if requested_effort is ReasoningEffort.AUTO
                else ReasoningDowngradeReason.NONE
            ),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "schema_version": self.schema_version,
            "requested_effort": self.requested_effort.value,
            "effective_effort": self.effective_effort.value,
            "provider": self.provider,
            "model": self.model,
            "mapping_version": self.mapping_version,
            "mode": self.mode.value,
            "downgrade_reason": self.downgrade_reason.value,
        }


def reasoning_profile_from_dict(value: object) -> ReasoningProfile:
    if not isinstance(value, dict):
        raise ValueError("Reasoning profile must be an object")
    return ReasoningProfile(
        requested_effort=ReasoningEffort(str(value.get("requested_effort", "auto"))),
        effective_effort=ReasoningEffort(str(value.get("effective_effort", "none"))),
        provider=str(value.get("provider", "unresolved")),
        model=str(value.get("model", "unresolved")),
        mapping_version=str(value.get("mapping_version", "reasoning-mapping-v1")),
        mode=ReasoningMode(str(value.get("mode", "disabled"))),
        downgrade_reason=ReasoningDowngradeReason(str(value.get("downgrade_reason", "none"))),
    )


__all__ = [
    "ReasoningDowngradeReason",
    "ReasoningEffort",
    "ReasoningMode",
    "ReasoningProfile",
    "reasoning_profile_from_dict",
]
