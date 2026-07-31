"""LLM-directed read-only Agent adapter over the existing Grounded QA Port."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from agent_runtime import (
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

from application.qa.service import GroundedQAApplicationPort, GroundedQAExecutionProfile

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
        self.tool_registry = InMemoryToolRegistry(handlers={"grounded_qa": self.grounded_qa})
        self.tool = self.tool_registry.register(
            ToolDefinition(
                name="grounded_qa",
                version="1.0.0",
                description="Run grounded QA for the current question in the current Space.",
                input_schema={"type": "object", "additionalProperties": False},
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
            )
        )

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
        loop_result = await BoundedLLMAgentNode(
            tool_registry=self.tool_registry,
            allowed_tools=(ToolRef(self.tool.name, self.tool.version),),
            system_prompt=self._config.system_prompt,
            max_iterations=2,
            max_tokens_per_decision=512,
        )(context)
        completed = self._completed.get(context.run.context.run_id)
        if completed is None:
            raise NodeExecutionError(
                "RUN_LLM_TOOL_REQUIRED",
                RunErrorCategory.SCHEMA,
                "The Agent must call grounded_qa before completing.",
            )
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

    async def grounded_qa(
        self, _arguments: dict[str, JSONValue], tool_context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        completed = await self._qa.execute(tool_context.run.run_id, profile=self._config.profile)
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


__all__ = ["KnowledgeAgentSkillAdapter", "KnowledgeAgentSkillConfig"]
