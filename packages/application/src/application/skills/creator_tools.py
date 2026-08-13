"""Model-visible Skill Creator Tools (Phase 4, Path A).

The creator is itself agent-driven: a scaffolded Skill package is built from the
shared template, written as a draft, validated, evaluated through the Phase 1
gate, and finally promoted to an active personal Skill on user approval. Every
state-changing Tool (scaffold / write / draft / activate) declares
``WRITE_KNOWLEDGE`` so it is gated by the existing durable-approval mechanism;
the read-only ones (validate / run_eval) declare ``READ_KNOWLEDGE``. Handlers
return structured, body-free payloads so the agent can iterate without seeing
private prompt or response bodies.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

import yaml
from agent_runtime import (
    InMemoryToolRegistry,
    JSONValue,
    ToolDefinition,
    ToolExecutionContext,
    ToolHandler,
)
from domain.agent_runtime import ToolPermission

from .drafts import SkillDraftError, SkillDraftStore, SkillDraftView
from .evaluation import SkillEvalSkillReport
from .personal import PersonalSkillView

_TOOL_VERSION = "1.0.0"

SCAFFOLD_WORKFLOW = """workflow_version: "1"
start: plan
nodes:
  - id: plan
    step: planning
    handler: grounded_qa_plan
    next: retrieve
    max_retries: 0
    required_permissions: []
    reserve: {tool_calls: 0, input_tokens: 0, output_tokens: 0}
  - id: retrieve
    step: retrieving
    handler: grounded_qa_plan
    next: delegate
    max_retries: 0
    required_permissions: []
    reserve: {tool_calls: 0, input_tokens: 0, output_tokens: 0}
  - id: delegate
    step: executing
    handler: grounded_qa_delegate
    next: verify
    max_retries: 0
    required_permissions: [read_knowledge]
    reserve: {tool_calls: 0, input_tokens: 8192, output_tokens: 4096}
  - id: verify
    step: verifying
    handler: grounded_qa_verify
    next: null
    max_retries: 0
    required_permissions: []
    reserve: {tool_calls: 0, input_tokens: 0, output_tokens: 0}
