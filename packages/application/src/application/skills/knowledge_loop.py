"""Knowledge Tools for the provisional generic Agent Loop.

The adapter deliberately contains no retrieval persistence or answer-generation logic.  Search is
performed through the application SearchService port, and the existing Grounded QA port remains
the sole owner of Evidence, citations, answer publication, and refusal semantics.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol, cast
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
    ToolHandler,
    ToolRef,
)
from domain.agent_loop import AgentLoopState, AgentLoopTask
from domain.agent_runtime import AgentRun, ApprovalPort, RunErrorCategory, ToolPermission
from domain.grounded_qa import QAOutcome, QAStatus
from domain.qa_persistence import QARetrievalScope, QARunRecord, QARunVersions
from domain.retrieval import SearchFilters, SearchHit, SearchRequest

from application.qa.query_planning import SearchServicePort
from application.qa.service import (
    AgentRetrievalPlan,
    GroundedQAApplicationPort,
    GroundedQAExecutionProfile,
)

from .knowledge_qa import qa_failure

type EnsureQARun = Callable[[ToolExecutionContext, QARetrievalScope], Awaitable[QARunRecord]]
type AdditionalToolRegistrar = Callable[[InMemoryToolRegistry], tuple[ToolDefinition, ...]]


class ResourceScopeResolver(Protocol):
    """Minimal resource port needed by a model-visible document Skill adapter."""

    async def resolve(self, *, space_id: UUID, resource_type: str, reference: str) -> object: ...


@dataclass(frozen=True)
class KnowledgeLoopToolsConfig:
    """Trusted per-Run inputs for the opt-in knowledge Agent Loop."""

    profile: GroundedQAExecutionProfile
    versions: QARunVersions
    retrieval_scope: QARetrievalScope = QARetrievalScope()
    max_search_observations: int = 8
    tool_version: str = "1.0.0"
    resource_resolver: ResourceScopeResolver | None = None

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
    scope: QARetrievalScope = QARetrievalScope()


@dataclass
class _RunFacts:
    started: bool = False
    searches: list[_SearchObservation] = field(default_factory=list)
    inspections: int = 0
    last_inspected_search_count: int = 0
    gap_signals: tuple[str, ...] = ()
    answer_run: QARunRecord | None = None
    verified: bool = False
    finalization_ready: bool = False
    document_skill_used: bool = False
    document_scopes: list[QARetrievalScope] = field(default_factory=list)
    clarification_needed: bool = False


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
        ensure_qa_run: EnsureQARun | None = None,
        extra_handlers: Mapping[str, ToolHandler] | None = None,
        extra_tool_registrar: AdditionalToolRegistrar | None = None,
        approval_port: ApprovalPort | None = None,
    ) -> None:
        self._qa = qa
        self._search = search
        self._config = config
        self._result_reader = result_reader
        self._ensure_qa_run = ensure_qa_run
        self._facts: dict[UUID, _RunFacts] = {}
        handlers: dict[str, ToolHandler] = {
            "knowledge_search": self.knowledge_search,
            "knowledge_inspect": self.knowledge_inspect,
            "grounded_answer": self.grounded_answer,
            "verify_answer": self.verify_answer,
            "finalize_answer": self.finalize_answer,
            **(
                {"summarize_document": self.summarize_document}
                if config.resource_resolver is not None
                else {}
            ),
        }
        if extra_handlers is not None:
            if set(handlers).intersection(extra_handlers):
                raise ValueError("Additional Tool handlers conflict with knowledge Tools")
            handlers.update(extra_handlers)
        if (extra_handlers is None) != (extra_tool_registrar is None):
            raise ValueError("Additional Tool handlers and definitions must be supplied together")
        registry = InMemoryToolRegistry(handlers=handlers, approval_port=approval_port)
        self.search_tool = registry.register(_knowledge_search_definition(config.tool_version))
        self.inspect_tool = registry.register(_knowledge_inspect_definition(config.tool_version))
        self.answer_tool = registry.register(_grounded_answer_definition(config.tool_version))
        self.verify_tool = registry.register(_verify_answer_definition(config.tool_version))
        self.finalize_tool = registry.register(_finalize_answer_definition(config.tool_version))
        self.summary_tool = (
            registry.register(_summarize_document_definition(config.tool_version))
            if config.resource_resolver is not None
            else None
        )
        self.additional_tools = (
            extra_tool_registrar(registry) if extra_tool_registrar is not None else ()
        )
        self.tool_registry: AgentToolRegistry = registry

    @property
    def allowed_tools(self) -> tuple[ToolRef, ...]:
        tools = [
            self.search_tool.ref,
            self.inspect_tool.ref,
            self.answer_tool.ref,
            self.verify_tool.ref,
            self.finalize_tool.ref,
        ]
        if self.summary_tool is not None:
            tools.insert(2, self.summary_tool.ref)
        tools.extend(tool.ref for tool in self.additional_tools)
        return tuple(tools)

    async def summarize_document(
        self, arguments: dict[str, JSONValue], context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        reference, focus = _document_arguments(arguments)
        facts = self._facts_for(context.run.run_id)
        facts.started = True
        facts.document_skill_used = True
        resolver = self._config.resource_resolver
        if resolver is None:
            return self._with_guidance(
                {
                    "trust": "untrusted",
                    "status": "unavailable",
                    "candidate_count": 0,
                    "hit_count": 0,
                    "matched_count": 0,
                    "context_only_count": 0,
                    "evidence_ids": [],
                    "source_versions": [],
                },
                recommended_next="clarify",
            )
        try:
            resolved = await resolver.resolve(
                space_id=context.run.space_id, resource_type="document", reference=reference
            )
            scope = getattr(resolved, "scope", None)
            if not isinstance(scope, QARetrievalScope):
                raise ValueError("RESOURCE_SCOPE_INVALID")
        except ValueError as exc:
            facts.clarification_needed = True
            code = str(getattr(exc, "code", ""))
            candidates = getattr(exc, "candidates", ())
            labels = cast(
                list[JSONValue],
                [
                    str(getattr(candidate, "label", ""))[:280]
                    for candidate in candidates
                    if getattr(candidate, "label", "")
                ][:20],
            )
            status = "ambiguous" if code.endswith("CONFLICT") else "not_found"
            return self._with_guidance(
                {
                    "trust": "untrusted",
                    "status": status,
                    "candidate_count": len(labels),
                    "candidate_labels": labels,
                    "hit_count": 0,
                    "matched_count": 0,
                    "context_only_count": 0,
                    "evidence_ids": [],
                    "source_versions": [],
                },
                recommended_next="clarify",
            )
        facts.document_scopes.append(scope)
        query = _document_query(reference, focus)
        return await self._search_with_scope(query, scope, facts, context)

    async def _search_with_scope(
        self,
        query: str,
        scope: QARetrievalScope,
        facts: _RunFacts,
        context: ToolExecutionContext,
    ) -> dict[str, JSONValue]:
        result = await self._search.search(
            SearchRequest(
                query=query,
                space_id=context.run.space_id,
                filters=SearchFilters(
                    source_ids=scope.source_ids,
                    document_ids=scope.document_ids,
                    version_ids=scope.version_ids,
                ),
            ),
            self._config.profile.retrieval,
        )
        hits = result.hits
        if len(facts.searches) < self._config.max_search_observations:
            facts.searches.append(_observation(query, hits, scope=scope))
        return self._with_guidance(
            {
                "trust": "untrusted",
                "status": "resolved",
                "candidate_count": 1,
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
            },
            recommended_next="knowledge_inspect",
        )

    def replace_tool_registry(self, registry: AgentToolRegistry) -> None:
        """Install an infrastructure tracing decorator without changing Tool definitions."""
        self.tool_registry = registry

    async def knowledge_search(
        self, arguments: dict[str, JSONValue], context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        query = _required_query(arguments)
        facts = self._facts_for(context.run.run_id)
        facts.started = True
        scope = self._qa_scope(facts)
        result = await self._search.search(
            SearchRequest(
                query=query,
                space_id=context.run.space_id,
                filters=SearchFilters(
                    source_ids=scope.source_ids,
                    document_ids=scope.document_ids,
                    version_ids=scope.version_ids,
                ),
            ),
            self._config.profile.retrieval,
        )
        hits = result.hits
        if len(facts.searches) < self._config.max_search_observations:
            facts.searches.append(_observation(query, hits))
        return self._with_guidance(
            {
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
            },
            recommended_next="knowledge_inspect",
        )

    async def knowledge_inspect(
        self, arguments: dict[str, JSONValue], context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        _require_inspection(arguments)
        facts = self._facts_for(context.run.run_id)
        facts.started = True
        observations = facts.searches
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
        facts.inspections += 1
        facts.last_inspected_search_count = len(observations)
        facts.gap_signals = tuple(str(signal) for signal in gap_signals)
        evidence_ids = tuple(
            dict.fromkeys(evidence_id for item in observations for evidence_id in item.evidence_ids)
        )
        source_versions = tuple(
            dict.fromkeys(
                item for observation in observations for item in observation.source_versions
            )
        )
        return self._with_guidance(
            {
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
            },
            recommended_next=(
                "knowledge_search" if _needs_followup_search(facts) else "grounded_answer"
            ),
        )

    async def grounded_answer(
        self, arguments: dict[str, JSONValue], context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        _require_empty(arguments)
        facts = self._facts_for(context.run.run_id)
        facts.started = True
        if self._uses_advisory_guidance() and not facts.searches:
            return self._with_guidance(
                {
                    "status": "pending",
                    "outcome": "pending",
                    "claim_count": 0,
                    "citation_count": 0,
                },
                recommended_next="knowledge_search",
            )
        if self._uses_advisory_guidance() and facts.last_inspected_search_count < len(
            facts.searches
        ):
            return self._with_guidance(
                {
                    "status": "pending",
                    "outcome": "pending",
                    "claim_count": 0,
                    "citation_count": 0,
                },
                recommended_next="knowledge_inspect",
            )
        if facts.answer_run is None and self._result_reader is not None:
            facts.answer_run = await self._result_reader(context.run.run_id)
        if facts.answer_run is None and self._ensure_qa_run is not None:
            facts.answer_run = await self._ensure_qa_run(context, self._qa_scope(facts))
        if facts.answer_run is None or facts.answer_run.status not in {
            QAStatus.COMPLETED,
            QAStatus.REFUSED,
        }:
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
        return self._with_guidance(_answer_status(completed), recommended_next="verify_answer")

    async def verify_answer(
        self, arguments: dict[str, JSONValue], context: ToolExecutionContext
    ) -> dict[str, JSONValue]:
        _require_empty(arguments)
        facts = self._facts_for(context.run.run_id)
        facts.started = True
        completed = facts.answer_run
        if completed is None:
            return self._with_guidance(
                {
                    "ready": False,
                    "outcome": "pending",
                    "claim_count": 0,
                    "citation_count": 0,
                    "current_run_only": True,
                    "conflict": False,
                    "terminal_reason": "grounded_answer_required",
                },
                recommended_next="knowledge_search",
            )
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
            return self._with_guidance(
                {
                    "ready": ready,
                    "outcome": result.outcome.value,
                    "claim_count": len(result.answer.claims),
                    "citation_count": len(citations),
                    "current_run_only": True,
                    "conflict": False,
                    "terminal_reason": terminal_reason,
                },
                recommended_next="finalize_answer" if ready else "knowledge_search",
            )
        if result.outcome is QAOutcome.REFUSE and result.refusal is not None:
            facts.verified = True
            return self._with_guidance(
                {
                    "ready": True,
                    "outcome": result.outcome.value,
                    "claim_count": 0,
                    "citation_count": 0,
                    "current_run_only": True,
                    "conflict": False,
                    "terminal_reason": "evidence_insufficient",
                },
                recommended_next="finalize_answer",
            )
        if result.outcome is QAOutcome.CONFLICT and result.conflict is not None:
            facts.verified = True
            return self._with_guidance(
                {
                    "ready": True,
                    "outcome": result.outcome.value,
                    "claim_count": 0,
                    "citation_count": len(result.conflict.evidence_ids),
                    "current_run_only": True,
                    "conflict": True,
                    "terminal_reason": "conflict",
                },
                recommended_next="finalize_answer",
            )
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
        facts.started = True
        completed = facts.answer_run
        if completed is None or not facts.verified or completed.result is None:
            return self._with_guidance(
                {
                    "ready": False,
                    "publication": "grounded_qa",
                    "outcome": "pending",
                },
                recommended_next="verify_answer" if completed is not None else "knowledge_search",
            )
        facts.finalization_ready = True
        terminal = (
            "refuse"
            if completed.result.outcome in {QAOutcome.REFUSE, QAOutcome.CONFLICT}
            else "complete"
        )
        return self._with_guidance(
            {
                "ready": True,
                "publication": "grounded_qa",
                "outcome": completed.result.outcome.value,
            },
            recommended_next=terminal,
        )

    def finalizer(self) -> AgentLoopFinalizer:
        return _KnowledgeLoopFinalizer(self)

    def decision_policy(
        self, run: AgentRun, state: AgentLoopState, decision: LLMDecision
    ) -> LLMDecision:
        """Enforce retrieval and QA gates regardless of model drift.

        The prompt explains the workflow, while this policy makes an early terminal decision
        harmless: the model cannot skip the first search, coverage inspection, grounded QA, or
        the post-answer verification/finalization gates.
        """
        facts = self._facts_for(run.context.run_id)
        completed = facts.answer_run
        if self._config.versions.skill_version in {"0.7.0", "0.8.0", "0.9.0"}:
            required_document = _explicit_document_summary_reference(state.task.goal)
            if required_document is not None and not facts.document_skill_used:
                if self.summary_tool is None:
                    return LLMDecision(
                        action=LLMDecisionAction.CLARIFY,
                        reason="The named document summary capability is unavailable.",
                    )
                if decision.action is LLMDecisionAction.CALL_TOOL and (
                    decision.tool_name in {"knowledge_search", "knowledge_inspect"}
                    or (
                        decision.tool_name == "summarize_document"
                        and _valid_document_summary_arguments(decision.arguments, required_document)
                    )
                ):
                    return decision
                return LLMDecision(
                    action=LLMDecisionAction.CALL_TOOL,
                    tool_name="summarize_document",
                    arguments={"document_reference": required_document},
                    reason="server-required explicit document summary",
                )
            # The outer Assistant shares these Tools with ordinary conversation. Until a
            # knowledge Tool is actually selected, leave direct/clarification decisions alone.
            if not facts.started and any(
                observation.tool_name
                in {
                    "knowledge_search",
                    "knowledge_inspect",
                    "summarize_document",
                    "grounded_answer",
                    "verify_answer",
                    "finalize_answer",
                }
                for observation in state.observations
            ):
                facts.started = True
            if not facts.started:
                return decision
            _restore_finalization_from_checkpoint(facts, state)
            if facts.clarification_needed:
                if decision.action is LLMDecisionAction.CLARIFY:
                    return decision
                return LLMDecision(
                    action=LLMDecisionAction.CLARIFY,
                    reason="A document selection is needed before the request can continue.",
                )
            if (
                decision.action is LLMDecisionAction.CALL_TOOL
                and decision.tool_name == "knowledge_search"
                and _valid_search_arguments(decision.arguments)
            ):
                query = str(decision.arguments["query"])
                if facts.searches and (
                    query in {item.query for item in facts.searches} or _is_meta_search_query(query)
                ):
                    if facts.last_inspected_search_count < len(facts.searches):
                        return LLMDecision(
                            action=LLMDecisionAction.CALL_TOOL,
                            tool_name="knowledge_inspect",
                            arguments={"inspection_round": facts.inspections + 1},
                            reason="server-required retrieval coverage inspection",
                        )
                    return LLMDecision(
                        action=LLMDecisionAction.CALL_TOOL,
                        tool_name="grounded_answer",
                        arguments={},
                        reason="server-recovered repeated or meta retrieval query",
                    )
            workspace_write = self._resolve_qa_workspace_write(decision, facts)
            if workspace_write is not None:
                return workspace_write
            # A checkpoint can preserve the observation history before process-local search facts
            # are rehydrated. A new bounded query is allowed to rebuild those facts; do not
            # manufacture a query from an instruction such as "use knowledge_search".
            # A terminal answer can never bypass QA-owned verification and publication.  Unlike
            # 0.6.0 this policy does not prescribe retrieval order; individual Tools return a
            # local, model-visible next-step recommendation when their precondition is unmet.
            if not facts.finalization_ready and decision.action is not LLMDecisionAction.CALL_TOOL:
                return LLMDecision(
                    action=LLMDecisionAction.CALL_TOOL,
                    tool_name="finalize_answer",
                    arguments={},
                    reason="server-required finalization check",
                )
            if not facts.finalization_ready:
                return decision
            if decision.action not in {LLMDecisionAction.COMPLETE, LLMDecisionAction.REFUSE}:
                return decision
            if completed is None or completed.result is None:
                return decision
            expected = _terminal_action(completed)
            return (
                decision
                if decision.action is expected
                else LLMDecision(
                    action=expected,
                    reason="server-verified QA terminal outcome",
                )
            )
        if completed is None:
            if self._config.versions.skill_version != "0.6.0":
                return decision
            if not facts.searches:
                if (
                    decision.action is LLMDecisionAction.CALL_TOOL
                    and decision.tool_name == "knowledge_search"
                    and _valid_search_arguments(decision.arguments)
                ):
                    return decision
                return LLMDecision(
                    action=LLMDecisionAction.CALL_TOOL,
                    tool_name="knowledge_search",
                    arguments={"query": _bounded_query(state.task.goal)},
                    reason="server-required initial knowledge search",
                )
            if facts.last_inspected_search_count < len(facts.searches):
                return LLMDecision(
                    action=LLMDecisionAction.CALL_TOOL,
                    tool_name="knowledge_inspect",
                    arguments={"inspection_round": facts.inspections + 1},
                    reason="server-required retrieval coverage inspection",
                )
            if _needs_followup_search(facts) and len(facts.searches) < 2:
                if (
                    decision.action is LLMDecisionAction.CALL_TOOL
                    and decision.tool_name == "knowledge_search"
                    and _valid_search_arguments(decision.arguments)
                ):
                    return decision
                return LLMDecision(
                    action=LLMDecisionAction.CALL_TOOL,
                    tool_name="knowledge_search",
                    arguments={"query": _followup_query(facts, state.task.goal)},
                    reason="server-required follow-up for weak retrieval coverage",
                )
            if (
                decision.action is LLMDecisionAction.CALL_TOOL
                and decision.tool_name == "grounded_answer"
                and not decision.arguments
            ):
                return decision
            return LLMDecision(
                action=LLMDecisionAction.CALL_TOOL,
                tool_name="grounded_answer",
                arguments={},
                reason="server-required grounded QA execution",
            )
        if not facts.verified:
            if (
                decision.action is LLMDecisionAction.CALL_TOOL
                and decision.tool_name == "verify_answer"
                and not decision.arguments
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
                and not decision.arguments
            ):
                return decision
            return LLMDecision(
                action=LLMDecisionAction.CALL_TOOL,
                tool_name="finalize_answer",
                arguments={},
                reason="server-required QA finalization",
            )
        if decision.action not in {LLMDecisionAction.COMPLETE, LLMDecisionAction.REFUSE}:
            return decision
        if completed is None or completed.result is None:
            return decision
        expected = _terminal_action(completed)
        if decision.action is expected:
            return decision
        return LLMDecision(action=expected, reason="server-verified QA terminal outcome")

    def _uses_advisory_guidance(self) -> bool:
        return self._config.tool_version == "1.1.0"

    def _with_guidance(
        self, value: dict[str, JSONValue], *, recommended_next: str
    ) -> dict[str, JSONValue]:
        if self._uses_advisory_guidance():
            return {**value, "recommended_next": recommended_next}
        return value

    def _facts_for(self, run_id: UUID) -> _RunFacts:
        return self._facts.setdefault(run_id, _RunFacts())

    def _resolve_qa_workspace_write(
        self, decision: LLMDecision, facts: _RunFacts
    ) -> LLMDecision | None:
        """Resolve an explicitly requested write of the fixed QA artifact.

        The model chooses whether to save, where to save, and when workspace inspection is useful.
        It cannot see the authoritative answer text, so the marker is resolved only after the
        existing QA verification gate has completed.
        """
        if (
            decision.action is not LLMDecisionAction.CALL_TOOL
            or decision.tool_name != "fs_write"
            or decision.arguments.get("content") != _CURRENT_QA_ANSWER_MARKER
        ):
            return None
        completed = facts.answer_run
        if completed is None or completed.result is None:
            return LLMDecision(
                action=LLMDecisionAction.CALL_TOOL,
                tool_name="grounded_answer",
                arguments={},
                reason="QA result required for the model-selected workspace write",
            )
        if not facts.verified:
            return LLMDecision(
                action=LLMDecisionAction.CALL_TOOL,
                tool_name="verify_answer",
                arguments={},
                reason="QA verification required for the model-selected workspace write",
            )
        if not facts.finalization_ready:
            return LLMDecision(
                action=LLMDecisionAction.CALL_TOOL,
                tool_name="finalize_answer",
                arguments={},
                reason="QA finalization required for the model-selected workspace write",
            )
        content = _qa_result_markdown(completed)
        if content is None:
            return None
        return LLMDecision(
            action=LLMDecisionAction.CALL_TOOL,
            tool_name="fs_write",
            arguments={**decision.arguments, "content": content},
            reason=decision.reason,
        )

    def _qa_scope(self, facts: _RunFacts) -> QARetrievalScope:
        """Pin any document Skill observations before creating the shared QA projection."""
        if not facts.document_skill_used:
            return self._config.retrieval_scope
        scopes = [self._config.retrieval_scope, *facts.document_scopes]
        scopes.extend(item.scope for item in facts.searches)
        return QARetrievalScope(
            source_ids=frozenset(value for scope in scopes for value in scope.source_ids),
            document_ids=frozenset(value for scope in scopes for value in scope.document_ids),
            version_ids=frozenset(value for scope in scopes for value in scope.version_ids),
        )

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


def _knowledge_search_definition(version: str) -> ToolDefinition:
    return ToolDefinition(
        name="knowledge_search",
        version=version,
        description="Search the fixed current-Space knowledge scope and return metadata only.",
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["query"],
            "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 512}},
        },
        output_schema=_with_guidance_schema(
            {
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
            version=version,
            next_actions=("knowledge_inspect",),
        ),
        permissions=frozenset({ToolPermission.READ_KNOWLEDGE}),
        handler_name="knowledge_search",
        model_visible=True,
        max_retries=1,
    )


def _summarize_document_definition(version: str) -> ToolDefinition:
    return ToolDefinition(
        name="summarize_document",
        version=version,
        description=(
            "Resolve one named published document in the current Space and search it for a "
            "grounded summary; returns metadata only."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["document_reference"],
            "properties": {
                "document_reference": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 280,
                },
                "focus": {"type": "string", "maxLength": 256},
            },
        },
        output_schema=_with_guidance_schema(
            {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "trust",
                    "status",
                    "candidate_count",
                    "hit_count",
                    "matched_count",
                    "context_only_count",
                    "evidence_ids",
                    "source_versions",
                ],
                "properties": {
                    "trust": {"const": "untrusted"},
                    "status": {"enum": ["resolved", "not_found", "ambiguous", "unavailable"]},
                    "candidate_count": {"type": "integer", "minimum": 0},
                    "candidate_labels": {
                        "type": "array",
                        "maxItems": 20,
                        "items": {"type": "string", "minLength": 1, "maxLength": 280},
                    },
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
            version=version,
            next_actions=("knowledge_inspect", "knowledge_search", "clarify"),
        ),
        permissions=frozenset({ToolPermission.READ_KNOWLEDGE}),
        handler_name="summarize_document",
        model_visible=True,
        max_retries=1,
    )


def _knowledge_inspect_definition(version: str) -> ToolDefinition:
    return ToolDefinition(
        name="knowledge_inspect",
        version=version,
        description="Inspect only safe coverage metadata from earlier knowledge searches.",
        input_schema=_INSPECTION_SCHEMA,
        output_schema=_with_guidance_schema(
            {
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
            version=version,
            next_actions=("knowledge_search", "grounded_answer"),
        ),
        permissions=frozenset({ToolPermission.READ_KNOWLEDGE}),
        handler_name="knowledge_inspect",
        model_visible=True,
    )


def _grounded_answer_definition(version: str) -> ToolDefinition:
    return ToolDefinition(
        name="grounded_answer",
        version=version,
        description="Delegate answer generation and publication to the fixed Grounded QA Run.",
        input_schema=_EMPTY_SCHEMA,
        output_schema=_with_guidance_schema(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["status", "outcome", "claim_count", "citation_count"],
                "properties": {
                    "status": {
                        "enum": (
                            [QAStatus.COMPLETED.value, QAStatus.REFUSED.value, "pending"]
                            if version == "1.1.0"
                            else [QAStatus.COMPLETED.value, QAStatus.REFUSED.value]
                        )
                    },
                    "outcome": {
                        "enum": (
                            [*(item.value for item in QAOutcome), "pending"]
                            if version == "1.1.0"
                            else [item.value for item in QAOutcome]
                        )
                    },
                    "claim_count": {"type": "integer", "minimum": 0},
                    "citation_count": {"type": "integer", "minimum": 0},
                },
            },
            version=version,
            next_actions=("knowledge_search", "knowledge_inspect", "verify_answer"),
        ),
        permissions=frozenset({ToolPermission.READ_KNOWLEDGE, ToolPermission.MODEL}),
        handler_name="grounded_answer",
        model_visible=True,
        timeout_seconds=180.0,
    )


def _verify_answer_definition(version: str) -> ToolDefinition:
    return ToolDefinition(
        name="verify_answer",
        version=version,
        description=(
            "Verify the current Grounded QA result using claims and citation metadata only."
        ),
        input_schema=_EMPTY_SCHEMA,
        output_schema=_with_guidance_schema(
            {
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
            version=version,
            next_actions=("knowledge_search", "finalize_answer"),
        ),
        permissions=frozenset({ToolPermission.READ_KNOWLEDGE}),
        handler_name="verify_answer",
        model_visible=True,
    )


def _finalize_answer_definition(version: str) -> ToolDefinition:
    return ToolDefinition(
        name="finalize_answer",
        version=version,
        description="Signal that the verified QA result may enter the Loop finalization gate.",
        input_schema=_EMPTY_SCHEMA,
        output_schema=_with_guidance_schema(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["ready", "publication", "outcome"],
                "properties": {
                    "ready": {"type": "boolean"},
                    "publication": {"const": "grounded_qa"},
                    "outcome": {"type": "string", "minLength": 1},
                },
            },
            version=version,
            next_actions=(
                "knowledge_search",
                "verify_answer",
                "complete",
                "refuse",
            ),
        ),
        permissions=frozenset({ToolPermission.READ_KNOWLEDGE}),
        handler_name="finalize_answer",
        model_visible=True,
    )


_EMPTY_SCHEMA: dict[str, JSONValue] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {},
}

_INSPECTION_SCHEMA: dict[str, JSONValue] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "inspection_round": {"type": "integer", "minimum": 1, "maximum": 8},
    },
}


def _with_guidance_schema(
    schema: dict[str, JSONValue], *, version: str, next_actions: tuple[str, ...]
) -> dict[str, JSONValue]:
    if version != "1.1.0":
        return schema
    required = schema.get("required")
    properties = schema.get("properties")
    assert isinstance(required, list) and isinstance(properties, dict)
    return {
        **schema,
        "required": [*required, "recommended_next"],
        "properties": {
            **properties,
            "recommended_next": {"enum": list(next_actions)},
        },
    }


def _required_query(arguments: dict[str, JSONValue]) -> str:
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise NodeExecutionError(
            "SKILL_INPUT_INVALID", RunErrorCategory.INPUT, "Knowledge search query is required."
        )
    return query


def _document_arguments(arguments: Mapping[str, JSONValue]) -> tuple[str, str | None]:
    reference = arguments.get("document_reference")
    focus = arguments.get("focus")
    if (
        set(arguments) - {"document_reference", "focus"}
        or not isinstance(reference, str)
        or not reference.strip()
        or len(reference) > 280
        or (focus is not None and (not isinstance(focus, str) or len(focus) > 256))
    ):
        raise NodeExecutionError(
            "SKILL_INPUT_INVALID",
            RunErrorCategory.INPUT,
            "A bounded document name is required for document summarization.",
        )
    return reference.strip(), focus.strip() if isinstance(focus, str) and focus.strip() else None


def _document_query(reference: str, focus: str | None) -> str:
    query = f"Summarize {reference}"
    if focus:
        query += f" with focus on {focus}"
    return query[:512]


def _observation(
    query: str,
    hits: tuple[SearchHit, ...],
    *,
    scope: QARetrievalScope | None = None,
) -> _SearchObservation:
    # SearchHit is intentionally kept behind the SearchService port; this helper only projects
    # its stable identities and safe counters into the model-visible loop facts.
    source_ids = frozenset(hit.source_id for hit in hits)
    document_ids = frozenset(hit.document_id for hit in hits)
    version_ids = frozenset(hit.version_id for hit in hits)
    observed_scope = QARetrievalScope(
        source_ids=source_ids,
        document_ids=document_ids,
        version_ids=version_ids,
    )
    fixed_scope = scope or QARetrievalScope()
    return _SearchObservation(
        query=query,
        hit_count=len(hits),
        matched_count=sum(not hit.context_only for hit in hits),
        context_only_count=sum(hit.context_only for hit in hits),
        document_count=len(document_ids),
        source_count=len(source_ids),
        evidence_ids=tuple(str(hit.chunk_id) for hit in hits),
        source_versions=tuple(
            (
                str(hit.source_id),
                str(hit.document_id),
                str(hit.version_id),
            )
            for hit in hits
        ),
        scope=QARetrievalScope(
            source_ids=observed_scope.source_ids | fixed_scope.source_ids,
            document_ids=observed_scope.document_ids | fixed_scope.document_ids,
            version_ids=observed_scope.version_ids | fixed_scope.version_ids,
        ),
    )


def _needs_followup_search(facts: _RunFacts) -> bool:
    return bool(
        set(facts.gap_signals)
        & {"no_matched_evidence", "matched_evidence_limited", "single_source_coverage"}
    )


def _bounded_query(goal: str, *, followup: bool = False) -> str:
    """Create a bounded fallback query when the model tries to skip retrieval."""
    normalized = " ".join(goal.split()) or "knowledge request"
    suffix = " additional supporting evidence" if followup else ""
    limit = max(1, 512 - len(suffix))
    return f"{normalized[:limit]}{suffix}"[:512]


def _followup_query(facts: _RunFacts, goal: str) -> str:
    """Derive a follow-up from the last real topic, never from a meta Tool instruction."""
    base = facts.searches[-1].query if facts.searches else _bounded_query(goal)
    return _bounded_query(base, followup=True)


def _restore_finalization_from_checkpoint(facts: _RunFacts, state: AgentLoopState) -> None:
    """Restore the terminal QA gate after an approval resumes in a fresh Worker process."""
    if facts.finalization_ready:
        return
    for observation in reversed(state.observations):
        if observation.tool_name != "finalize_answer" or not isinstance(
            observation.model_output, dict
        ):
            continue
        output = observation.model_output
        outcome = output.get("outcome")
        if (
            output.get("ready") is True
            and output.get("publication") == "grounded_qa"
            and isinstance(outcome, str)
            and outcome in {item.value for item in QAOutcome}
        ):
            facts.finalization_ready = True
            facts.verified = True
        return


def _terminal_action(completed: QARunRecord) -> LLMDecisionAction:
    assert completed.result is not None
    outcome = completed.result.outcome
    return (
        LLMDecisionAction.REFUSE
        if outcome in {QAOutcome.REFUSE, QAOutcome.CONFLICT}
        else LLMDecisionAction.COMPLETE
    )


_META_SEARCH_QUERY = re.compile(
    r"(?:knowledge_search|knowledge search|additional supporting evidence|"
    r"通过.+查询|use.+search)",
    re.IGNORECASE,
)

_CURRENT_QA_ANSWER_MARKER = "{{current_grounded_qa_answer}}"


def _is_meta_search_query(query: str) -> bool:
    return bool(_META_SEARCH_QUERY.search(query))


def _qa_result_markdown(qa_run: QARunRecord) -> str | None:
    result = qa_run.result
    if result is None:
        return None
    if result.answer is not None:
        return result.answer.text
    if result.refusal is not None:
        return result.refusal.message
    if result.conflict is not None:
        return result.conflict.message
    return None


def _valid_search_arguments(arguments: Mapping[str, JSONValue]) -> bool:
    query = arguments.get("query")
    return (
        set(arguments) == {"query"}
        and isinstance(query, str)
        and bool(query.strip())
        and len(query) <= 512
    )


def _valid_document_summary_arguments(
    arguments: Mapping[str, JSONValue], required_reference: str
) -> bool:
    reference = arguments.get("document_reference")
    focus = arguments.get("focus")
    return (
        set(arguments) <= {"document_reference", "focus"}
        and isinstance(reference, str)
        and reference.strip() == required_reference
        and (focus is None or isinstance(focus, str))
    )


_DOCUMENT_REFERENCE_PATTERN = re.compile(
    r"(?<![\w.-])([\w][\w.-]{0,255}\.(?:md|markdown|txt|pdf|docx?))(?![\w-]|\.(?=\w))",
    re.IGNORECASE,
)
_DOCUMENT_SUMMARY_INTENT_PATTERN = re.compile(
    r"\u6458\u8981|\u603b\u7ed3|\u6982\u8ff0|\u56de\u987e|\u6c47\u603b|"
    r"summari[sz]e|summary|recap",
    re.IGNORECASE,
)


def _explicit_document_summary_reference(goal: str) -> str | None:
    """Find a named file only when nearby request text explicitly asks for a summary."""
    for match in _DOCUMENT_REFERENCE_PATTERN.finditer(goal):
        start = max(0, match.start() - 120)
        end = min(len(goal), match.end() + 120)
        if _DOCUMENT_SUMMARY_INTENT_PATTERN.search(goal[start:end]):
            return match.group(1)
    return None


def _require_empty(arguments: dict[str, JSONValue]) -> None:
    if arguments:
        raise NodeExecutionError(
            "SKILL_INPUT_INVALID", RunErrorCategory.INPUT, "Knowledge Tool does not accept input."
        )


def _require_inspection(arguments: dict[str, JSONValue]) -> None:
    round_number = arguments.get("inspection_round")
    if round_number is None and not arguments:
        return
    if (
        set(arguments) != {"inspection_round"}
        or not isinstance(round_number, int)
        or isinstance(round_number, bool)
        or not 1 <= round_number <= 8
    ):
        raise NodeExecutionError(
            "SKILL_INPUT_INVALID",
            RunErrorCategory.INPUT,
            "Knowledge inspection round is invalid.",
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
