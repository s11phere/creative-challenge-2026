"""Worker-only orchestration for ordinary Assistant conversation turns."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, Protocol
from uuid import UUID, uuid4

from domain.assistant_sse import AssistantEventStore, AssistantEventType
from domain.conversation_run import (
    Clarification,
    ClarificationKind,
    ConversationRun,
    ConversationRunKind,
    ConversationRunRepository,
    ConversationRunSelectionSource,
    ConversationRunStatus,
    ConversationRunUsage,
)
from domain.qa_persistence import MessageRecord, MessageRole
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from model_gateway import (
    CapabilityAlias,
    ChatMessage,
    ChatRequest,
    ChatRole,
    ModelGateway,
    ModelGatewayError,
)

from application.skills import SkillCatalogPort, SkillInvocationView

from .context import ConversationContextService, ConversationContextSnapshot
from .metrics import AssistantMetrics

_CONTRACT_ROOT = files("application.assistant").joinpath("contracts")
_BASE_PROMPT = _CONTRACT_ROOT.joinpath("base-system-prompt-v1.txt").read_text(encoding="utf-8")
_BASE_PROMPT_V2 = _CONTRACT_ROOT.joinpath("base-system-prompt-v2.txt").read_text(encoding="utf-8")
_ROUTER_SCHEMA = json.loads(
    _CONTRACT_ROOT.joinpath("router-decision-v1.schema.json").read_text(encoding="utf-8")
)
_ROUTER_VALIDATOR = Draft202012Validator(_ROUTER_SCHEMA)


class AssistantMessageReader(Protocol):
    async def get_message(self, message_id: UUID) -> MessageRecord | None: ...


class AssistantSkillInvoker(Protocol):
    async def invoke(
        self,
        run: ConversationRun,
        *,
        skill: SkillInvocationView,
        arguments: Mapping[str, object],
        selection_source: ConversationRunSelectionSource = ConversationRunSelectionSource.AUTO,
        context: ConversationContextSnapshot | None = None,
    ) -> ConversationRun: ...


class AssistantAgentError(ValueError):
    """Safe execution error that never contains user or model content."""


@dataclass(frozen=True)
class AssistantRouterDecision:
    action: str
    assistant_message: str | None = None
    skill_name: str | None = None
    arguments: Mapping[str, object] | None = None


class AssistantRouterDecisionParser:
    """Validate the frozen JSON-only decision contract without exposing raw output."""

    def parse(self, raw_response: str) -> AssistantRouterDecision:
        try:
            payload: Any = json.loads(raw_response)
        except (TypeError, json.JSONDecodeError) as exc:
            raise AssistantAgentError("RUN_AGENT_DECISION_INVALID") from exc
        if not isinstance(payload, dict):
            raise AssistantAgentError("RUN_AGENT_DECISION_INVALID")
        try:
            _ROUTER_VALIDATOR.validate(payload)
        except ValidationError as exc:
            raise AssistantAgentError("RUN_AGENT_DECISION_INVALID") from exc
        action = payload["action"]
        assert isinstance(action, str)
        message = payload.get("assistant_message")
        if message is not None and not isinstance(message, str):
            raise AssistantAgentError("RUN_AGENT_DECISION_INVALID")
        skill_name = payload.get("skill_name")
        arguments = payload.get("arguments")
        if skill_name is not None and not isinstance(skill_name, str):
            raise AssistantAgentError("RUN_AGENT_DECISION_INVALID")
        if arguments is not None and not isinstance(arguments, dict):
            raise AssistantAgentError("RUN_AGENT_DECISION_INVALID")
        return AssistantRouterDecision(
            action=action,
            assistant_message=message,
            skill_name=skill_name,
            arguments=arguments,
        )


class AssistantAgentService:
    """Execute a claimed direct-conversation Run through the shared ModelGateway port."""

    def __init__(
        self,
        *,
        runs: ConversationRunRepository,
        messages: AssistantMessageReader,
        gateway: ModelGateway,
        events: AssistantEventStore,
        decision_parser: AssistantRouterDecisionParser | None = None,
        skill_catalog: SkillCatalogPort | None = None,
        skill_invoker: AssistantSkillInvoker | None = None,
        context: ConversationContextService | None = None,
        metrics: AssistantMetrics | None = None,
    ) -> None:
        self._runs = runs
        self._messages = messages
        self._gateway = gateway
        self._events = events
        self._decision_parser = decision_parser or AssistantRouterDecisionParser()
        self._skill_catalog = skill_catalog
        self._skill_invoker = skill_invoker
        self._context = context
        self._metrics = metrics

    async def execute(self, run_id: UUID) -> ConversationRun | None:
        """Finish a Worker-claimed turn; API handlers only enqueue this work."""
        run = await self._runs.get_conversation_run(run_id)
        if run is None:
            return None
        if run.run_kind is not ConversationRunKind.ASSISTANT_TURN:
            raise AssistantAgentError("RUN_AGENT_DECISION_INVALID")
        if run.status in {
            ConversationRunStatus.COMPLETED,
            ConversationRunStatus.FAILED,
            ConversationRunStatus.CANCELLED,
            ConversationRunStatus.TIMED_OUT,
            ConversationRunStatus.REFUSED,
        }:
            await self._emit_terminal(run)
            return run
        if run.status is ConversationRunStatus.WAITING_CLARIFICATION:
            await self._events.append(
                run_id,
                AssistantEventType.CLARIFICATION,
                {"status": run.status.value, "action": "clarify"},
            )
            return run
        if run.cancellation_requested:
            cancelled = await self._runs.cancel_conversation_run(run_id)
            self._record_termination(cancelled, reason="cancel_requested")
            await self._emit_terminal(cancelled)
            return cancelled

        user_message = await self._messages.get_message(run.user_message_id)
        if (
            user_message is None
            or user_message.role is not MessageRole.USER
            or user_message.conversation_id != run.conversation_id
            or user_message.space_id != run.space_id
        ):
            return await self._fail(run_id, "RUN_AGENT_DECISION_INVALID")

        try:
            context = await self._context.snapshot(run) if self._context is not None else None
            response = await self._gateway.chat(
                ChatRequest(
                    messages=(
                        ChatMessage(
                            role=ChatRole.SYSTEM,
                            content=self._system_prompt(),
                        ),
                        ChatMessage(
                            role=ChatRole.USER,
                            content=(
                                context.router_input()
                                if context is not None
                                else user_message.content
                            ),
                        ),
                    ),
                    temperature=0.0,
                    max_tokens=12000,
                ),
                capability=CapabilityAlias.FAST_CHAT,
            )
            decision = self._decision_parser.parse(response.text)
        except ModelGatewayError as exc:
            return await self._fail(run_id, exc.code.value)
        except AssistantAgentError as exc:
            return await self._fail(run_id, str(exc))

        await self._events.append(
            run_id,
            AssistantEventType.ROUTING,
            {"status": ConversationRunStatus.RUNNING.value, "action": decision.action},
        )
        if self._metrics is not None:
            self._metrics.record_router_decision(decision.action)
        usage = ConversationRunUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            model_latency_ms=response.latency_ms,
        )
        model_identity = self._gateway.status.provider.value
        if self._metrics is not None:
            self._metrics.record_usage(
                run_kind=run.run_kind.value,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                latency_ms=usage.model_latency_ms,
            )

        if decision.action == "respond":
            assert decision.assistant_message is not None
            published = await self._runs.publish_direct_message(
                run_id=run_id,
                message=MessageRecord(
                    message_id=uuid4(),
                    conversation_id=run.conversation_id,
                    space_id=run.space_id,
                    role=MessageRole.ASSISTANT,
                    content=decision.assistant_message,
                    run_id=run_id,
                ),
                usage=usage,
                model_identity=model_identity,
            )
            self._record_termination(published, reason="respond")
            await self._emit_terminal(published, action="respond")
            return published
        if decision.action == "clarify":
            clarified = await self._runs.publish_clarification(
                run_id=run_id,
                clarification=Clarification(
                    clarification_id=f"clarify:{run_id.hex}",
                    kind=ClarificationKind.INPUT_REQUIRED,
                    message="Please provide the missing detail needed to continue.",
                ),
                usage=usage,
                model_identity=model_identity,
            )
            if clarified.status is ConversationRunStatus.WAITING_CLARIFICATION:
                assert clarified.result is not None
                assert clarified.result.clarification is not None
                if self._metrics is not None:
                    self._metrics.record_clarification(clarified.result.clarification.kind.value)
                await self._events.append(
                    run_id,
                    AssistantEventType.CLARIFICATION,
                    {"status": clarified.status.value, "action": "clarify"},
                )
            else:
                self._record_termination(clarified, reason="clarify")
                await self._emit_terminal(clarified, action="clarify")
            return clarified
        if decision.action == "invoke_skill":
            skill_name = decision.skill_name
            arguments = decision.arguments
            skill = self._find_skill(skill_name)
            if skill is None:
                return await self._fail(run_id, "SKILL_NOT_ACTIVE")
            if self._skill_invoker is None or arguments is None:
                return await self._fail(run_id, "RUN_AGENT_DECISION_INVALID")
            await self._events.append(
                run_id,
                AssistantEventType.SKILL_STARTED,
                {"status": ConversationRunStatus.RUNNING.value, "skill": skill.name},
            )
            try:
                if context is None:
                    invoked = await self._skill_invoker.invoke(
                        run, skill=skill, arguments=arguments
                    )
                else:
                    invoked = await self._skill_invoker.invoke(
                        run, skill=skill, arguments=arguments, context=context
                    )
            except AssistantAgentError as exc:
                return await self._fail(run_id, str(exc))
            except ValueError as exc:
                code = str(exc)
                if code not in {"SKILL_NOT_ACTIVE", "RESOURCE_NOT_FOUND", "RESOURCE_CONFLICT"}:
                    code = "RUN_AGENT_DECISION_INVALID"
                return await self._fail(run_id, code)
            if invoked.status in {
                ConversationRunStatus.FAILED,
                ConversationRunStatus.CANCELLED,
                ConversationRunStatus.COMPLETED,
                ConversationRunStatus.REFUSED,
            }:
                self._record_termination(invoked, reason="invoke_skill")
                await self._emit_terminal(invoked, action="invoke_skill")
            elif (
                invoked.status is ConversationRunStatus.WAITING_CLARIFICATION
                and invoked.result is not None
                and invoked.result.clarification is not None
                and self._metrics is not None
            ):
                self._metrics.record_clarification(invoked.result.clarification.kind.value)
            return invoked
        return await self._fail(run_id, "RUN_AGENT_DECISION_INVALID")

    def _system_prompt(self) -> str:
        if self._skill_catalog is None:
            return _BASE_PROMPT_V2
        entries = self._skill_catalog.list_active_invocations()
        lines = [_BASE_PROMPT_V2, "\nActive Skill catalog (untrusted metadata only):"]
        for entry in entries:
            aliases = ", ".join(entry.aliases) if entry.aliases else "none"
            lines.append(
                f"- {entry.name}: command={entry.command}; aliases={aliases}; "
                f"input_mode={entry.input_mode}; trigger={entry.description}; "
                f"argument_hint={entry.argument_hint}"
            )
        return "\n".join(lines)

    def _find_skill(self, name: str | None) -> SkillInvocationView | None:
        if self._skill_catalog is None or name is None:
            return None
        return next(
            (
                entry
                for entry in self._skill_catalog.list_active_invocations()
                if entry.name == name
            ),
            None,
        )

    async def _fail(self, run_id: UUID, error_code: str) -> ConversationRun:
        failed = await self._runs.fail_conversation_run(run_id, error_code=error_code)
        self._record_termination(failed, reason=error_code)
        await self._emit_terminal(failed)
        return failed

    def _record_termination(self, run: ConversationRun, *, reason: str) -> None:
        if self._metrics is not None and run.status in {
            ConversationRunStatus.COMPLETED,
            ConversationRunStatus.FAILED,
            ConversationRunStatus.CANCELLED,
            ConversationRunStatus.REFUSED,
            ConversationRunStatus.TIMED_OUT,
        }:
            self._metrics.record_termination(
                run_kind=run.run_kind.value,
                status=run.status.value,
                reason=reason,
            )

    async def _emit_terminal(self, run: ConversationRun, *, action: str | None = None) -> None:
        if run.status is ConversationRunStatus.COMPLETED:
            await self._events.append(
                run.run_id,
                AssistantEventType.COMPLETED,
                {"status": run.status.value, "action": action or "respond"},
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


__all__ = [
    "AssistantAgentError",
    "AssistantAgentService",
    "AssistantMessageReader",
    "AssistantSkillInvoker",
    "AssistantRouterDecision",
    "AssistantRouterDecisionParser",
]