"""

SCAFFOLD_PROMPT = (
    "Return only the structure declared by the output schema. When the workflow "
    "delegates to grounded retrieval, ground every statement in the cited sources "
    "that the delegate returns.\n"
)

SCAFFOLD_CASES = (
    '{"case_id":"scaffold-001","input":{"question":"fixture question"},'
    '"expected":"complete","checks":['
    '{"type":"output_has_key","key":"status"},'
    '{"type":"output_has_key","key":"result"},'
    '{"type":"finalized"}]}\n'
)

_DEFAULT_INPUT_SCHEMA: dict[str, JSONValue] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"question": {"type": "string", "minLength": 1}},
    "required": ["question"],
}

_DEFAULT_OUTPUT_SCHEMA: dict[str, JSONValue] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"status": {"type": "string"}, "result": {"type": "string"}},
    "required": ["status", "result"],
}


def scaffold_skill_files(
    *,
    name: str,
    description: str,
    input_schema: Mapping[str, object] | None = None,
    output_schema: Mapping[str, object] | None = None,
) -> dict[str, str]:
    """Generate a complete, validated-by-construction personal Skill package.

    The package composes the existing Grounded QA handlers
    (``grounded_qa_plan`` / ``grounded_qa_delegate`` / ``grounded_qa_verify``),
    matching the ADR-018 rule that personal Skills only compose registered
    handlers. The scaffolded eval cases carry structural checks the
    deterministic gate can pass.
    """
    merged_input = _merge_input_schema(input_schema)
    merged_output = _merge_output_schema(output_schema)
    manifest = _scaffold_manifest(
        name=name,
        description=description,
        command=_command_slug(name),
    )
    return {
        "skill.yaml": yaml.safe_dump(manifest, sort_keys=False),
        "workflow.yaml": SCAFFOLD_WORKFLOW,
        "schemas/input.json": json.dumps(merged_input),
        "schemas/output.json": json.dumps(merged_output),
        "prompts/system.md": SCAFFOLD_PROMPT,
        "evals/cases.jsonl": SCAFFOLD_CASES,
    }


def _scaffold_manifest(*, name: str, description: str, command: str) -> dict[str, object]:
    return {
        "manifest_version": "2",
        "name": name,
        "version": "1.0.0",
        "description": description,
        "input_schema": "schemas/input.json",
        "output_schema": "schemas/output.json",
        "required_tools": [],
        "required_capabilities": [],
        "permissions": ["read_knowledge", "model"],
        "budgets": {
            "max_steps": 8,
            "max_tool_calls": 4,
            "max_input_tokens": 8192,
            "max_output_tokens": 4096,
            "timeout_seconds": 60,
        },
        "entrypoint": "workflow.yaml",
        "compatibility": {"runtime": ">=0.1.0,<1.0.0", "checkpoint_schema_versions": [1]},
        "prompts": ["prompts/system.md"],
        "evals": ["evals/cases.jsonl"],
        "invocation": {
            "command": command,
            "aliases": [],
            "argument_hint": "<问题或任务描述>",
            "trigger": {
                "summary": description,
                "when": [f"用户要求：{description}"],
                "avoid_when": [],
                "examples": [],
            },
            "input_mode": "question",
            "execution_mode": "projected",
        },
    }


def _merge_input_schema(value: Mapping[str, object] | None) -> dict[str, JSONValue]:
    if value is None or not isinstance(value.get("type"), str):
        return dict(_DEFAULT_INPUT_SCHEMA)
    merged = dict(value)
    properties = merged.get("properties")
    if not isinstance(properties, dict):
        properties = {}
    if "question" not in properties:
        properties = dict(properties)
        properties["question"] = {"type": "string", "minLength": 1}
        merged["properties"] = properties
        required = merged.get("required")
        if isinstance(required, list) and "question" not in required:
            merged["required"] = [*required, "question"]
    return cast(dict[str, JSONValue], merged)


def _merge_output_schema(value: Mapping[str, object] | None) -> dict[str, JSONValue]:
    if value is None or not isinstance(value.get("type"), str):
        return dict(_DEFAULT_OUTPUT_SCHEMA)
    return cast(dict[str, JSONValue], dict(value))


def _command_slug(name: str) -> str:
    return name.replace("_", "-")[:32]


class SkillCreatorTools:
    """Register and execute the Skill Creator Tools over one draft store."""

    def __init__(self, *, draft_store: SkillDraftStore) -> None:
        self._drafts = draft_store

    def handlers(self) -> dict[str, ToolHandler]:
        return {
            "skill_scaffold": self.skill_scaffold,
            "skill_write": self.skill_write,
            "skill_validate": self.skill_validate,
            "skill_run_eval": self.skill_run_eval,
            "skill_activate": self.skill_activate,
            "skill_draft": self.skill_draft,
        }

    async def skill_scaffold(
        self, arguments: dict[str, JSONValue], context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        del context
        name = _required_string(arguments, "name")
        description = _required_string(arguments, "description")
        raw_input = arguments.get("input_schema")
        raw_output = arguments.get("output_schema")
        files = scaffold_skill_files(
            name=name,
            description=description,
            input_schema=raw_input if isinstance(raw_input, dict) else None,
            output_schema=raw_output if isinstance(raw_output, dict) else None,
        )
        try:
            view = self._drafts.create(name, files)
        except SkillDraftError as exc:
            return _draft_error_payload(name, exc)
        return _draft_view_payload(view)

    async def skill_draft(
        self, arguments: dict[str, JSONValue], context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        del context
        name = _required_string(arguments, "name")
        files = _required_files(arguments)
        try:
            view = self._drafts.create(name, files)
        except SkillDraftError as exc:
            return _draft_error_payload(name, exc)
        return _draft_view_payload(view)

    async def skill_write(
        self, arguments: dict[str, JSONValue], context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        del context
        name = _required_string(arguments, "name")
        files = _required_files(arguments)
        try:
            view = self._drafts.update(name, files)
        except SkillDraftError as exc:
            return _draft_error_payload(name, exc)
        return _draft_view_payload(view)

    async def skill_validate(
        self, arguments: dict[str, JSONValue], context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        del context
        name = _required_string(arguments, "name")
        try:
            result = self._drafts.validate(name)
        except SkillDraftError as exc:
            return _validate_error_payload(name, exc)
        return {
            "trust": "untrusted",
            "ok": True,
            "name": name,
            "valid": result.valid,
            "error": result.error,
            "description": result.description,
            "version": result.version,
            "content_sha256": result.content_sha256,
        }

    async def skill_run_eval(
        self, arguments: dict[str, JSONValue], context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        del context
        name = _required_string(arguments, "name")
        try:
            report = await self._drafts.run_eval(name)
        except SkillDraftError as exc:
            return _eval_error_payload(name, exc)
        return _eval_payload(name, report)

    async def skill_activate(
        self, arguments: dict[str, JSONValue], context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        del context
        name = _required_string(arguments, "name")
        try:
            view = await self._drafts.activate(name)
        except SkillDraftError as exc:
            return _activate_error_payload(name, exc)
        return _personal_view_payload(view)


def register_skill_creator_tools(
    registry: InMemoryToolRegistry,
) -> tuple[ToolDefinition, ...]:
    """Register all Skill Creator Tools against a registry that already holds their handlers.

    ``SkillCreatorTools.handlers()`` must have been merged into ``registry``
    beforehand; ``register`` then validates each definition's handler against
    the registry.
    """
    return (
        registry.register(_skill_scaffold_definition()),
        registry.register(_skill_write_definition()),
        registry.register(_skill_validate_definition()),
        registry.register(_skill_run_eval_definition()),
        registry.register(_skill_activate_definition()),
        registry.register(_skill_draft_definition()),
    )


def _skill_scaffold_definition() -> ToolDefinition:
    return ToolDefinition(
        name="skill_scaffold",
        version=_TOOL_VERSION,
        description=(
            "Scaffold a new personal Skill draft from the shared template, "
            "composing the grounded retrieval handlers."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "description"],
            "properties": {
                "name": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                "description": {"type": "string", "minLength": 1, "maxLength": 280},
                "input_schema": {"type": ["object", "null"]},
                "output_schema": {"type": ["object", "null"]},
            },
        },
        output_schema=_DRAFT_VIEW_OUTPUT_SCHEMA,
        permissions=frozenset({ToolPermission.WRITE_KNOWLEDGE}),
        handler_name="skill_scaffold",
        timeout_seconds=60.0,
        max_retries=0,
        idempotent=True,
        audit_event="skill_scaffold",
        model_visible=True,
    )


def _skill_write_definition() -> ToolDefinition:
    return ToolDefinition(
        name="skill_write",
        version=_TOOL_VERSION,
        description="Write or overwrite files inside an existing Skill draft.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "files"],
            "properties": {
                "name": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                "files": {
                    "type": "object",
                    "maxProperties": 32,
                    "additionalProperties": {"type": "string", "maxLength": 262_144},
                },
            },
        },
        output_schema=_DRAFT_VIEW_OUTPUT_SCHEMA,
        permissions=frozenset({ToolPermission.WRITE_KNOWLEDGE}),
        handler_name="skill_write",
        timeout_seconds=60.0,
        max_retries=0,
        idempotent=True,
        audit_event="skill_write",
        model_visible=True,
    )


def _skill_draft_definition() -> ToolDefinition:
    return ToolDefinition(
        name="skill_draft",
        version=_TOOL_VERSION,
        description=(
            "Register a complete set of package files as a new Skill draft "
            "without running the full validation suite."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "files"],
            "properties": {
                "name": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                "files": {
                    "type": "object",
                    "maxProperties": 32,
                    "additionalProperties": {"type": "string", "maxLength": 262_144},
                },
            },
        },
        output_schema=_DRAFT_VIEW_OUTPUT_SCHEMA,
        permissions=frozenset({ToolPermission.WRITE_KNOWLEDGE}),
        handler_name="skill_draft",
        timeout_seconds=60.0,
        max_retries=0,
        idempotent=True,
        audit_event="skill_draft",
        model_visible=True,
    )


def _skill_validate_definition() -> ToolDefinition:
    return ToolDefinition(
        name="skill_validate",
        version=_TOOL_VERSION,
        description="Validate one Skill draft through the full trusted package suite.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["name"],
            "properties": {"name": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"}},
        },
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["trust", "ok", "name"],
            "properties": {
                "trust": {"const": "untrusted"},
                "ok": {"type": "boolean"},
                "name": {"type": "string"},
                "valid": {"type": "boolean"},
                "error": {"type": ["string", "null"]},
                "description": {"type": ["string", "null"]},
                "version": {"type": ["string", "null"]},
                "content_sha256": {"type": ["string", "null"]},
            },
        },
        permissions=frozenset({ToolPermission.READ_KNOWLEDGE}),
        handler_name="skill_validate",
        timeout_seconds=60.0,
        max_retries=0,
        idempotent=True,
        audit_event="skill_validate",
        model_visible=True,
    )


def _skill_run_eval_definition() -> ToolDefinition:
    return ToolDefinition(
        name="skill_run_eval",
        version=_TOOL_VERSION,
        description=(
            "Run the deterministic Skill eval gate over one draft and return "
            "body-free pass/fail metrics."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["name"],
            "properties": {"name": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"}},
        },
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["trust", "ok", "name"],
            "properties": {
                "trust": {"const": "untrusted"},
                "ok": {"type": "boolean"},
                "name": {"type": "string"},
                "skill_name": {"type": "string"},
                "skill_version": {"type": "string"},
                "total": {"type": "integer"},
                "passed": {"type": "integer"},
                "failed": {"type": "integer"},
                "inconclusive": {"type": "integer"},
                "errored": {"type": "integer"},
                "pass_rate": {"type": ["number", "null"]},
                "check_pass_rate": {"type": ["number", "null"]},
                "gate_passed": {"type": "boolean"},
                "failure_categories": {"type": "object"},
                "error": {"type": ["string", "null"]},
            },
        },
        permissions=frozenset({ToolPermission.READ_KNOWLEDGE}),
        handler_name="skill_run_eval",
        timeout_seconds=60.0,
        max_retries=0,
        idempotent=True,
        audit_event="skill_run_eval",
        model_visible=True,
    )


def _skill_activate_definition() -> ToolDefinition:
    return ToolDefinition(
        name="skill_activate",
        version=_TOOL_VERSION,
        description=(
            "Promote a validated, eval-passed Skill draft to an active personal "
            "Skill. Durable user approval is required."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["name"],
            "properties": {"name": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"}},
        },
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["trust", "ok", "name"],
            "properties": {
                "trust": {"const": "untrusted"},
                "ok": {"type": "boolean"},
                "name": {"type": "string"},
                "version": {"type": "string"},
                "description": {"type": "string"},
                "active": {"type": "boolean"},
                "content_sha256": {"type": "string"},
                "error": {"type": ["string", "null"]},
            },
        },
        permissions=frozenset({ToolPermission.WRITE_KNOWLEDGE}),
        handler_name="skill_activate",
        timeout_seconds=60.0,
        max_retries=0,
        idempotent=True,
        audit_event="skill_activate",
        model_visible=True,
    )


_DRAFT_VIEW_OUTPUT_SCHEMA: dict[str, JSONValue] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["trust", "ok", "name"],
    "properties": {
        "trust": {"const": "untrusted"},
        "ok": {"type": "boolean"},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "complete": {"type": "boolean"},
        "valid": {"type": "boolean"},
        "file_count": {"type": "integer"},
        "files": {"type": "array", "items": {"type": "string"}},
        "error": {"type": ["string", "null"]},
    },
}


def _draft_view_payload(view: SkillDraftView) -> dict[str, JSONValue]:
    return {
        "trust": "untrusted",
        "ok": True,
        "name": view.name,
        "description": view.description,
        "complete": view.complete,
        "valid": view.valid,
        "file_count": view.file_count,
        "files": list(view.files),
        "error": None,
    }


def _eval_payload(name: str, report: SkillEvalSkillReport) -> dict[str, JSONValue]:
    metrics = report.metrics
    gate_passed = metrics.total > 0 and metrics.passed == metrics.total and metrics.errored == 0
    return {
        "trust": "untrusted",
        "ok": True,
        "name": name,
        "skill_name": report.skill_name,
        "skill_version": report.skill_version,
        "total": metrics.total,
        "passed": metrics.passed,
        "failed": metrics.failed,
        "inconclusive": metrics.inconclusive,
        "errored": metrics.errored,
        "pass_rate": metrics.pass_rate,
        "check_pass_rate": metrics.check_pass_rate,
        "gate_passed": gate_passed,
        "failure_categories": dict(metrics.failure_category_counts),
        "error": None,
    }


def _personal_view_payload(view: PersonalSkillView) -> dict[str, JSONValue]:
    return {
        "trust": "untrusted",
        "ok": True,
        "name": view.name,
        "version": view.version,
        "description": view.description,
        "active": view.active,
        "content_sha256": view.content_sha256,
        "error": None,
    }


def _draft_error_payload(name: str, exc: SkillDraftError) -> dict[str, JSONValue]:
    return {
        "trust": "untrusted",
        "ok": False,
        "name": name,
        "description": "",
        "complete": False,
        "valid": False,
        "file_count": 0,
        "files": [],
        "error": f"{exc.code.value}: {exc}",
    }


def _validate_error_payload(name: str, exc: SkillDraftError) -> dict[str, JSONValue]:
    return {
        "trust": "untrusted",
        "ok": False,
        "name": name,
        "valid": False,
        "error": f"{exc.code.value}: {exc}",
        "description": None,
        "version": None,
        "content_sha256": None,
    }


def _eval_error_payload(name: str, exc: SkillDraftError) -> dict[str, JSONValue]:
    return {
        "trust": "untrusted",
        "ok": False,
        "name": name,
        "skill_name": name,
        "skill_version": "",
        "total": 0,
        "passed": 0,
        "failed": 0,
        "inconclusive": 0,
        "errored": 0,
        "pass_rate": None,
        "check_pass_rate": None,
        "gate_passed": False,
        "failure_categories": {},
        "error": f"{exc.code.value}: {exc}",
    }


def _activate_error_payload(name: str, exc: SkillDraftError) -> dict[str, JSONValue]:
    return {
        "trust": "untrusted",
        "ok": False,
        "name": name,
        "version": "",
        "description": "",
        "active": False,
        "content_sha256": "",
        "error": f"{exc.code.value}: {exc}",
    }


def _required_string(arguments: Mapping[str, JSONValue], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Skill Creator Tool argument {name!r} is required.")
    return value.strip()


def _required_files(arguments: Mapping[str, JSONValue]) -> dict[str, str]:
    raw = arguments.get("files")
    if not isinstance(raw, dict) or not raw:
        raise ValueError("Skill Creator Tool requires a non-empty files mapping.")
    files: dict[str, str] = {}
    for path, content in raw.items():
        if not isinstance(path, str) or not isinstance(content, str):
            raise ValueError("Skill Creator Tool files must map path to string content.")
        files[path] = content
    return files


__all__ = [
    "SkillCreatorTools",
    "register_skill_creator_tools",
    "scaffold_skill_files",
]
