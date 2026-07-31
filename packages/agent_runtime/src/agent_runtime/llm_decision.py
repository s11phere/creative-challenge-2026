"""Strict, provider-neutral LLM decisions for bounded Agent workflows."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import cast

from domain.agent_runtime import BudgetUsage, RunErrorCategory
from jsonschema import Draft202012Validator
from model_gateway import CapabilityAlias, ChatMessage, ChatRequest, ChatRole

from .executor import NodeExecutionContext, NodeExecutionError, NodeResult
from .tools import JSONValue


class LLMDecisionAction(StrEnum):
    """Actions the model may request from a bounded workflow."""

    CALL_TOOL = "call_tool"
    COMPLETE = "complete"
    REFUSE = "refuse"


@dataclass(frozen=True)
class LLMDecision:
    """Validated model intent; it does not execute a Tool."""

    action: LLMDecisionAction
    tool_name: str | None = None
    arguments: dict[str, JSONValue] = field(default_factory=dict)
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.action is LLMDecisionAction.CALL_TOOL and not self.tool_name:
            raise ValueError("call_tool decisions require a tool_name")
        if self.action is not LLMDecisionAction.CALL_TOOL and self.tool_name is not None:
            raise ValueError("non-tool decisions cannot select a tool")
        if self.action is LLMDecisionAction.COMPLETE and self.reason is None:
            raise ValueError("complete decisions require a reason")
        if self.action is LLMDecisionAction.REFUSE and self.reason is None:
            raise ValueError("refuse decisions require a reason")

    def as_json(self) -> dict[str, JSONValue]:
        value: dict[str, JSONValue] = {"action": self.action.value}
        if self.tool_name is not None:
            value["tool_name"] = self.tool_name
            value["arguments"] = self.arguments
        if self.reason is not None:
            value["reason"] = self.reason
        return value


class LLMDecisionError(NodeExecutionError):
    """Raised when a model response cannot become a safe decision."""

    def __init__(self, message: str) -> None:
        super().__init__(
            code="RUN_LLM_DECISION_INVALID",
            category=RunErrorCategory.SCHEMA,
            message=message,
        )


_DECISION_SCHEMA: dict[str, JSONValue] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["action"],
    "properties": {
        "action": {"enum": [action.value for action in LLMDecisionAction]},
        "tool_name": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
        "arguments": {"type": "object"},
        "reason": {"type": "string", "minLength": 1, "maxLength": 2000},
    },
    "allOf": [
        {
            "if": {"properties": {"action": {"const": LLMDecisionAction.CALL_TOOL.value}}},
            "then": {"required": ["tool_name", "arguments"]},
        },
        {
            "if": {"properties": {"action": {"enum": ["complete", "refuse"]}}},
            "then": {
                "required": ["reason"],
                "not": {
                    "anyOf": [
                        {"required": ["tool_name"]},
                        {"required": ["arguments"]},
                    ]
                },
            },
        },
    ],
}
_DECISION_VALIDATOR = Draft202012Validator(_DECISION_SCHEMA)


def parse_llm_decision(text: str, *, allowed_tools: frozenset[str]) -> LLMDecision:
    """Parse one strict JSON decision and enforce the server-side Tool allowlist."""

    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMDecisionError("Model decision must be valid JSON.") from exc
    if not isinstance(value, dict):
        raise LLMDecisionError("Model decision must be a JSON object.")
    errors = sorted(_DECISION_VALIDATOR.iter_errors(value), key=lambda error: error.path)
    if errors:
        raise LLMDecisionError("Model decision does not match the required schema.")
    action = LLMDecisionAction(cast(str, value["action"]))
    tool_name = cast(str | None, value.get("tool_name"))
    if tool_name is not None and tool_name not in allowed_tools:
        raise LLMDecisionError("Model selected a Tool outside the server allowlist.")
    arguments = cast(dict[str, JSONValue], value.get("arguments", {}))
    return LLMDecision(
        action=action,
        tool_name=tool_name,
        arguments=arguments,
        reason=cast(str | None, value.get("reason")),
    )


@dataclass(frozen=True)
class LLMDecisionNode:
    """A bounded workflow node that asks the ModelGateway for one decision."""

    allowed_tools: frozenset[str]
    system_prompt: str
    max_tokens: int = 512

    def __post_init__(self) -> None:
        if not self.system_prompt.strip():
            raise ValueError("LLM decision system prompt must not be blank")
        if self.max_tokens < 1:
            raise ValueError("LLM decision max_tokens must be positive")

    async def __call__(self, context: NodeExecutionContext) -> NodeResult:
        user_input = json.dumps(
            {"input": context.input, "state": context.state},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        response = await context.model_gateway.chat(
            ChatRequest(
                messages=(
                    ChatMessage(role=ChatRole.SYSTEM, content=self.system_prompt),
                    ChatMessage(role=ChatRole.USER, content=user_input),
                ),
                temperature=0.0,
                max_tokens=self.max_tokens,
            ),
            capability=CapabilityAlias.FAST_CHAT,
        )
        decision = parse_llm_decision(response.text, allowed_tools=self.allowed_tools)
        return NodeResult(
            state_updates={"llm_decision": decision.as_json()},
            output=decision.as_json(),
            usage=BudgetUsage(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            ),
        )


__all__ = [
    "LLMDecision",
    "LLMDecisionAction",
    "LLMDecisionError",
    "LLMDecisionNode",
    "parse_llm_decision",
]
