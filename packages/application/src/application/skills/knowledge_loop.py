"""Knowledge Tools for the provisional generic Agent Loop.

The adapter deliberately contains no retrieval persistence or answer-generation logic.  Search is
performed through the application SearchService port, and the existing Grounded QA port remains
the sole owner of Evidence, citations, answer publication, and refusal semantics.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from uuid import UUID

from agent_runtime import (
    AgentLoopFinalizer,
    AgentToolRegistry,
    InMemoryToolRegistry,
    JSONValue,
    LLMDecision,
    LLMDecisionAction,
    NodeExecutionError,
    ToolDefinition,
    ToolExecutionContext,
    ToolRef,
)
from domain.agent_loop import AgentLoopState, AgentLoopTask
from domain.agent_runtime import AgentRun, RunErrorCategory, ToolPermission
from domain.grounded_qa import QAOutcome, QAStatus
from domain.qa_persistence import QARetrievalScope, QARunRecord, QARunVersions
from domain.retrieval import SearchFilters, SearchRequest

from application.qa.query_planning import SearchServicePort
from application.qa.service import (
    AgentRetrievalPlan,
    GroundedQAApplicationPort,
    GroundedQAExecutionProfile,
)

from .knowledge_qa import qa_failure


@dataclass(frozen=True)
class KnowledgeLoopToolsConfig:
    """Trusted per-Run inputs for the opt-in knowledge Agent Loop."""

    profile: GroundedQAExecutionProfile
    versions: QARunVersions
    retrieval_scope: QARetrievalScope = QARetrievalScope()
    max_search_observations: int = 8

    def __post_init__(self) -> None:
        if self.max_search_observations < 1:
            raise ValueError("Knowledge Loop search observation limit must be positive")
        if (
            self.profile.planning.profile_id != self.versions.profile_version
            or self.profile.retrieval.profile_version != self.versions.retrieval_profile_version
        ):
            raise ValueError("Knowledge Loop profile versions are inconsistent")


@dataclass(frozen=True)
class _SearchObservation:
    query: str
    hit_count: int
    matched_count: int
    context_only_count: int
    document_count: int
    source_count: int
    evidence_ids: tuple[str, ...]
    source_versions: tuple[tuple[str, str, str], ...]


@dataclass
class _RunFacts:
    searches: list[_SearchObservation] = field(default_factory=list)
    answer_run: QARunRecord | None = None
    verified: bool = False
    finalization_ready: bool = False


class KnowledgeLoopTools:
    """Register model-visible, content-safe Tools for one fixed QA Run.

    The process-local facts are only a gate for the current execution.  The authoritative answer
    and citation record always remain on the fixed QA Run, so an interrupted process cannot invent
    a final message or cite a different Run.
    """

    def __init__(
        self,
        *,
        qa: GroundedQAApplicationPort,
        search: SearchServicePort,
        config: KnowledgeLoopToolsConfig,
        result_reader: Callable[[UUID], Awaitable[QARunRecord | None]] | None = None,
    ) -> None:
        self._qa = qa
        self._search = search
        self._config = config
        self._result_reader = result_reader
        self._facts: dict[UUID, _RunFacts] = {}
        registry = InMemoryToolRegistry(
            handlers={
                "knowledge_search": self.knowledge_search,
                "knowledge_inspect": self.knowledge_inspect,
                "grounded_answer": self.grounded_answer,
                "verify_answer": self.verify_answer,
                "finalize_answer": self.finalize_answer,
            }
        )
        self.search_tool = registry.register(_knowledge_search_definition())
        self.inspect_tool = registry.register(_knowledge_inspect_definition())
        self.answer_tool = registry.register(_grounded_answer_definition())
        self.verify_tool = registry.register(_verify_answer_definition())
        self.finalize_tool = registry.register(_finalize_answer_definition())
        self.tool_registry: AgentToolRegistry = registry

    @property
    def allowed_tools(self) -> tuple[ToolRef, ...]:
        return (
            self.search_tool.ref,
            self.inspect_tool.ref,
            self.answer_tool.ref,
            self.verify_tool.ref,
            self.finalize_tool.ref,
        )

    def replace_tool_registry(self, registry: AgentToolRegistry) -> None:
        """Install an infrastructure tracing decorator without changing Tool definitions."""
        self.tool_registry = registry

    async def knowledge_search(
        self, arguments: dict[str, JSONValue], context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        query = _required_query(arguments)
        result = await self._search.search(
            SearchRequest(
                query=query,
                space_id=context.run.space_id,
                filters=SearchFilters(
                    source_ids=self._config.retrieval_scope.source_ids,
                    document_ids=self._config.retrieval_scope.document_ids,
                    version_ids=self._config.retrieval_scope.version_ids,
                ),
            ),
            self._config.profile.retrieval,
        )
        hits = result.hits
        facts = self._facts_for(context.run.run_id)
        if len(facts.searches) < self._config.max_search_observations:
            source_versions = tuple(
                (
                    str(hit.source_id),
                    str(hit.document_id),
                    str(hit.version_id),
                )
                for hit in hits
            )
            facts.searches.append(
                _SearchObservation(
                    query=query,
                    hit_count=len(hits),
                    matched_count=sum(not hit.context_only for hit in hits),
                    context_only_count=sum(hit.context_only for hit in hits),
                    document_count=len({hit.document_id for hit in hits}),
                    source_count=len({hit.source_id for hit in hits}),
                    evidence_ids=tuple(str(hit.chunk_id) for hit in hits),
                    source_versions=source_versions,
                )
            )
        return {
            "trust": "untrusted",
            "query_count": 1,
            "hit_count": len(hits),
            "matched_count": sum(not hit.context_only for hit in hits),
            "context_only_count": sum(hit.context_only for hit in hits),
            "evidence_ids": [str(hit.chunk_id) for hit in hits],
            "source_versions": [
                {
                    "source_id": str(hit.source_id),
                    "document_id": str(hit.document_id),
                    "version_id": str(hit.version_id),
                }
                for hit in hits
            ],
            "profile_version": result.diagnostics.profile_version,
        }

    async def knowledge_inspect(
        self, arguments: dict[str, JSONValue], context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        _require_empty(arguments)
        observations = self._facts_for(context.run.run_id).searches
        hit_count = sum(item.hit_count for item in observations)
        matched_count = sum(item.matched_count for item in observations)
        context_only_count = sum(item.context_only_count for item in observations)
        gap_signals: list[JSONValue] = []
        if not observations:
            gap_signals.append("no_search_observations")
        elif matched_count == 0:
            gap_signals.append("no_matched_evidence")
        elif matched_count <= context_only_count:
            gap_signals.append("matched_evidence_limited")
        if observations and max(item.source_count for item in observations) < 2:
            gap_signals.append("single_source_coverage")
        evidence_ids = tuple(
            dict.fromkeys(evidence_id for item in observations for evidence_id in item.evidence_ids)
        )
        source_versions = tuple(
            dict.fromkeys(
                item for observation in observations for item in observation.source_versions
            )
        )
        return {
            "trust": "untrusted",
            "search_count": len(observations),
            "hit_count": hit_count,
            "matched_count": matched_count,
            "context_only_count": context_only_count,
            "gap_signals": gap_signals,
            "evidence_ids": list(evidence_ids),
            "source_versions": [
                {
                    "source_id": source_id,
                    "document_id": document_id,
                    "version_id": version_id,
                }
                for source_id, document_id, version_id in source_versions
            ],
        }

    async def grounded_answer(
        self, arguments: dict[str, JSONValue], context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        _require_empty(arguments)
        facts = self._facts_for(context.run.run_id)
        if facts.answer_run is None:
            additional_queries = tuple(dict.fromkeys(item.query for item in facts.searches))[
                : self._config.profile.planning.max_subqueries - 1
            ]
            # SearchService retains the user question as the trusted base request.  Tool-selected
            # queries can only add bounded recall hints to the existing QA planning profile.
            plan = AgentRetrievalPlan(additional_queries=additional_queries)
            facts.answer_run = await self._qa.execute(
                context.run.run_id,
                profile=self._config.profile,
                agent_plan=plan,
            )
        completed = facts.answer_run
        if completed.status not in {QAStatus.COMPLETED, QAStatus.REFUSED}:
            raise qa_failure(completed)
        if completed.run_id != context.run.run_id or completed.result is None:
            raise NodeExecutionError(
                "SKILL_QA_RUN_INVALID",
                RunErrorCategory.SCHEMA,
                "Grounded QA did not return the current Run result.",
            )
        return _answer_status(completed)

    async def verify_answer(
        self, arguments: dict[str, JSONValue], context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        _require_empty(arguments)
        facts = self._facts_for(context.run.run_id)
        completed = facts.answer_run
        if completed is None:
            return {
                "ready": False,
                "outcome": "pending",
                "claim_count": 0,
                "citation_count": 0,
                "current_run_only": True,
                "conflict": False,
                "terminal_reason": "grounded_answer_required",
            }
        if completed.run_id != context.run.run_id or completed.result is None:
            raise NodeExecutionError(
                "SKILL_QA_RUN_INVALID",
                RunErrorCategory.SCHEMA,
                "Grounded QA verification escaped the current Run.",
            )
        result = completed.result
        if result.outcome is QAOutcome.ANSWER and result.answer is not None:
            citations = result.answer.citations
            citation_ids = {citation.evidence_id for citation in citations}
            claims_valid = all(
                set(claim.evidence_ids) <= citation_ids for claim in result.answer.claims
            )
            ready = bool(citations) and bool(result.answer.claims) and claims_valid
            terminal_reason = "answer_verified" if ready else "citation_incomplete"
            facts.verified = ready
            return {
                "ready": ready,
                "outcome": result.outcome.value,
                "claim_count": len(result.answer.claims),
                "citation_count": len(citations),
                "current_run_only": True,
                "conflict": False,
                "terminal_reason": terminal_reason,
            }
        if result.outcome is QAOutcome.REFUSE and result.refusal is not None:
            facts.verified = True
            return {
                "ready": True,
                "outcome": result.outcome.value,
                "claim_count": 0,
                "citation_count": 0,
                "current_run_only": True,
                "conflict": False,
                "terminal_reason": "evidence_insufficient",
            }
        if result.outcome is QAOutcome.CONFLICT and result.conflict is not None:
            facts.verified = True
            return {
                "ready": True,
                "outcome": result.outcome.value,
                "claim_count": 0,
                "citation_count": len(result.conflict.evidence_ids),
                "current_run_only": True,
                "conflict": True,
                "terminal_reason": "conflict",
            }
        raise NodeExecutionError(
            "SKILL_QA_RESULT_INVALID",
            RunErrorCategory.SCHEMA,
            "Grounded QA result is not a publishable outcome.",
        )

    async def finalize_answer(
        self, arguments: dict[str, JSONValue], context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        _require_empty(arguments)
        facts = self._facts_for(context.run.run_id)
        completed = facts.answer_run
        if completed is None or not facts.verified or completed.result is None:
            return {
                "ready": False,
                "publication": "grounded_qa",
                "outcome": "pending",
            }
        facts.finalization_ready = True
        return {
            "ready": True,
            "publication": "grounded_qa",
            "outcome": completed.result.outcome.value,
        }

    def finalizer(self) -> AgentLoopFinalizer:
        return _KnowledgeLoopFinalizer(self)

    def decision_policy(
        self, run: AgentRun, _state: AgentLoopState, decision: LLMDecision
    ) -> LLMDecision:
        """Enforce the QA-owned post-answer sequence regardless of model drift."""
        facts = self._facts_for(run.context.run_id)
        completed = facts.answer_run
        if completed is None:
            return decision
        if not facts.verified:
            if (
                decision.action is LLMDecisionAction.CALL_TOOL
                and decision.tool_name == "verify_answer"
            ):
                return decision
            return LLMDecision(
                action=LLMDecisionAction.CALL_TOOL,
                tool_name="verify_answer",
                arguments={},
                reason="server-required QA verification",
            )
        if not facts.finalization_ready:
            if (
                decision.action is LLMDecisionAction.CALL_TOOL
                and decision.tool_name == "finalize_answer"
            ):
                return decision
            return LLMDecision(
                action=LLMDecisionAction.CALL_TOOL,
                tool_name="finalize_answer",
                arguments={},
                reason="server-required QA finalization",
            )
        expected = (
            LLMDecisionAction.REFUSE
            if completed.result is not None
            and completed.result.outcome in {QAOutcome.REFUSE, QAOutcome.CONFLICT}
            else LLMDecisionAction.COMPLETE
        )
        if decision.action is expected:
            return decision
        return LLMDecision(action=expected, reason="server-verified QA terminal outcome")

    def _facts_for(self, run_id: UUID) -> _RunFacts:
        return self._facts.setdefault(run_id, _RunFacts())

    async def restore_finalization_facts(self, run_id: UUID) -> _RunFacts:
        """Rehydrate the QA-owned terminal result before retrying a checkpointed finalizer."""
        facts = self._facts_for(run_id)
        if facts.finalization_ready or self._result_reader is None:
            return facts
        completed = await self._result_reader(run_id)
        if (
            completed is not None
            and completed.status in {QAStatus.COMPLETED, QAStatus.REFUSED}
            and completed.result is not None
        ):
            facts.answer_run = completed
            facts.verified = True
            facts.finalization_ready = True
        return facts


class _KnowledgeLoopFinalizer:
    """Permit a Loop terminal transition only after the QA-owned finalization signal."""

    def __init__(self, tools: KnowledgeLoopTools) -> None:
        self._tools = tools

    async def finalize(
        self,
        *,
        run: AgentRun,
        task: AgentLoopTask,
        decision: LLMDecision,
        state: AgentLoopState,
        input_data: Mapping[str, JSONValue],
    ) -> JSONValue:
        del task, state, input_data
        facts = await self._tools.restore_finalization_facts(run.context.run_id)
        completed = facts.answer_run
        if not facts.finalization_ready or completed is None or completed.result is None:
            raise NodeExecutionError(
                "RUN_KNOWLEDGE_FINALIZATION_REQUIRED",
                RunErrorCategory.SCHEMA,
                "Knowledge Loop requires verified Grounded QA finalization.",
            )
        outcome = completed.result.outcome
        requires_refusal = outcome in {QAOutcome.REFUSE, QAOutcome.CONFLICT}
        expected_action = (
            LLMDecisionAction.REFUSE if requires_refusal else LLMDecisionAction.COMPLETE
        )
        if decision.action is not expected_action:
            raise NodeExecutionError(
                "RUN_KNOWLEDGE_TERMINAL_MISMATCH",
                RunErrorCategory.SCHEMA,
                "Knowledge Loop terminal action does not match Grounded QA verification.",
            )
        # Grounded QA already published the single user-visible Assistant message atomically.  The
        # generic Loop finalizer emits only a safe routing projection and never publishes again.
        return {
            "status": completed.status.value,
            "outcome": outcome.value,
            "qa_run_id": str(completed.run_id),
            "publication": "grounded_qa",
        }


def _knowledge_search_definition() -> ToolDefinition:
    return ToolDefinition(
        name="knowledge_search",
        version="1.0.0",
        description="Search the fixed current-Space knowledge scope and return metadata only.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["query"],
            "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 512}},
        },
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": [
                "trust",
                "query_count",
                "hit_count",
                "matched_count",
                "context_only_count",
                "evidence_ids",
                "source_versions",
                "profile_version",
            ],
            "properties": {
                "trust": {"const": "untrusted"},
                "query_count": {"const": 1},
                "hit_count": {"type": "integer", "minimum": 0},
                "matched_count": {"type": "integer", "minimum": 0},
                "context_only_count": {"type": "integer", "minimum": 0},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "source_versions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["source_id", "document_id", "version_id"],
                        "properties": {
                            "source_id": {"type": "string"},
                            "document_id": {"type": "string"},
                            "version_id": {"type": "string"},
                        },
                    },
                },
                "profile_version": {"type": "string", "minLength": 1},
            },
        },
        permissions=frozenset({ToolPermission.READ_KNOWLEDGE}),
        handler_name="knowledge_search",
        model_visible=True,
        max_retries=1,
    )


def _knowledge_inspect_definition() -> ToolDefinition:
    return ToolDefinition(
        name="knowledge_inspect",
        version="1.0.0",
        description="Inspect only safe coverage metadata from earlier knowledge searches.",
        input_schema=_EMPTY_SCHEMA,
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": [
                "trust",
                "search_count",
                "hit_count",
                "matched_count",
                "context_only_count",
                "gap_signals",
                "evidence_ids",
                "source_versions",
            ],
            "properties": {
                "trust": {"const": "untrusted"},
                "search_count": {"type": "integer", "minimum": 0},
                "hit_count": {"type": "integer", "minimum": 0},
                "matched_count": {"type": "integer", "minimum": 0},
                "context_only_count": {"type": "integer", "minimum": 0},
                "gap_signals": {"type": "array", "items": {"type": "string"}},
                "evidence_ids": {"type": "array", "items": {"type": "string"}},
                "source_versions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["source_id", "document_id", "version_id"],
                        "properties": {
                            "source_id": {"type": "string"},
                            "document_id": {"type": "string"},
                            "version_id": {"type": "string"},
                        },
                    },
                },
            },
        },
        permissions=frozenset({ToolPermission.READ_KNOWLEDGE}),
        handler_name="knowledge_inspect",
        model_visible=True,
    )


def _grounded_answer_definition() -> ToolDefinition:
    return ToolDefinition(
        name="grounded_answer",
        version="1.0.0",
        description="Delegate answer generation and publication to the fixed Grounded QA Run.",
        input_schema=_EMPTY_SCHEMA,
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["status", "outcome", "claim_count", "citation_count"],
            "properties": {
                "status": {"enum": [QAStatus.COMPLETED.value, QAStatus.REFUSED.value]},
                "outcome": {"enum": [item.value for item in QAOutcome]},
                "claim_count": {"type": "integer", "minimum": 0},
                "citation_count": {"type": "integer", "minimum": 0},
            },
        },
        permissions=frozenset({ToolPermission.READ_KNOWLEDGE, ToolPermission.MODEL}),
        handler_name="grounded_answer",
        model_visible=True,
        timeout_seconds=180.0,
    )


def _verify_answer_definition() -> ToolDefinition:
    return ToolDefinition(
        name="verify_answer",
        version="1.0.0",
        description=(
            "Verify the current Grounded QA result using claims and citation metadata only."
        ),
        input_schema=_EMPTY_SCHEMA,
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": [
                "ready",
                "outcome",
                "claim_count",
                "citation_count",
                "current_run_only",
                "conflict",
                "terminal_reason",
            ],
            "properties": {
                "ready": {"type": "boolean"},
                "outcome": {"type": "string", "minLength": 1},
                "claim_count": {"type": "integer", "minimum": 0},
                "citation_count": {"type": "integer", "minimum": 0},
                "current_run_only": {"const": True},
                "conflict": {"type": "boolean"},
                "terminal_reason": {"type": "string", "minLength": 1},
            },
        },
        permissions=frozenset({ToolPermission.READ_KNOWLEDGE}),
        handler_name="verify_answer",
        model_visible=True,
    )


def _finalize_answer_definition() -> ToolDefinition:
    return ToolDefinition(
        name="finalize_answer",
        version="1.0.0",
        description="Signal that the verified QA result may enter the Loop finalization gate.",
        input_schema=_EMPTY_SCHEMA,
        output_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["ready", "publication", "outcome"],
            "properties": {
                "ready": {"type": "boolean"},
                "publication": {"const": "grounded_qa"},
                "outcome": {"type": "string", "minLength": 1},
            },
        },
        permissions=frozenset({ToolPermission.READ_KNOWLEDGE}),
        handler_name="finalize_answer",
        model_visible=True,
    )


_EMPTY_SCHEMA: dict[str, JSONValue] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {},
}


def _required_query(arguments: dict[str, JSONValue]) -> str:
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise NodeExecutionError(
            "SKILL_INPUT_INVALID", RunErrorCategory.INPUT, "Knowledge search query is required."
        )
    return query


def _require_empty(arguments: dict[str, JSONValue]) -> None:
    if arguments:
        raise NodeExecutionError(
            "SKILL_INPUT_INVALID", RunErrorCategory.INPUT, "Knowledge Tool does not accept input."
        )


def _answer_status(run: QARunRecord) -> dict[str, JSONValue]:
    assert run.result is not None
    answer = run.result.answer
    return {
        "status": run.status.value,
        "outcome": run.result.outcome.value,
        "claim_count": len(answer.claims) if answer is not None else 0,
        "citation_count": len(answer.citations) if answer is not None else 0,
    }


__all__ = ["KnowledgeLoopTools", "KnowledgeLoopToolsConfig"]
