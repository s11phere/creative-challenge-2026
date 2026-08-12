"""Product-level autonomous Assistant Loop over trusted Skill Tool adapters."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from importlib.resources import files
from typing import Protocol, cast
from uuid import UUID, uuid4

from agent_runtime import (
    AgentLoopDebugTrace,
    AgentLoopExecutor,
    AgentToolRegistry,
    JSONValue,
    LLMDecision,
    LLMDecisionAction,
    LLMDecisionError,
    NodeExecutionError,
    ToolRef,
)
from agent_runtime.skills import PinnedSkill
from domain.agent_loop import AgentLoopState, AgentLoopTask
from domain.agent_runtime import (
    AgentRun,
    AgentRunContext,
    ApprovalPort,
    RunBudget,
    RunErrorCategory,
    RunStatus,
    RuntimeStateStore,
    ToolPermission,
)
from domain.agent_sse import AgentRunEventStore
from domain.assistant_sse import AssistantEventStore, AssistantEventType
from domain.conversation_run import (
    Clarification,
    ClarificationKind,
    ConversationRun,
    ConversationRunKind,
    ConversationRunRepository,
    ConversationRunStatus,
    ConversationRunUsage,
)
from domain.grounded_qa import QAStatus
from domain.qa_persistence import MessageRecord, MessageRole, QARunRecord
from model_gateway import ModelGateway

from .context import ConversationContextService
from .finalization import ConversationFinalizer
from .metrics import AssistantMetrics

_CONTRACT_ROOT = files("application.assistant").joinpath("contracts")
_BASE_PROMPT_V7 = _CONTRACT_ROOT.joinpath("base-system-prompt-v7.txt").read_text(encoding="utf-8")

type QARunReader = Callable[[UUID], Awaitable[QARunRecord | None]]
type DecisionPolicy = Callable[[AgentRun, AgentLoopState, LLMDecision], LLMDecision]


class AssistantLoopMessageReader(Protocol):
    async def get_message(self, message_id: UUID) -> MessageRecord | None: ...


@dataclass(frozen=True)
class AssistantSkillContext:
    """Trusted, version-pinned Skill instructions available to this outer Loop."""

    name: str
    version: str
    description: str
    instructions: str

    def __post_init__(self) -> None:
        if not all((self.name, self.version, self.description, self.instructions.strip())):
            raise ValueError("Assistant Skill context is incomplete")


class AssistantConversationLoopFinalizer:
    """Publish one direct response or the already-published grounded QA message."""

    def __init__(
        self,
        *,
        runs: ConversationRunRepository,
        qa_results: QARunReader,
        model_identity: str,
        conversation_finalizer: ConversationFinalizer | None = None,
    ) -> None:
        self._runs = runs
        self._qa_results = qa_results
        self._conversation_finalizer = conversation_finalizer
        self._model_identity = model_identity

    async def finalize(
        self,
        *,
        run: AgentRun,
        task: AgentLoopTask,
        decision: LLMDecision,
        state: AgentLoopState,
        input_data: Mapping[str, JSONValue],
    ) -> dict[str, JSONValue]:
        del task, input_data
        parent = await self._runs.get_conversation_run(run.context.run_id)
        if parent is None:
            raise NodeExecutionError(
                "RUN_ASSISTANT_PARENT_MISSING",
                RunErrorCategory.INTERNAL,
                "Assistant parent Run is unavailable.",
            )
        if parent.status in {
            ConversationRunStatus.COMPLETED,
            ConversationRunStatus.REFUSED,
            ConversationRunStatus.FAILED,
            ConversationRunStatus.CANCELLED,
            ConversationRunStatus.TIMED_OUT,
        }:
            return {"status": parent.status.value, "publication": "existing"}
        usage = _combined_usage(parent, run)
        if _grounded_finalization_ready(state):
            qa_run = await self._qa_results(run.context.run_id)
            if (
                qa_run is None
                or qa_run.status not in {QAStatus.COMPLETED, QAStatus.REFUSED}
                or qa_run.answer_message_id is None
            ):
                raise NodeExecutionError(
                    "RUN_KNOWLEDGE_FINALIZATION_REQUIRED",
                    RunErrorCategory.SCHEMA,
                    "Grounded QA finalization is unavailable.",
                )
            published = await self._runs.publish_existing_skill_result(
                run_id=parent.run_id,
                message_id=qa_run.answer_message_id,
                usage=usage,
                model_identity=self._model_identity,
                refused=qa_run.status is QAStatus.REFUSED,
            )
            return {"status": published.status.value, "publication": "grounded_qa"}
        if decision.action.value == "clarify":
            clarified = await self._runs.publish_clarification(
                run_id=parent.run_id,
                clarification=Clarification(
                    clarification_id=f"clarify:{parent.run_id.hex}",
                    kind=ClarificationKind.INPUT_REQUIRED,
                    message=_clarification_message(decision, state),
                ),
                usage=usage,
                model_identity=self._model_identity,
            )
            return {"status": clarified.status.value, "publication": "clarification"}
        refused = decision.action.value == "refuse"
        final_response = decision.final_response or (
            "I cannot help with that request." if refused else None
        )
        if not final_response:
            raise NodeExecutionError(
                "RUN_ASSISTANT_FINAL_RESPONSE_MISSING",
                RunErrorCategory.SCHEMA,
                "Assistant terminal response is missing.",
            )
        published = await self._runs.publish_direct_message(
            run_id=parent.run_id,
            message=MessageRecord(
                message_id=uuid4(),
                conversation_id=parent.conversation_id,
                space_id=parent.space_id,
                role=MessageRole.ASSISTANT,
                content=final_response,
                run_id=parent.run_id,
            ),
            usage=usage,
            model_identity=self._model_identity,
            refused=refused,
        )
        return {
            "status": published.status.value,
            "publication": "refusal" if refused else "direct",
        }


class AutonomousAssistantLoopService:
    """Run a direct conversation as one model-directed Skill/Tool loop."""

    def __init__(
        self,
        *,
        runs: ConversationRunRepository,
        messages: AssistantLoopMessageReader,
        gateway: ModelGateway,
        events: AssistantEventStore,
        agent_events: AgentRunEventStore,
        runtime_state: RuntimeStateStore,
        pin: PinnedSkill,
        budget: RunBudget,
        tool_registry: AgentToolRegistry,
        allowed_tools: tuple[ToolRef, ...],
        qa_results: QARunReader,
        skill_contexts: tuple[AssistantSkillContext, ...],
        context: ConversationContextService | None = None,
        metrics: AssistantMetrics | None = None,
        conversation_finalizer: ConversationFinalizer | None = None,
        decision_policy: DecisionPolicy | None = None,
        tool_skill_refs: Mapping[ToolRef, ToolRef] | None = None,
        workspace_context: Mapping[str, JSONValue] | None = None,
        additional_permissions: frozenset[ToolPermission] = frozenset(),
        approval_port: ApprovalPort | None = None,
        debug_trace: AgentLoopDebugTrace | None = None,
    ) -> None:
        self._runs = runs
        self._messages = messages
        self._gateway = gateway
        self._events = events
        self._agent_events = agent_events
        self._runtime_state = runtime_state
        self._pin = pin
        self._budget = budget
        self._tool_registry = tool_registry
        self._allowed_tools = allowed_tools
        self._qa_results = qa_results
        self._conversation_finalizer = conversation_finalizer or ConversationFinalizer(
            runs=runs, gateway=gateway
        )
        self._skill_contexts = skill_contexts
        self._context = context
        self._metrics = metrics
        self._decision_policy = decision_policy
        self._tool_skill_refs = dict(tool_skill_refs or {})
        self._workspace_context = dict(workspace_context or {})
        self._additional_permissions = additional_permissions
        self._approval_port = approval_port
        self._debug_trace = debug_trace

    async def execute(self, run_id: UUID, *, trace_id: str) -> ConversationRun | None:
        parent = await self._runs.get_conversation_run(run_id)
        if parent is None:
            return None
        if parent.run_kind is not ConversationRunKind.ASSISTANT_TURN:
            raise ValueError("RUN_AGENT_DECISION_INVALID")
        if parent.status in {
            ConversationRunStatus.COMPLETED,
            ConversationRunStatus.REFUSED,
            ConversationRunStatus.FAILED,
            ConversationRunStatus.CANCELLED,
            ConversationRunStatus.TIMED_OUT,
        }:
            await self._emit_terminal(parent)
            return parent
        if parent.cancellation_requested:
            cancelled = await self._runs.cancel_conversation_run(run_id)
            await self._emit_terminal(cancelled)
            return cancelled
        user_message = await self._messages.get_message(parent.user_message_id)
        if (
            user_message is None
            or user_message.role is not MessageRole.USER
            or user_message.conversation_id != parent.conversation_id
            or user_message.space_id != parent.space_id
        ):
            return await self._fail(run_id, "RUN_AGENT_DECISION_INVALID")
        snapshot = await self._context.snapshot(parent) if self._context is not None else None
        input_data: dict[str, JSONValue] = {
            "question": user_message.content,
            "conversation": cast(
                JSONValue,
                snapshot.decision_request() if snapshot is not None else user_message.content,
            ),
            "workspace": cast(JSONValue, self._workspace_context),
        }
        await self._events.append(
            run_id,
            AssistantEventType.ROUTING,
            {"status": ConversationRunStatus.RUNNING.value, "action": "agent_loop"},
        )
        finalizer = AssistantConversationLoopFinalizer(
            runs=self._runs,
            qa_results=self._qa_results,
            conversation_finalizer=self._conversation_finalizer,
            model_identity=self._gateway.status.provider.value,
        )
        executor = AgentLoopExecutor(
            tool_registry=self._tool_registry,
            allowed_tools=self._allowed_tools,
            system_prompt=self._system_prompt(),
            model_gateway=self._gateway,
            state_store=self._runtime_state,
            event_store=self._agent_events,
            reasoning_profile=parent.reasoning_profile,
            finalizer=finalizer,
            cancellation_check=self._cancel_requested,
            decision_policy=(
                lambda runtime, state, decision: self._apply_decision_policy(
                    runtime, state, decision, input_data
                )
            ),
            invalid_decision_recovery=(
                lambda runtime, state, error: self._recover_workspace_artifact_decision(
                    runtime, state, error, input_data
                )
            ),
            escalate_long_answer=True,
            tool_skill_refs=self._tool_skill_refs,
            approval_request=(
                self._approval_port.request if self._approval_port is not None else None
            ),
            approval_port=self._approval_port,
            debug_trace=self._debug_trace,
        )
        persisted = await self._runtime_state.get_run(run_id)
        checkpoint = await self._runtime_state.get_latest(run_id)
        if (
            persisted is not None
            and checkpoint is not None
            and persisted.status
            not in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.TIMED_OUT}
        ):
            approval_id = None
            loop_state = AgentLoopState.from_checkpoint(
                cast(dict[str, object], dict(checkpoint.state))
            )
            if (
                loop_state.approval_id is not None
                and self._approval_port is not None
                and await self._approval_port.is_approved(loop_state.approval_id, persisted.context)
            ):
                approval_id = loop_state.approval_id
            result = await executor.resume(
                persisted,
                self._pin,
                checkpoint,
                input_data,
                caller_id=parent.caller_id,
                space_id=parent.space_id,
                approval_id=approval_id,
            )
        else:
            runtime = AgentRun(
                context=AgentRunContext(
                    run_id=parent.run_id,
                    space_id=parent.space_id,
                    skill_name=self._pin.name,
                    skill_version=self._pin.version,
                    skill_content_sha256=self._pin.content_sha256,
                    trace_id=trace_id,
                    caller_id=parent.caller_id,
                    granted_permissions=frozenset(
                        {ToolPermission.READ_KNOWLEDGE, ToolPermission.MODEL}
                    ).union(self._additional_permissions),
                ),
                budget=self._budget,
            )
            result = await executor.execute(
                runtime,
                self._pin,
                input_data,
                goal=user_message.content,
            )
        if result.error is not None:
            unavailable = await self._publish_unavailable_workspace_artifact_response(
                run_id=run_id,
                runtime=result.run,
                request_texts=(user_message.content, input_data.get("conversation")),
                error_code=result.error.code,
                error_message=result.error.message,
            )
            if unavailable is not None:
                return unavailable
            failed = await self._fail(run_id, result.error.code)
            return failed
        if result.waiting_approval:
            return await self._runs.wait_for_approval(run_id)
        completed = await self._runs.get_conversation_run(run_id)
        if completed is None:
            return await self._fail(run_id, "RUN_ASSISTANT_PARENT_MISSING")
        if self._metrics is not None:
            self._metrics.record_usage(
                run_kind=completed.run_kind.value,
                input_tokens=result.run.usage.input_tokens,
                output_tokens=result.run.usage.output_tokens,
                latency_ms=0.0,
            )
        await self._emit_terminal(completed)
        return completed

    async def _cancel_requested(self, runtime: AgentRun) -> bool:
        parent = await self._runs.get_conversation_run(runtime.context.run_id)
        return parent is None or parent.cancellation_requested

    def _apply_decision_policy(
        self,
        run: AgentRun,
        state: AgentLoopState,
        decision: LLMDecision,
        input_data: Mapping[str, JSONValue],
    ) -> LLMDecision:
        """Apply Skill guidance, then enforce completion postconditions for this request.

        The model still chooses the order of useful Tools. Once the QA finalization signal is
        observed, however, an artifact request cannot terminate before a successful write, and a
        QA-only request cannot keep selecting unrelated Tools indefinitely.
        """
        if self._decision_policy is not None:
            decision = self._decision_policy(run, state, decision)
        if not _qa_finalization_observed(state):
            return decision
        request_texts = (state.task.goal, input_data.get("conversation"))
        artifact_requested = _workspace_artifact_requested(request_texts)
        if artifact_requested and self._workspace_context.get("tools_enabled") is True:
            if _workspace_write_observed(state):
                if decision.action is LLMDecisionAction.CALL_TOOL:
                    return LLMDecision(
                        action=LLMDecisionAction.COMPLETE,
                        reason="All requested QA and workspace artifact actions are complete.",
                    )
                return decision
            if decision.action is LLMDecisionAction.CALL_TOOL and decision.tool_name == "fs_write":
                return decision
            return _next_workspace_artifact_decision(state, state.task.goal)
        if not artifact_requested and decision.action is LLMDecisionAction.CALL_TOOL:
            return LLMDecision(
                action=LLMDecisionAction.COMPLETE,
                reason="QA finalization is complete and no requested follow-up action remains.",
            )
        return decision

    def _recover_workspace_artifact_decision(
        self,
        run: AgentRun,
        state: AgentLoopState,
        error: LLMDecisionError,
        input_data: Mapping[str, JSONValue],
    ) -> LLMDecision | None:
        """Recover only an invalid Tool choice that blocks a known required artifact."""
        del run
        if error.message != "Model selected a Tool outside the server allowlist.":
            return None
        workspace_tools_enabled = self._workspace_context.get("tools_enabled") is True
        if not _qa_finalization_observed(state):
            return None
        artifact_requested = _workspace_artifact_requested(
            (state.task.goal, input_data.get("conversation"))
        )
        if not artifact_requested:
            return LLMDecision(
                action=LLMDecisionAction.COMPLETE,
                reason="QA finalization is complete and no requested workspace artifact remains.",
            )
        if _workspace_write_observed(state):
            return LLMDecision(
                action=LLMDecisionAction.COMPLETE,
                reason="All requested QA and workspace artifact actions are complete.",
            )
        if not workspace_tools_enabled:
            return None
        return _next_workspace_artifact_decision(state, state.task.goal)

    async def _publish_unavailable_workspace_artifact_response(
        self,
        *,
        run_id: UUID,
        runtime: AgentRun,
        request_texts: tuple[object, ...],
        error_code: str,
        error_message: str,
    ) -> ConversationRun | None:
        """Turn one accidental unavailable-Tool decision into a useful terminal reply.

        The Tool registry remains the authority: this path never executes or registers a
        missing Tool. It only prevents a model/schema mismatch from leaving a user-facing
        artifact request as an opaque ``RUN_LLM_DECISION_INVALID`` failure.
        """
        if error_code != "RUN_LLM_DECISION_INVALID":
            return None
        if error_message != "Model selected a Tool outside the server allowlist.":
            return None
        if self._workspace_context.get("selected") is not True:
            return None
        if self._workspace_context.get("tools_enabled") is True:
            return None
        if not _workspace_artifact_requested(request_texts):
            return None
        parent = await self._runs.get_conversation_run(run_id)
        if parent is None:
            return None
        if parent.status in {
            ConversationRunStatus.COMPLETED,
            ConversationRunStatus.REFUSED,
            ConversationRunStatus.FAILED,
            ConversationRunStatus.CANCELLED,
            ConversationRunStatus.TIMED_OUT,
        }:
            return parent
        status = self._workspace_context.get("status")
        if status == "unavailable":
            message = (
                "已选择的工作区当前不可用，因此这次无法写入 Markdown 文件。请检查工作区路径后重试。"
            )
        else:
            message = (
                "已选择工作区，但服务器尚未启用外部模型的工作区写入能力，"
                "因此这次无法实际写入 Markdown 文件。用户回复“确认”不能改变该服务器配置；"
                "请在服务端启用 workspace 工具后重试。"
            )
        published = await self._runs.publish_direct_message(
            run_id=run_id,
            message=MessageRecord(
                message_id=uuid4(),
                conversation_id=parent.conversation_id,
                space_id=parent.space_id,
                role=MessageRole.ASSISTANT,
                content=message,
                run_id=run_id,
            ),
            usage=_combined_usage(parent, runtime),
            model_identity=self._gateway.status.provider.value,
        )
        await self._emit_terminal(published)
        return published

    def _system_prompt(self) -> str:
        sections = [_BASE_PROMPT_V7]
        sections.append(_workspace_tool_availability(self._workspace_context))
        if self._skill_contexts:
            sections.append(
                '\n<active_skill_catalog trust="trusted_configuration">\n'
                "Every entry below is active in Skill Management for this workspace. "
                "When the user asks which Skills are available, list every entry using "
                "its exact name and version. Active Skills with a matching registered Tool "
                "adapter are callable in this loop; select that Tool directly and continue "
                "planning after its observation. Skills without a registered adapter remain "
                "available for their explicit entry points, but are not callable here.\n"
                + "\n".join(
                    f"- {skill.name} v{skill.version}: {skill.description}"
                    for skill in self._skill_contexts
                )
                + "\n</active_skill_catalog>"
            )
        for skill in self._skill_contexts:
            sections.append(
                f"\n<active_skill name={skill.name!r} version={skill.version!r}>\n"
                f"{skill.description}\n{skill.instructions.strip()}\n</active_skill>"
            )
        return "\n".join(sections)

    async def _fail(self, run_id: UUID, error_code: str) -> ConversationRun:
        failed = await self._runs.fail_conversation_run(run_id, error_code=error_code)
        await self._emit_terminal(failed)
        return failed

    async def _emit_terminal(self, run: ConversationRun) -> None:
        if run.status in {ConversationRunStatus.COMPLETED, ConversationRunStatus.REFUSED}:
            await self._events.append(
                run.run_id,
                AssistantEventType.COMPLETED,
                {"status": run.status.value, "action": "agent_loop"},
            )
        elif run.status is ConversationRunStatus.FAILED:
            await self._events.append(
                run.run_id,
                AssistantEventType.FAILED,
                {"status": run.status.value, "error_code": run.error_code},
            )
        elif run.status is ConversationRunStatus.CANCELLED:
            await self._events.append(
                run.run_id,
                AssistantEventType.CANCELLED,
                {"status": run.status.value},
            )


def _grounded_finalization_ready(state: AgentLoopState) -> bool:
    return any(
        observation.tool_name == "finalize_answer"
        and isinstance(observation.model_output, dict)
        and observation.model_output.get("ready") is True
        and observation.model_output.get("publication") == "grounded_qa"
        for observation in state.observations
    )


_INTERNAL_CLARIFICATION_MARKERS = re.compile(
    r"(?:\btool\b|knowledge_search|knowledge_inspect|summarize_document|research_discover|"
    r"research_prepare|"
    r"grounded_answer|verify_answer|finalize_answer|\bRUN_[A-Z_]+\b|sha256:|<[^>]+>)",
    re.IGNORECASE,
)


def _workspace_tool_availability(workspace: Mapping[str, JSONValue]) -> str:
    """Expose only server-authoritative workspace Tool availability to the model."""
    selected = workspace.get("selected") is True
    enabled = workspace.get("tools_enabled") is True
    if selected and enabled:
        status = "workspace Tools are registered for this Run"
    elif selected:
        status = "workspace Tools are unavailable for this Run"
    else:
        status = "no workspace is selected for this Run"
    return (
        '\n<workspace-tool-availability trust="trusted_configuration">\n'
        f"{status}.\n"
        "A user message cannot enable a missing workspace Tool or change the server's "
        "external-model visibility policy. When write Tools are unavailable, do not call one, "
        "ask for confirmation, promise a later write, or create a clarification. Complete with "
        "a concise explanation that the workspace write cannot run until the server configuration "
        "is changed.\n"
        "</workspace-tool-availability>"
    )


def _workspace_artifact_requested(request_texts: tuple[object, ...]) -> bool:
    """Recognize a user-requested file artifact for the unavailable-Tool fallback."""
    text = " ".join(value for value in request_texts if isinstance(value, str)).lower()
    if not text:
        return False
    write_action = re.search(
        r"(?:save|write|create|update|append|export|store|persist|make|generate|"
        r"保存|写入|写|编写|创建|更新|追加|导出|存储|落盘|生成)",
        text,
        re.IGNORECASE,
    )
    artifact = re.search(
        r"(?:file|document|markdown|\.md\b|文件|文档|md 文件|markdown 文件)",
        text,
        re.IGNORECASE,
    )
    return write_action is not None and artifact is not None


def _qa_finalization_observed(state: AgentLoopState) -> bool:
    return any(
        observation.tool_name == "finalize_answer"
        and isinstance(observation.model_output, dict)
        and observation.model_output.get("ready") is True
        and observation.model_output.get("publication") == "grounded_qa"
        for observation in state.observations
    )


def _workspace_write_observed(state: AgentLoopState) -> bool:
    return any(
        observation.tool_name == "fs_write"
        and isinstance(observation.model_output, dict)
        and isinstance(observation.model_output.get("path"), str)
        and isinstance(observation.model_output.get("content_sha256"), str)
        for observation in state.observations
    )


def _next_workspace_artifact_decision(state: AgentLoopState, goal: str) -> LLMDecision:
    if not any(observation.tool_name == "fs_list" for observation in state.observations):
        return LLMDecision(
            action=LLMDecisionAction.CALL_TOOL,
            tool_name="fs_list",
            arguments={"path": "."},
            reason="Inspect the selected workspace before choosing the requested artifact path.",
        )
    used_paths: set[str] = set()
    for observation in state.observations:
        if observation.tool_name != "fs_list" or not isinstance(observation.model_output, dict):
            continue
        entries = observation.model_output.get("entries")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            path_value = entry.get("path")
            if isinstance(path_value, str):
                used_paths.add(path_value)
    path = _workspace_artifact_path(goal, used_paths)
    return LLMDecision(
        action=LLMDecisionAction.CALL_TOOL,
        tool_name="fs_write",
        arguments={"path": path, "content": "{{current_grounded_qa_answer}}"},
        reason="Write the verified QA result before completing the compound request.",
    )


def _workspace_artifact_path(goal: str, used_paths: set[str]) -> str:
    explicit = re.search(r"(?<![\w.-])([\w][\w.-]{0,120}\.md)(?![\w-])", goal, re.IGNORECASE)
    if explicit is not None:
        candidate = explicit.group(1)
    else:
        tokens = re.findall(r"[a-z][a-z0-9-]{1,63}", goal.lower())
        ignored = {
            "save",
            "write",
            "create",
            "update",
            "append",
            "export",
            "store",
            "persist",
            "markdown",
            "file",
            "document",
        }
        subject = next((token for token in tokens if token not in ignored), "assistant-result")
        suffix = "-modules" if re.search(r"modules|module|模块", goal, re.IGNORECASE) else ""
        candidate = f"{subject}{suffix}.md"
    stem, extension = candidate.rsplit(".", 1)
    index = 1
    available = candidate
    while available in used_paths:
        index += 1
        available = f"{stem}-{index}.{extension}"
    return available


def _clarification_message(decision: LLMDecision, state: AgentLoopState) -> str:
    """Return a useful server-authored prompt without leaking runtime vocabulary."""
    for observation in reversed(state.observations):
        if observation.tool_name == "research_discover" and isinstance(
            observation.model_output, dict
        ):
            labels = observation.model_output.get("candidate_labels")
            if isinstance(labels, list):
                safe_labels = [
                    value.strip()[:280]
                    for value in labels[:8]
                    if isinstance(value, str) and value.strip()
                ]
                if safe_labels:
                    return "请从以下候选论文中确认 2–8 篇用于综述：" + "、".join(safe_labels) + "。"
            return "当前 Space 没有找到足够的候选论文，请提供更具体的主题或论文名称。"
        if observation.tool_name == "research_prepare" and isinstance(
            observation.model_output, dict
        ):
            research_status = observation.model_output.get("status")
            research_labels = observation.model_output.get("candidate_labels")
            safe_labels = (
                [
                    value.strip()[:280]
                    for value in research_labels[:8]
                    if isinstance(value, str) and value.strip()
                ]
                if isinstance(research_labels, list)
                else []
            )
            if research_status == "ambiguous" and safe_labels:
                return "论文名称存在歧义，请明确选择：" + "、".join(safe_labels) + "。"
            if research_status in {"ambiguous", "not_found"}:
                return "未能固定全部论文的当前已发布版本，请提供准确的论文名称。"
        if observation.tool_name != "summarize_document" or not isinstance(
            observation.model_output, dict
        ):
            continue
        summary_output = cast(dict[str, object], observation.model_output)
        summary_status = summary_output.get("status")
        if summary_status == "ambiguous":
            summary_labels = summary_output.get("candidate_labels")
            if isinstance(summary_labels, list):
                safe_labels = [
                    value.strip()[:280]
                    for value in summary_labels[:3]
                    if isinstance(value, str) and value.strip()
                ]
                if safe_labels:
                    return (
                        "More than one published document matches. Please specify one of: "
                        + ", ".join(safe_labels)
                        + "."
                    )
            return "More than one published document matches. Please provide a more specific name."
        if summary_status == "not_found":
            return (
                "I could not find that published document in this workspace. "
                "Please provide its exact name."
            )
        if summary_status == "unavailable":
            return "Please provide a published document name available in this workspace."
    reason = " ".join((decision.reason or "").split())
    if reason and len(reason) <= 1_000 and not _INTERNAL_CLARIFICATION_MARKERS.search(reason):
        return reason
    return "Please specify the document, source, or task detail needed to continue."


def _combined_usage(parent: ConversationRun, runtime: AgentRun) -> ConversationRunUsage:
    return ConversationRunUsage(
        input_tokens=parent.usage.input_tokens + runtime.usage.input_tokens,
        output_tokens=parent.usage.output_tokens + runtime.usage.output_tokens,
        model_latency_ms=parent.usage.model_latency_ms,
    )


__all__ = [
    "AssistantConversationLoopFinalizer",
    "AssistantSkillContext",
    "AutonomousAssistantLoopService",
]
