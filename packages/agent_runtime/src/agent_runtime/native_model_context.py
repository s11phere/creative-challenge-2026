"""Bounded, body-free model context projections for native Tool-use v2.

The dataclasses mirror ``agent-model-context-v2.schema.json``.  They never
contain prompt text, answers, source bodies, raw Tool arguments, or complete
Tool output.  Projection is deterministic so a recovered checkpoint renders
the same context in the same order.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from jsonschema import Draft202012Validator

from .tools import JSONValue

MODEL_CONTEXT_SCHEMA_VERSION = "agent-model-context-v2"

_MAX_OBSERVATION_SUMMARY_BYTES = 1_200
_MAX_DECISION_SUMMARY_BYTES = 320
_MAX_PROGRESS_SUMMARY_BYTES = 16_000
_MAX_OBSERVATIONS = 32
_MAX_DECISIONS = 48
_DEFAULT_STATUS = "succeeded"
_FORBIDDEN_OBSERVATION_NAMES = frozenset(
    {"prompt", "answer", "secret", "credential", "raw", "stdout", "stderr", "document_body"}
)


@dataclass(frozen=True)
class NativeDecisionHistoryItem:
    iteration: int
    kind: str
    summary: str
    tool_name: str | None = None
    status: str = "succeeded"
    error_code: str | None = None
    unresolved_item: str | None = None

    def __post_init__(self) -> None:
        if self.iteration < 1 or self.kind not in {"tool", "terminal"}:
            raise ValueError("Native decision history identity is invalid")
        if self.kind == "tool" and not self.tool_name:
            raise ValueError("Native Tool decisions require a Tool name")
        if self.kind == "terminal" and self.tool_name is not None:
            raise ValueError("Native terminal decisions cannot name a Tool")
        if not self.summary.strip() or len(self.summary.encode("utf-8")) > (
            _MAX_DECISION_SUMMARY_BYTES
        ):
            raise ValueError("Native decision summary is invalid or too large")
        if self.error_code is not None and len(self.error_code) > 120:
            raise ValueError("Native decision error code is too large")
        if self.unresolved_item is not None and len(self.unresolved_item.encode("utf-8")) > (
            _MAX_DECISION_SUMMARY_BYTES
        ):
            raise ValueError("Native decision unresolved item is too large")

    def as_dict(self) -> dict[str, JSONValue]:
        value: dict[str, JSONValue] = {
            "iteration": self.iteration,
            "kind": self.kind,
            "status": self.status,
            "summary": self.summary,
        }
        if self.tool_name is not None:
            value["tool_name"] = self.tool_name
        if self.error_code is not None:
            value["error_code"] = self.error_code
        if self.unresolved_item is not None:
            value["unresolved_item"] = self.unresolved_item
        return value


@dataclass(frozen=True)
class NativeModelObservation:
    iteration: int
    tool_name: str
    summary: str
    status: str = _DEFAULT_STATUS
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.iteration < 1 or not self.tool_name:
            raise ValueError("Native model observation identity is invalid")
        if self.status not in {"succeeded", "failed", "skipped", "needs_input"}:
            raise ValueError("Native model observation status is invalid")
        if not self.summary.strip() or len(self.summary.encode("utf-8")) > (
            _MAX_OBSERVATION_SUMMARY_BYTES
        ):
            raise ValueError("Native model observation summary is invalid or too large")
        if self.error_code is not None and len(self.error_code) > 120:
            raise ValueError("Native model observation error code is too large")

    def as_dict(self) -> dict[str, JSONValue]:
        value: dict[str, JSONValue] = {
            "iteration": self.iteration,
            "tool_name": self.tool_name,
            "status": self.status,
            "summary": self.summary,
        }
        if self.error_code is not None:
            value["error_code"] = self.error_code
        return value


@dataclass(frozen=True)
class NativeModelContextV2:
    goal: str
    selected_skills: tuple[dict[str, JSONValue], ...]
    decision_history: tuple[NativeDecisionHistoryItem, ...]
    observations: tuple[NativeModelObservation, ...]
    progress_summary: str
    schema_version: str = MODEL_CONTEXT_SCHEMA_VERSION
    approval_pending: bool = False
    cancellation_requested: bool = False

    def __post_init__(self) -> None:
        if self.schema_version != MODEL_CONTEXT_SCHEMA_VERSION:
            raise ValueError("Unsupported native model context schema version")
        if not self.goal.strip() or len(self.goal.encode("utf-8")) > 12_000:
            raise ValueError("Native model context goal is invalid or too large")
        if len(self.selected_skills) > 2:
            raise ValueError("Native model context has too many selected Skills")
        if len(self.decision_history) > _MAX_DECISIONS:
            raise ValueError("Native decision history is too large")
        if len(self.observations) > _MAX_OBSERVATIONS:
            raise ValueError("Native model observations are too large")
        if len(self.progress_summary.encode("utf-8")) > _MAX_PROGRESS_SUMMARY_BYTES:
            raise ValueError("Native progress summary is too large")
        for item in self.selected_skills:
            _validate_selected_skill(item)

    def as_dict(self) -> dict[str, JSONValue]:
        return {
            "schema_version": self.schema_version,
            "goal": self.goal,
            "selected_skills": list(self.selected_skills),
            "decision_history": [item.as_dict() for item in self.decision_history],
            "observations": [item.as_dict() for item in self.observations],
            "progress_summary": self.progress_summary,
            "approval_pending": self.approval_pending,
            "cancellation_requested": self.cancellation_requested,
        }

    def render_model_context(self) -> str:
        return json.dumps(
            self.as_dict(),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def digest(self) -> str:
        return f"sha256:{hashlib.sha256(self.render_model_context().encode('utf-8')).hexdigest()}"


def project_model_observation(
    observation: Mapping[str, JSONValue],
    schema: Mapping[str, JSONValue] | None,
    *,
    summary: str,
    status: str = _DEFAULT_STATUS,
) -> dict[str, JSONValue]:
    """Project one Tool result through its declared model-visible schema.

    Tools without a projection schema receive only status plus a bounded
    summary.  Unknown fields, forbidden names, and oversized values never
    become model context.
    """
    if not summary.strip() or len(summary.encode("utf-8")) > _MAX_OBSERVATION_SUMMARY_BYTES:
        raise ValueError("Native Tool projection summary is invalid or too large")
    if status not in {"succeeded", "failed", "skipped", "needs_input"}:
        raise ValueError("Native Tool projection status is invalid")
    if schema is None:
        return {"status": status, "summary": summary}
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("Native Tool model observation schema must declare properties")
    candidate: dict[str, JSONValue] = {}
    for name, property_schema in properties.items():
        if not isinstance(name, str) or not isinstance(property_schema, dict):
            raise ValueError("Native Tool model observation schema is invalid")
        if name in _FORBIDDEN_OBSERVATION_NAMES or not _safe_property_name(name):
            raise ValueError("Native Tool model observation schema exposes a forbidden field")
        if name not in observation:
            continue
        value = _clamp_projection_value(
            observation[name],
            property_schema,
            name=name,
        )
        if value is not _UNSET:
            candidate[name] = value
    if "status" in properties and "status" not in candidate:
        candidate["status"] = status
    if "summary" in properties and "summary" not in candidate:
        candidate["summary"] = _clamp_projection_value(
            summary,
            cast(Mapping[str, JSONValue], properties["summary"]),
            name="summary",
        )
    validator = Draft202012Validator(dict(schema))
    candidate = _fit_projection(candidate, validator)
    return candidate


_UNSET = object()


def _clamp_projection_value(
    value: JSONValue,
    schema: Mapping[str, JSONValue],
    *,
    name: str,
) -> JSONValue:
    del name
    if isinstance(value, str):
        max_length = _positive_int(schema.get("maxLength"), default=320)
        return value[:max_length]
    if isinstance(value, list):
        max_items = _positive_int(schema.get("maxItems"), default=8)
        item_schema = schema.get("items")
        item_mapping = item_schema if isinstance(item_schema, dict) else {}
        projected_items: list[JSONValue] = []
        for item in value[:max_items]:
            projected_items.append(_clamp_projection_value(item, item_mapping, name="item"))
        return projected_items
    if isinstance(value, dict):
        max_properties = _positive_int(schema.get("maxProperties"), default=16)
        properties = schema.get("properties")
        if isinstance(properties, dict):
            projected_object: dict[str, JSONValue] = {}
            for key, item in value.items():
                if key not in properties or len(projected_object) >= max_properties:
                    continue
                projected_object[key] = _clamp_projection_value(
                    item,
                    cast(Mapping[str, JSONValue], properties[key]),
                    name=key,
                )
            return projected_object
        return dict(list(value.items())[:max_properties])
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return _UNSET


def _fit_projection(
    value: dict[str, JSONValue],
    validator: Draft202012Validator,
) -> dict[str, JSONValue]:
    candidate = value
    for string_limit in (240, 128, 64):
        candidate = _shorten_projection(candidate, string_limit)
        serialized = json.dumps(
            candidate,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        if len(serialized.encode("utf-8")) <= _MAX_OBSERVATION_SUMMARY_BYTES and validator.is_valid(
            candidate
        ):
            return candidate
    raise ValueError("Native Tool projection exceeds the model context budget")


def _shorten_projection(value: dict[str, JSONValue], limit: int) -> dict[str, JSONValue]:
    shortened: dict[str, JSONValue] = {}
    for key, item in value.items():
        if isinstance(item, str):
            shortened[key] = item[:limit]
        elif isinstance(item, list):
            shortened[key] = [_shorten(item_value, limit) for item_value in item]
        elif isinstance(item, dict):
            shortened[key] = {
                item_key: _shorten(item_value, limit) for item_key, item_value in item.items()
            }
        else:
            shortened[key] = item
    return shortened


def _shorten(value: JSONValue, limit: int) -> JSONValue:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, list):
        return [_shorten(item, limit) for item in value]
    if isinstance(value, dict):
        return {key: _shorten(item, limit) for key, item in value.items()}
    return value


def _positive_int(value: JSONValue, *, default: int) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return default


def _safe_property_name(value: str) -> bool:
    lowered = value.lower()
    return not any(marker in lowered for marker in ("secret", "credential", "prompt", "answer"))


def _validate_selected_skill(value: Mapping[str, JSONValue]) -> None:
    required = {"name", "version", "content_sha256"}
    if set(value) != required or not all(isinstance(value.get(key), str) for key in required):
        raise ValueError("Native selected Skill projection is invalid")


__all__ = [
    "MODEL_CONTEXT_SCHEMA_VERSION",
    "NativeDecisionHistoryItem",
    "NativeModelContextV2",
    "NativeModelObservation",
    "project_model_observation",
]
