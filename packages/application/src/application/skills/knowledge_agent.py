"""LLM-directed read-only Agent adapter over the existing Grounded QA Port."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast
from uuid import UUID

from agent_runtime import (
    AgentToolRegistry,
    BoundedLLMAgentNode,
    InMemoryToolRegistry,
    JSONValue,
    LLMDecisionVerifyNode,
    NodeExecutionContext,
    NodeExecutionError,
    NodeHandler,
    NodeResult,
    ToolDefinition,
    ToolExecutionContext,
    ToolRef,
)
from domain.agent_runtime import RunErrorCategory, ToolPermission
from domain.grounded_qa import QAStatus
from domain.qa_persistence import QARunRecord, QARunVersions

from application.qa.service import (
    AgentRetrievalPlan,
    GroundedQAApplicationPort,
    GroundedQAExecutionProfile,
)

from .knowledge_qa import qa_failure


@dataclass(frozen=True)
class KnowledgeAgentSkillConfig:
    profile: GroundedQAExecutionProfile
    versions: QARunVersions
    system_prompt: str


class KnowledgeAgentSkillAdapter:
    """Run a bounded LLM decision loop while QA remains the answer authority."""

    def __init__(self, *, qa: GroundedQAApplicationPort, config: KnowledgeAgentSkillConfig) -> None:
        self._qa = qa
        self._config = config
        self._completed: dict[UUID, QARunRecord] = {}
        registry = InMemoryToolRegistry(
            handlers={
                "inspect_retrieval": self.inspect_retrieval,
                "grounded_qa": self.grounded_qa,
            }
        )
        self.inspect_tool = registry.register(
            ToolDefinition(
                name="inspect_retrieval",
                version="1.0.0",
                description=(
                    "Inspect retrieval coverage for proposed queries in the current fixed Space "
                    "scope. "
                    "Returns counts only, never document text."
                ),
                input_schema=_RETRIEVAL_PLAN_SCHEMA,
                output_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["query_count", "hit_count", "matched_count", "document_count"],
                    "properties": {
                        "query_count": {"type": "integer", "minimum": 1},
                        "hit_count": {"type": "integer", "minimum": 0},
                        "matched_count": {"type": "integer", "minimum": 0},
                        "document_count": {"type": "integer", "minimum": 0},
                    },
                },
                permissions=frozenset({ToolPermission.READ_KNOWLEDGE}),
                handler_name="inspect_retrieval",
                model_visible=True,
            )
        )
        self.tool = registry.register(
            ToolDefinition(
                name="grounded_qa",
                version="1.0.0",
                description=(
                    "Generate a citation-validated answer using the selected retrieval plan in the "
                    "current fixed Space scope."
                ),
                input_schema=_RETRIEVAL_PLAN_SCHEMA,
                output_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["status", "citation_count", "result_type"],
                    "properties": {
                        "status": {"enum": ["completed", "refused"]},
                        "citation_count": {"type": "integer", "minimum": 0},
                        "result_type": {"type": "string", "minLength": 1},
                    },
                },
                permissions=frozenset({ToolPermission.READ_KNOWLEDGE, ToolPermission.MODEL}),
                handler_name="grounded_qa",
                model_visible=True,
                timeout_seconds=180.0,
            )
        )
        self.tool_registry: AgentToolRegistry = registry

    def replace_tool_registry(self, registry: AgentToolRegistry) -> None:
        """Install an infrastructure decorator without changing Tool definitions."""
        self.tool_registry = registry

    def handlers(self) -> dict[str, NodeHandler]:
        return {
            "knowledge_agent_plan": self.plan,
            "knowledge_agent_execute": self.execute,
            "knowledge_agent_verify": self.verify,
        }

    async def plan(self, context: NodeExecutionContext) -> NodeResult:
        question = context.input.get("question")
        if not isinstance(question, str) or not question.strip():
            raise NodeExecutionError(
                "SKILL_INPUT_INVALID", RunErrorCategory.INPUT, "Agent question is required."
            )
        return NodeResult()

    async def execute(self, context: NodeExecutionContext) -> NodeResult:
        try:
            loop_result = await BoundedLLMAgentNode(
                tool_registry=self.tool_registry,
                allowed_tools=(
                    ToolRef(self.inspect_tool.name, self.inspect_tool.version),
                    ToolRef(self.tool.name, self.tool.version),
                ),
                system_prompt=self._config.system_prompt,
                max_iterations=5,
                max_tokens_per_decision=2048,
                terminal_tools=frozenset({self.tool.name}),
            )(context)
        except NodeExecutionError as error:
            loop_result = NodeResult(
                state_updates={
                    "agent_decision": {
                        "action": "complete",
                        "reason": f"Planning fallback: {error.code}",
                    },
                    "agent_tool_calls": [],
                }
            )
        completed = self._completed.get(context.run.context.run_id)
        if completed is None:
            # A provider may terminate early despite the routing instruction. Preserve the
            # grounded QA contract with a server-side fallback instead of failing the user run.
            await self.grounded_qa(
                {},
                ToolExecutionContext(
                    run=context.run.context,
                    idempotency_key=f"{context.run.context.run_id}:fallback:grounded_qa",
                ),
            )
            completed = self._completed[context.run.context.run_id]
        if completed.result is None:
            raise NodeExecutionError(
                "RUN_WORKFLOW_INVALID",
                RunErrorCategory.SCHEMA,
                "Grounded QA returned no terminal result.",
            )
        return NodeResult(
            state_updates={
                "agent_decision": loop_result.state_updates.get("agent_decision", {}),
                "agent_tool_calls": loop_result.state_updates.get("agent_tool_calls", []),
                "qa_status": completed.status.value,
            },
            usage=loop_result.usage,
        )

    async def inspect_retrieval(
        self, arguments: dict[str, JSONValue], tool_context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        plan = _agent_plan(arguments)
        result = await self._qa.inspect_retrieval(
            tool_context.run.run_id, profile=self._config.profile, agent_plan=plan
        )
        return {
            "query_count": len(plan.additional_queries) + 1,
            "hit_count": len(result.hits),
            "matched_count": sum(not hit.context_only for hit in result.hits),
            "document_count": len({hit.document_id for hit in result.hits}),
        }

    async def grounded_qa(
        self, arguments: dict[str, JSONValue], tool_context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        completed = await self._qa.execute(
            tool_context.run.run_id,
            profile=self._config.profile,
            agent_plan=_agent_plan(arguments),
        )
        self._completed[tool_context.run.run_id] = completed
        if completed.status not in {QAStatus.COMPLETED, QAStatus.REFUSED}:
            raise qa_failure(completed)
        return {
            "status": completed.status.value,
            "citation_count": _citation_count(completed),
            "result_type": (
                completed.result.outcome.value if completed.result is not None else "unknown"
            ),
        }

    async def verify(self, context: NodeExecutionContext) -> NodeResult:
        status = context.state.get("qa_status")
        if status not in {QAStatus.COMPLETED.value, QAStatus.REFUSED.value}:
            raise NodeExecutionError(
                "RUN_WORKFLOW_INVALID",
                RunErrorCategory.SCHEMA,
                "Agent QA status is not terminal.",
            )
        return await LLMDecisionVerifyNode()(context)


def _citation_count(run: QARunRecord) -> int:
    # QARunRecord carries the answer citations; this helper avoids exposing full text to the Agent.
    if run.result is None or run.result.answer is None:
        return 0
    return len(run.result.answer.citations)


_RETRIEVAL_PLAN_SCHEMA: dict[str, JSONValue] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "additional_queries": {
            "type": "array",
            "maxItems": 5,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 12000},
        },
        "max_evidence_items": {"type": "integer", "minimum": 1, "maximum": 100000},
        "max_input_tokens": {"type": "integer", "minimum": 1, "maximum": 100000},
        "max_tokens_per_evidence": {"type": "integer", "minimum": 1, "maximum": 100000},
        "max_evidence_per_source": {"type": "integer", "minimum": 1, "maximum": 100000},
        "max_chunks_per_document": {"type": "integer", "minimum": 1, "maximum": 100000},
    },
}


def _agent_plan(arguments: dict[str, JSONValue]) -> AgentRetrievalPlan:
    queries = arguments.get("additional_queries", [])
    if not isinstance(queries, list) or not all(isinstance(query, str) for query in queries):
        raise ValueError("Agent retrieval queries must be strings")
    values: dict[str, int | None] = {}
    for name in (
        "max_evidence_items",
        "max_input_tokens",
        "max_tokens_per_evidence",
        "max_evidence_per_source",
        "max_chunks_per_document",
    ):
        value = arguments.get(name)
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            raise ValueError(f"Agent retrieval {name} must be an integer")
        values[name] = value
    return AgentRetrievalPlan(additional_queries=tuple(cast(list[str], queries)), **values)


__all__ = ["KnowledgeAgentSkillAdapter", "KnowledgeAgentSkillConfig"]
