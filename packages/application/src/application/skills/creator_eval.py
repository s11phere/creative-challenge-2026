"""Deterministic draft Skill evaluation reusing the Phase 1 eval framework.

The creator gate (Phase 4) is structural: it executes the draft's declarative
workflow through the production ``DeterministicWorkflowExecutor`` with
deterministic fixture handlers (no-op / echo) and judges the results with
``StructuralSkillEvalJudge``. No model, no database, and no private bodies are
involved — a draft passes the gate when its package loads, its workflow runs
to a finalizing step, and its declared checks hold. The ``fixture`` probe
handlers map every workflow node handler name so an incomplete or arbitrary
draft workflow still yields a meaningful (deterministic) observation.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from time import perf_counter
from typing import cast
from uuid import UUID, uuid4, uuid5

from agent_runtime import (
    DeterministicWorkflowExecutor,
    InMemoryRuntimeStateStore,
    JSONValue,
    NodeExecutionContext,
    NodeHandler,
    NodeOutcome,
    NodeResult,
    PersonalSkillRegistry,
    PinnedSkill,
    RuntimeAuditEventType,
    RuntimeExecutionResult,
    SkillPackage,
    load_workflow,
)
from domain.agent_runtime import AgentRun, AgentRunContext, RunStep
from model_gateway import GatewayConfig, ModelGateway, create_model_gateway

from .evaluation import (
    SkillEvalCase,
    SkillEvalObservation,
    SkillEvalSkillReport,
    StructuralSkillEvalJudge,
    build_skill_report,
    invalid_case_result,
)

_EVAL_NAMESPACE = UUID("4a5d3c2b-1e0f-4a3d-9c2b-8f1e0d4a3c2b")
_EVAL_SPACE_ID = uuid5(_EVAL_NAMESPACE, "skill-creator-draft-evaluation")


class DraftSkillEvalRunner:
    """Run every eval case of one draft package through deterministic fixture handlers."""

    def __init__(
        self,
        *,
        registry: PersonalSkillRegistry,
        handler_overrides: Mapping[str, NodeHandler] | None = None,
    ) -> None:
        self._registry = registry
        self._handler_overrides = dict(handler_overrides or {})

    async def evaluate(self, name: str) -> SkillEvalSkillReport:
        package = self._registry.validate_draft(name)
        pin = self._registry.pin(package.manifest.name, package.manifest.version)
        output_schema = _load_json(package, package.manifest.output_schema)
        cases = _load_cases(package)
        results = []
        for index, case in enumerate(cases):
            if case is None:
                results.append(
                    invalid_case_result(f"case-invalid-{index + 1}", reason="case_invalid")
                )
                continue
            observation = await self._run_case(package, pin, case, output_schema)
            results.append(
                StructuralSkillEvalJudge().judge(case, observation, output_schema=output_schema)
            )
        return build_skill_report(package.manifest.name, package.manifest.version, results)

    async def _run_case(
        self,
        package: SkillPackage,
        pin: PinnedSkill,
        case: SkillEvalCase,
        output_schema: Mapping[str, object],
    ) -> SkillEvalObservation:
        started = perf_counter()
        try:
            observation = await self._execute(package, pin, case, output_schema, started)
        except Exception as exc:
            observation = SkillEvalObservation(
                case_id=case.case_id,
                status="error",
                output=None,
                tool_calls=(),
                latency_ms=(perf_counter() - started) * 1000,
                error=f"SKILL_EVAL_DRAFT_{type(exc).__name__}",
            )
        return observation

    async def _execute(
        self,
        package: SkillPackage,
        pin: PinnedSkill,
        case: SkillEvalCase,
        output_schema: Mapping[str, object],
        started: float,
    ) -> SkillEvalObservation:
        invocation = package.manifest.invocation
        if invocation is not None and invocation.execution_mode == "agent_loop":
            # Agent-loop drafts need model-driven tool calls; the creator gate is
            # deterministic and structural, so this draft is reported as unsupported.
            return _error_observation(case, "SKILL_EVAL_DRAFT_AGENT_LOOP", started)
        run = _build_run(package, pin, case)
        handlers = self._fixture_handlers(package, output_schema)
        executor = DeterministicWorkflowExecutor(
            skill_registry=self._registry,
            model_gateway=_fixture_gateway(),
            handlers=handlers,
            state_store=InMemoryRuntimeStateStore(),
        )
        result = await executor.execute(run, pin, _input_data(case, package))
        return _runtime_observation(result, case, started)

    def _fixture_handlers(
        self, package: SkillPackage, output_schema: Mapping[str, object]
    ) -> dict[str, NodeHandler]:
        workflow = load_workflow(package)
        handlers: dict[str, NodeHandler] = dict(self._handler_overrides)

        async def _echo(context: NodeExecutionContext) -> NodeResult:
            if context.run.current_step is RunStep.VERIFYING:
                return NodeResult(
                    outcome=NodeOutcome.COMPLETE,
                    output=_fixture_output(output_schema, context.input),
                )
            return NodeResult()

        for node in workflow.nodes:
            handlers.setdefault(node.handler, _echo)
        return handlers


_FIXTURE_GATEWAY: ModelGateway | None = None


def _fixture_gateway() -> ModelGateway:
    """Deterministic fake gateway; the fixture handlers never call the model."""
    global _FIXTURE_GATEWAY
    if _FIXTURE_GATEWAY is None:
        _FIXTURE_GATEWAY = create_model_gateway(GatewayConfig())
    return _FIXTURE_GATEWAY


def _build_run(package: SkillPackage, pin: PinnedSkill, case: SkillEvalCase) -> AgentRun:
    return AgentRun(
        context=AgentRunContext(
            run_id=uuid4(),
            space_id=_EVAL_SPACE_ID,
            skill_name=pin.name,
            skill_version=pin.version,
            skill_content_sha256=pin.content_sha256,
            trace_id=f"skill-creator-eval-{case.case_id}",
            caller_id="skill_creator",
            granted_permissions=package.manifest.permissions,
        ),
        budget=package.manifest.budgets,
    )


def _input_data(case: SkillEvalCase, package: SkillPackage) -> Mapping[str, JSONValue]:
    del package
    if case.input is not None:
        return cast(Mapping[str, JSONValue], case.input)
    return {"question": case.case_id}


def _runtime_observation(
    result: RuntimeExecutionResult, case: SkillEvalCase, started: float
) -> SkillEvalObservation:
    latency_ms = (perf_counter() - started) * 1000
    if result.error is not None:
        return SkillEvalObservation(
            case_id=case.case_id,
            status="error",
            output=None,
            tool_calls=(),
            latency_ms=latency_ms,
            error=result.error.code,
        )
    status = "refused" if result.refused else "complete"
    tool_calls = tuple(
        event.node_id
        for event in result.events
        if event.event_type is RuntimeAuditEventType.NODE_STARTED and event.node_id
    )
    return SkillEvalObservation(
        case_id=case.case_id,
        status=status,
        output=result.output,
        tool_calls=tool_calls,
        latency_ms=latency_ms,
    )


def _error_observation(case: SkillEvalCase, error: str, started: float) -> SkillEvalObservation:
    return SkillEvalObservation(
        case_id=case.case_id,
        status="error",
        output=None,
        tool_calls=(),
        latency_ms=(perf_counter() - started) * 1000,
        error=error,
    )


def _load_cases(package: SkillPackage) -> tuple[SkillEvalCase | None, ...]:
    """Parse every referenced eval case; invalid lines become ``None`` (case_invalid)."""
    parsed: list[SkillEvalCase | None] = []
    for reference in package.manifest.evals:
        path = package.root / reference
        if not path.is_file():
            parsed.append(None)
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            try:
                raw = json.loads(raw_line)
            except json.JSONDecodeError:
                parsed.append(None)
                continue
            if not isinstance(raw, dict):
                parsed.append(None)
                continue
            try:
                parsed.append(SkillEvalCase.from_mapping(raw))
            except ValueError:
                parsed.append(None)
    return tuple(parsed)


def _load_json(package: SkillPackage, relative: str) -> Mapping[str, object]:
    value = json.loads((package.root / relative).read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _fixture_output(
    schema: Mapping[str, object], input_data: Mapping[str, JSONValue]
) -> dict[str, JSONValue]:
    """Build a deterministic schema-conformant output from declared properties."""
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    output: dict[str, JSONValue] = {}
    for key, raw in properties.items():
        if not isinstance(raw, dict):
            continue
        output[key] = _fixture_value(raw, input_data)
    return output


def _fixture_value(prop: Mapping[str, object], input_data: Mapping[str, JSONValue]) -> JSONValue:
    if "const" in prop:
        return cast(JSONValue, prop["const"])
    enum = prop.get("enum")
    if isinstance(enum, list) and enum:
        return cast(JSONValue, enum[0])
    value_type = prop.get("type")
    if value_type == "string":
        return _echo_string(input_data)
    if value_type == "object":
        return cast(JSONValue, _fixture_output(prop, input_data))
    if value_type == "array":
        return []
    if value_type == "integer" or value_type == "number":
        return 0
    if value_type == "boolean":
        return True
    if value_type == "null":
        return None
    return _echo_string(input_data)


def _echo_string(input_data: Mapping[str, JSONValue]) -> str:
    for key in ("question", "text", "topic", "prompt", "content"):
        candidate = input_data.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate[:280]
    return "fixture-output"


__all__ = ["DraftSkillEvalRunner"]
