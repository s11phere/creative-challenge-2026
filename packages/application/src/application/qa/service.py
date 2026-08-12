"""The single provisional application path for bounded Grounded QA runs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from time import perf_counter
from typing import Protocol
from uuid import UUID, uuid4

from domain.grounded_qa import (
    QAAttempt,
    QAContractError,
    QAError,
    QAErrorCode,
    QAEvent,
    QAOutcome,
    QAResult,
    QAStatus,
    QuestionInput,
    QuestionType,
    Refusal,
    RefusalReason,
    normalize_question,
)
from domain.qa_persistence import (
    CitationRecord,
    ConversationRecord,
    EvidenceRecord,
    GroundedQARepository,
    MessageRecord,
    MessageRole,
    QAPhase,
    QAPhaseTiming,
    QARetrievalScope,
    QARunRecord,
    QARunUsage,
    QARunVersions,
)
from domain.retrieval import RetrievalError, RetrievalProfileV1, SearchFilters, SearchRequest

from .answer_mode import GroundedAnswerMode
from .context_builder import ContextBuilder, ConversationRole, ConversationTurn
from .evidence import EvidenceBindingService
from .generation import GenerationResult, GroundedAnswerGenerator
from .profile import QAPlanningProfileV1
from .query_planning import (
    MergedSearchResult,
    QASearchCoordinator,
    QueryPlanner,
    QueryPlanningResult,
)


@dataclass(frozen=True)
class GroundedQAExecutionProfile:
    """Trusted server-side profile inputs pinned before a run is executed."""

    planning: QAPlanningProfileV1
    retrieval: RetrievalProfileV1


@dataclass(frozen=True)
class AgentRetrievalPlan:
    """Model-selected retrieval preferences constrained by the trusted QA profile."""

    additional_queries: tuple[str, ...] = ()
    max_evidence_items: int | None = None
    max_input_tokens: int | None = None
    max_tokens_per_evidence: int | None = None
    max_evidence_per_source: int | None = None
    max_chunks_per_document: int | None = None
    answer_mode: GroundedAnswerMode = GroundedAnswerMode.DEFAULT

    def apply(self, profile: QAPlanningProfileV1) -> QAPlanningProfileV1:
        values = {
            field: value
            for field, value in (
                ("max_evidence_items", self.max_evidence_items),
                ("max_input_tokens", self.max_input_tokens),
                ("max_tokens_per_evidence", self.max_tokens_per_evidence),
                ("max_evidence_per_source", self.max_evidence_per_source),
                ("max_chunks_per_document", self.max_chunks_per_document),
            )
            if value is not None
        }
        for field, value in values.items():
            assert value is not None
            if value < 1:
                raise QAContractError(f"Agent retrieval {field} must be positive")
        if len(self.additional_queries) > profile.max_subqueries - 1:
            raise QAContractError("Agent submitted too many additional retrieval queries")
        return replace(
            profile,
            max_evidence_items=_clamp(
                values.get("max_evidence_items"), 1, profile.max_evidence_items
            ),
            max_input_tokens=_clamp(values.get("max_input_tokens"), 4096, profile.max_input_tokens),
            max_tokens_per_evidence=_clamp(
                values.get("max_tokens_per_evidence"), 256, profile.max_tokens_per_evidence
            ),
            max_evidence_per_source=_clamp(
                values.get("max_evidence_per_source"), 1, profile.max_evidence_per_source
            ),
            max_chunks_per_document=_clamp(
                values.get("max_chunks_per_document"), 1, profile.max_chunks_per_document
            ),
        )


class GroundedQAApplicationPort(Protocol):
    """Entry point shared by future HTTP, Worker, evaluation, and Skill adapters."""

    async def create_conversation(self, conversation: ConversationRecord) -> ConversationRecord: ...

    async def submit(self, question: QuestionInput, *, versions: QARunVersions) -> QARunRecord: ...

    async def execute(
        self,
        run_id: UUID,
        *,
        profile: GroundedQAExecutionProfile,
        agent_plan: AgentRetrievalPlan | None = None,
    ) -> QARunRecord: ...

    async def inspect_retrieval(
        self, run_id: UUID, *, profile: GroundedQAExecutionProfile, agent_plan: AgentRetrievalPlan
    ) -> MergedSearchResult: ...

    async def request_cancel(self, run_id: UUID) -> QARunRecord: ...


class GroundedQAService:
    """Orchestrate the provisional QA workflow without transport or storage coupling."""

    def __init__(
        self,
        *,
        repository: GroundedQARepository,
        planner: QueryPlanner,
        search: QASearchCoordinator,
        evidence_binding: EvidenceBindingService,
        context_builder: ContextBuilder,
        generator: GroundedAnswerGenerator,
    ) -> None:
        self._repository = repository
        self._planner = planner
        self._search = search
        self._evidence_binding = evidence_binding
        self._context_builder = context_builder
        self._generator = generator
        # Per-run planning cache: an agent flow that inspects retrieval and then
        # runs grounded_qa on the same run should not re-run the rewrite LLM.
        # Guarded against unbounded growth by clearing entries when the run
        # reaches a terminal state and by capping the total size.
        self._plan_cache: dict[UUID, QueryPlanningResult] = {}
        self._plan_cache_cap = 64

    async def create_conversation(self, conversation: ConversationRecord) -> ConversationRecord:
        return await self._repository.create_conversation(conversation)

    async def submit(self, question: QuestionInput, *, versions: QARunVersions) -> QARunRecord:
        if question.conversation_id is None or question.idempotency_key is None:
            raise QAContractError("QA submission requires a conversation and idempotency key")
        conversation = await self._repository.get_conversation(question.conversation_id)
        if conversation is None:
            raise QAContractError("QA conversation does not exist")
        if (
            conversation.space_id != question.space_id
            or conversation.owner_id != question.caller_id
        ):
            raise QAContractError("QA question does not belong to the conversation owner and Space")

        message = await self._repository.append_message(
            MessageRecord(
                conversation_id=conversation.conversation_id,
                space_id=question.space_id,
                role=MessageRole.USER,
                content=question.question,
                idempotency_key=question.idempotency_key,
            )
        )
        run_id = uuid4()
        created = await self._repository.create_run(
            QARunRecord(
                run_id=run_id,
                attempt=QAAttempt(run_id=run_id),
                conversation_id=conversation.conversation_id,
                question_message_id=message.message_id,
                space_id=question.space_id,
                caller_id=question.caller_id,
                idempotency_key=question.idempotency_key,
                versions=versions,
                retrieval_scope=QARetrievalScope(
                    source_ids=question.source_ids,
                    document_ids=question.document_ids,
                    version_ids=question.version_ids,
                ),
            )
        )
        if created.status is QAStatus.CREATED:
            return await self._repository.transition_run(created.run_id, QAEvent.QUEUE)
        return created

    async def execute(
        self,
        run_id: UUID,
        *,
        profile: GroundedQAExecutionProfile,
        agent_plan: AgentRetrievalPlan | None = None,
    ) -> QARunRecord:
        run = await self._require_run(run_id)
        if run.status in _TERMINAL_STATUSES:
            return run
        if run.status is QAStatus.CANCEL_REQUESTED:
            return await self._repository.transition_run(run_id, QAEvent.CANCEL)
        if run.status is not QAStatus.QUEUED:
            return run
        if run.versions.profile_version != profile.planning.profile_id:
            return await self._fail(
                run_id, QAError(QAErrorCode.INVALID_INPUT, "QA profile mismatch.")
            )
        if run.versions.retrieval_profile_version != profile.retrieval.profile_version:
            return await self._fail(
                run_id, QAError(QAErrorCode.INVALID_INPUT, "Retrieval profile mismatch.")
            )

        timings: list[QAPhaseTiming] = []
        try:
            run = await self._repository.transition_run(run_id, QAEvent.START)
            question = await self._question_for_run(run)

            started = perf_counter()
            planning, effective_planning = await self._planning_for(
                run_id, question, profile, agent_plan
            )
            timings.append(QAPhaseTiming(QAPhase.PLANNING, _elapsed_ms(started)))

            started = perf_counter()
            merged = await self._search_for(run, question, planning, profile, effective_planning)
            timings.append(QAPhaseTiming(QAPhase.RETRIEVAL, _elapsed_ms(started)))

            bound = self._evidence_binding.bind(space_id=run.space_id, hits=merged.hits)
            for item in bound:
                await self._repository.save_evidence(
                    EvidenceRecord(
                        run_id=run.run_id,
                        attempt_id=run.attempt.attempt_id,
                        candidate=item.candidate,
                    )
                )
            context = self._context_builder.build(
                question=question,
                history=await self._history_for_run(run),
                evidence=bound,
                profile=effective_planning,
            )
            run = await self._repository.transition_run(run_id, QAEvent.VERIFY)

            started = perf_counter()
            generated = await self._generator.generate(
                question=question,
                context=context,
                answer_mode=(
                    agent_plan.answer_mode if agent_plan is not None else GroundedAnswerMode.DEFAULT
                ),
            )
            answer_mode = (
                agent_plan.answer_mode if agent_plan is not None else GroundedAnswerMode.DEFAULT
            )
            if answer_mode is GroundedAnswerMode.DEFAULT:
                generated = _enforce_question_type_grounding(generated, planning.plan.question_type)
            generated = _enforce_answer_mode_grounding(
                generated,
                answer_mode,
            )
            timings.append(QAPhaseTiming(QAPhase.GENERATION, _elapsed_ms(started)))
            await self._ensure_not_cancelled(run_id)

            usage = _usage(generated, timings)
            await self._repository.save_usage(run_id, usage)
            answer_message = MessageRecord(
                conversation_id=run.conversation_id,
                space_id=run.space_id,
                role=MessageRole.ASSISTANT,
                content=_result_content(generated.result),
                run_id=run_id,
            )
            published = await self._repository.publish_terminal(
                run_id=run_id,
                result=generated.result,
                answer_message=answer_message,
                citations=_citation_records(run, generated.result, answer_message.message_id),
            )
            self._plan_cache.pop(run_id, None)
            return published
        except RetrievalError as error:
            return await self._fail(
                run_id,
                QAError(
                    QAErrorCode.RETRIEVAL_FAILED,
                    "QA retrieval is unavailable.",
                    retryable=error.retryable,
                ),
            )
        except QAError as error:
            return await self._fail(run_id, error)
        except QAContractError:
            return await self._fail(
                run_id,
                QAError(QAErrorCode.INVALID_INPUT, "QA execution violated its active contract."),
            )

    async def inspect_retrieval(
        self,
        run_id: UUID,
        *,
        profile: GroundedQAExecutionProfile,
        agent_plan: AgentRetrievalPlan,
    ) -> MergedSearchResult:
        run = await self._require_run(run_id)
        if run.status is not QAStatus.QUEUED:
            raise QAContractError("Retrieval inspection requires a queued QA Run")
        question = await self._question_for_run(run)
        planning, effective_planning = await self._planning_for(
            run_id, question, profile, agent_plan
        )
        return await self._search_for(run, question, planning, profile, effective_planning)

    async def _planning_for(
        self,
        run_id: UUID,
        question: QuestionInput,
        profile: GroundedQAExecutionProfile,
        agent_plan: AgentRetrievalPlan | None,
    ) -> tuple[QueryPlanningResult, QAPlanningProfileV1]:
        effective = (
            agent_plan.apply(profile.planning) if agent_plan is not None else profile.planning
        )
        planner_profile = effective
        agent_queries = () if agent_plan is None else agent_plan.additional_queries
        if agent_queries:
            # Agent-driven retrieval: the agent already chose its sub-queries, so
            # the generic rewrite LLM call is redundant. The rewrite stays
            # question-only (R4-04); only the planner budget is skipped here.
            planner_profile = replace(
                effective,
                max_subqueries=max(1, effective.max_subqueries - len(agent_queries)),
                rewrite_enabled=False,
            )
        cached = self._plan_cache.get(run_id)
        if (
            cached is not None
            and not agent_queries
            and (cached.plan.original_question == question.question)
        ):
            # Reuse the plan an earlier inspect_retrieval produced for this run
            # instead of re-running the rewrite LLM (and the duplicate retrieval).
            planning = cached
        else:
            planning = await self._planner.plan(question, planner_profile)
            if cached is None and not agent_queries:
                self._plan_cache[run_id] = planning
                if len(self._plan_cache) > self._plan_cache_cap:
                    self._plan_cache.clear()
        if not agent_queries:
            return planning, effective
        queries = _merge_queries(
            planning.plan.queries,
            agent_queries,
            max_queries=effective.max_subqueries,
        )
        plan = replace(
            planning.plan,
            queries=queries,
            rewrite_applied=len(queries) > 1,
            fallback_reason=None,
        )
        diagnostic = replace(
            planning.diagnostic,
            rewrite_applied=len(queries) > 1,
            fallback_reason=None,
            query_count=len(queries),
        )
        return replace(planning, plan=plan, diagnostic=diagnostic), effective

    async def _search_for(
        self,
        run: QARunRecord,
        question: QuestionInput,
        planning: QueryPlanningResult,
        profile: GroundedQAExecutionProfile,
        effective_planning: QAPlanningProfileV1,
    ) -> MergedSearchResult:
        return await self._search.search(
            base_request=SearchRequest(
                query=question.question,
                space_id=run.space_id,
                filters=SearchFilters(
                    source_ids=run.retrieval_scope.source_ids,
                    document_ids=run.retrieval_scope.document_ids,
                    version_ids=run.retrieval_scope.version_ids,
                ),
            ),
            plan=planning.plan,
            profile=profile.retrieval,
            limit=effective_planning.max_evidence_items,
        )

    async def request_cancel(self, run_id: UUID) -> QARunRecord:
        return await self._repository.request_cancel(run_id)

    async def _question_for_run(self, run: QARunRecord) -> QuestionInput:
        message = await self._repository.get_message(run.question_message_id)
        if message is None or message.role is not MessageRole.USER:
            raise QAContractError("QA run question is unavailable")
        return QuestionInput(
            question=run.standalone_request or message.content,
            space_id=run.space_id,
            caller_id=run.caller_id,
            conversation_id=run.conversation_id,
            idempotency_key=run.idempotency_key,
        )

    async def _history_for_run(self, run: QARunRecord) -> tuple[ConversationTurn, ...]:
        if run.standalone_request is not None:
            # Assistant context is already bounded into the standalone request. QA receives
            # only evidence and the task-specific request, never a second full chat history.
            return ()
        messages = await self._repository.list_messages(run.conversation_id)
        turns: list[ConversationTurn] = []
        for message in messages:
            if message.message_id == run.question_message_id:
                break
            role = (
                ConversationRole.USER
                if message.role is MessageRole.USER
                else ConversationRole.ASSISTANT
            )
            turns.append(ConversationTurn(role, message.content))
        return tuple(turns)

    async def _ensure_not_cancelled(self, run_id: UUID) -> None:
        run = await self._require_run(run_id)
        if run.cancellation_requested or run.status is QAStatus.CANCEL_REQUESTED:
            raise QAError(QAErrorCode.CANCELLED, "QA run was cancelled.")

    async def _fail(self, run_id: UUID, error: QAError) -> QARunRecord:
        self._plan_cache.pop(run_id, None)
        run = await self._require_run(run_id)
        if run.status in _TERMINAL_STATUSES:
            return run
        if run.status is QAStatus.CANCEL_REQUESTED:
            return await self._repository.transition_run(run_id, QAEvent.CANCEL)
        if error.code is QAErrorCode.CANCELLED:
            requested = await self._repository.request_cancel(run_id)
            if requested.status is QAStatus.CANCEL_REQUESTED:
                return await self._repository.transition_run(run_id, QAEvent.CANCEL)
            return requested
        if error.code in {QAErrorCode.TIMEOUT, QAErrorCode.TIMED_OUT}:
            return await self._repository.transition_run(run_id, QAEvent.TIMEOUT)
        return await self._repository.transition_run(
            run_id, QAEvent.FAIL, error_code=error.code.value
        )

    async def _require_run(self, run_id: UUID) -> QARunRecord:
        run = await self._repository.get_run(run_id)
        if run is None:
            raise QAContractError("QA run does not exist")
        return run


_TERMINAL_STATUSES = frozenset(
    {QAStatus.COMPLETED, QAStatus.REFUSED, QAStatus.FAILED, QAStatus.CANCELLED, QAStatus.TIMED_OUT}
)


def _elapsed_ms(started: float) -> float:
    return max(0.0, (perf_counter() - started) * 1000)


def _enforce_question_type_grounding(
    generated: GenerationResult, question_type: QuestionType
) -> GenerationResult:
    result = generated.result
    if (
        question_type is QuestionType.COMPARISON
        and result.outcome is QAOutcome.ANSWER
        and result.answer is not None
        and len({citation.source_id for citation in result.answer.citations}) < 2
    ):
        return replace(
            generated,
            result=QAResult(
                QAOutcome.REFUSE,
                refusal=Refusal(
                    RefusalReason.INSUFFICIENT_EVIDENCE,
                    "A comparison requires grounded evidence from at least two selected sources.",
                ),
            ),
        )
    return generated


def _enforce_answer_mode_grounding(
    generated: GenerationResult, answer_mode: GroundedAnswerMode
) -> GenerationResult:
    """Fail closed before publication when a Research format lacks source coverage."""
    if answer_mode is not GroundedAnswerMode.RESEARCH_LITERATURE_REVIEW:
        return generated
    answer = generated.result.answer
    # One upload connector can own several papers, so paper coverage is represented by
    # distinct pinned documents rather than connector/source rows.
    if answer is not None and len({citation.document_id for citation in answer.citations}) >= 2:
        return generated
    return replace(
        generated,
        result=QAResult(
            outcome=QAOutcome.REFUSE,
            refusal=Refusal(
                RefusalReason.INSUFFICIENT_EVIDENCE,
                "A literature review requires cited evidence from at least two selected papers.",
            ),
        ),
    )


def _usage(generated: GenerationResult, timings: list[QAPhaseTiming]) -> QARunUsage:
    return QARunUsage(
        input_tokens=generated.usage.input_tokens,
        output_tokens=generated.usage.output_tokens,
        model_calls=generated.usage.model_calls,
        repair_attempts=generated.usage.repair_attempts,
        model_latency_ms=generated.usage.model_latency_ms,
        phase_timings=tuple(timings),
    )


def _result_content(result: QAResult) -> str:
    if result.answer is not None:
        return result.answer.text
    if result.refusal is not None:
        return result.refusal.message
    if result.conflict is not None:
        return result.conflict.message
    raise QAContractError("Infrastructure failures cannot create assistant messages")


def _clamp(value: int | None, lower: int, upper: int) -> int:
    if value is None:
        return upper
    return min(max(value, lower), upper)


def _merge_queries(
    planned: tuple[str, ...], additional: tuple[str, ...], *, max_queries: int
) -> tuple[str, ...]:
    queries = list(planned)
    seen = {normalize_question(query) for query in queries}
    for query in additional:
        normalized = normalize_question(query)
        if normalized in seen:
            continue
        if len(queries) >= max_queries:
            break
        queries.append(normalized)
        seen.add(normalized)
    return tuple(queries)


def _citation_records(
    run: QARunRecord, result: QAResult, message_id: UUID
) -> tuple[CitationRecord, ...]:
    if result.answer is None:
        return ()
    return tuple(
        CitationRecord(
            run_id=run.run_id,
            attempt_id=run.attempt.attempt_id,
            message_id=message_id,
            citation=citation,
        )
        for citation in result.answer.citations
    )


__all__ = [
    "GroundedQAApplicationPort",
    "GroundedQAExecutionProfile",
    "GroundedQAService",
]
