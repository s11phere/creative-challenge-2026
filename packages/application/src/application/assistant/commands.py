"""Server-authoritative Assistant command catalog, parser, and command use cases."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from domain.conversation_run import (
    ConversationRun,
    ConversationRunKind,
    ConversationRunRepository,
    ConversationRunSelectionSource,
    ConversationRunStatus,
)
from domain.qa_persistence import ConversationRecord, GroundedQARepository

from application.skills import SkillCatalogPort, SkillInvocationView

from .context import ConversationContextService, ConversationContextSnapshot
from .runs import AssistantTurnApplicationPort, AssistantTurnSubmission


class AssistantCommandKind(StrEnum):
    BASE = "base"
    SKILL = "skill"


class CommandParseError(ValueError):
    """Safe parser error; candidates contain metadata only."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        candidates: tuple[CommandDescriptor, ...] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.candidates = candidates


class CommandCatalogError(ValueError):
    """The active command catalog cannot be exposed safely."""

    code = "RUN_COMMAND_ALIAS_CONFLICT"


@dataclass(frozen=True)
class CommandDescriptor:
    """Safe command metadata; internal Skill identity is never serialized directly."""

    name: str
    aliases: tuple[str, ...]
    kind: AssistantCommandKind
    description: str
    argument_hint: str
    input_mode: str
    skill_name: str | None = None

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    def public_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "aliases": list(self.aliases),
            "kind": self.kind.value,
            "description": self.description,
            "argument_hint": self.argument_hint,
            "input_mode": self.input_mode,
        }


@dataclass(frozen=True)
class ParsedAssistantCommand:
    """A command selected from the original user message."""

    descriptor: CommandDescriptor | None
    arguments: dict[str, object]
    argument_text: str
    content: str
    escaped: bool = False

    @property
    def is_command(self) -> bool:
        return self.descriptor is not None


@dataclass(frozen=True)
class CommandExecutionResult:
    """Result of a base command without forcing a ConversationRun response shape."""

    command: str
    status: str
    content: str | None = None
    conversation_id: UUID | None = None
    run: ConversationRun | None = None
    commands: tuple[CommandDescriptor, ...] = ()


_BASE_COMMANDS: tuple[CommandDescriptor, ...] = (
    CommandDescriptor(
        name="help",
        aliases=(),
        kind=AssistantCommandKind.BASE,
        description="展示当前可用基础指令和 active Skill 指令。",
        argument_hint="",
        input_mode="none",
    ),
    CommandDescriptor(
        name="skills",
        aliases=(),
        kind=AssistantCommandKind.BASE,
        description="展示当前 active Skill 及其用途和可用状态。",
        argument_hint="",
        input_mode="none",
    ),
    CommandDescriptor(
        name="new",
        aliases=(),
        kind=AssistantCommandKind.BASE,
        description="创建并切换到新会话。",
        argument_hint="",
        input_mode="none",
    ),
    CommandDescriptor(
        name="compact",
        aliases=(),
        kind=AssistantCommandKind.BASE,
        description="请求后台为当前会话生成滚动摘要。",
        argument_hint="",
        input_mode="none",
    ),
    CommandDescriptor(
        name="stop",
        aliases=(),
        kind=AssistantCommandKind.BASE,
        description="取消当前会话中的活动 Run。",
        argument_hint="",
        input_mode="none",
    ),
)


class AssistantCommandCatalog:
    """Merge fixed base commands with the active Skill invocation catalog."""

    def __init__(self, skill_catalog: SkillCatalogPort) -> None:
        self._skill_catalog = skill_catalog

    def list(self) -> tuple[CommandDescriptor, ...]:
        descriptors = list(_BASE_COMMANDS)
        descriptors.extend(
            self._skill_descriptor(item) for item in self._skill_catalog.list_active_invocations()
        )
        by_name: dict[str, CommandDescriptor] = {}
        for descriptor in descriptors:
            for name in descriptor.names:
                normalized = name.casefold()
                previous = by_name.get(normalized)
                if previous is not None and previous != descriptor:
                    raise CommandCatalogError(
                        f"Command name or alias '{name}' conflicts with '{previous.name}'."
                    )
                by_name[normalized] = descriptor
        return tuple(descriptors)

    def find(self, name: str) -> CommandDescriptor | None:
        normalized = name.casefold()
        return next(
            (
                descriptor
                for descriptor in self.list()
                if normalized in {item.casefold() for item in descriptor.names}
            ),
            None,
        )

    @staticmethod
    def _skill_descriptor(item: SkillInvocationView) -> CommandDescriptor:
        return CommandDescriptor(
            name=item.command,
            aliases=item.aliases,
            kind=AssistantCommandKind.SKILL,
            description=item.description,
            argument_hint=item.argument_hint,
            input_mode=item.input_mode,
            skill_name=item.name,
        )


class AssistantCommandParser:
    """Parse only the first non-whitespace slash command from original content."""

    def __init__(self, catalog: AssistantCommandCatalog) -> None:
        self._catalog = catalog

    def parse(self, content: str, *, declared_command: str | None = None) -> ParsedAssistantCommand:
        if not isinstance(content, str) or not content.strip():
            raise CommandParseError("RUN_COMMAND_ARGUMENT_REQUIRED", "Command content is required.")
        stripped = content.lstrip()
        if not stripped.startswith("/"):
            if declared_command is not None:
                raise CommandParseError(
                    "RUN_COMMAND_UNKNOWN", "Declared command does not match the message."
                )
            return ParsedAssistantCommand(None, {}, "", content)
        if stripped.startswith("//"):
            if declared_command is not None:
                raise CommandParseError(
                    "RUN_COMMAND_UNKNOWN",
                    "Declared command does not match the escaped message.",
                )
            return ParsedAssistantCommand(None, {}, "", stripped[1:], escaped=True)

        body = stripped[1:]
        pieces = body.split(maxsplit=1)
        command_token = pieces[0] if pieces else ""
        argument_text = pieces[1] if len(pieces) == 2 else ""
        descriptor = self._catalog.find(command_token) if command_token else None
        if descriptor is None:
            candidates = self._candidates(command_token)
            raise CommandParseError(
                "RUN_COMMAND_UNKNOWN",
                "Unknown Assistant command.",
                candidates=candidates,
            )
        if declared_command is not None:
            declared = self._catalog.find(declared_command.strip().lstrip("/"))
            if declared is None or declared.name != descriptor.name:
                raise CommandParseError(
                    "RUN_COMMAND_UNKNOWN", "Declared command does not match the message."
                )
        argument_text = argument_text.strip()
        arguments = self._arguments(descriptor, argument_text)
        return ParsedAssistantCommand(descriptor, arguments, argument_text, content)

    def _candidates(self, token: str) -> tuple[CommandDescriptor, ...]:
        normalized = token.casefold()
        descriptors = self._catalog.list()
        matching = tuple(
            descriptor
            for descriptor in descriptors
            if any(name.casefold().startswith(normalized) for name in descriptor.names)
        )
        return matching or descriptors[:16]

    @staticmethod
    def _arguments(descriptor: CommandDescriptor, argument_text: str) -> dict[str, object]:
        if descriptor.kind is AssistantCommandKind.BASE:
            return {}
        if descriptor.input_mode == "question":
            return {"question": argument_text}
        resource_type = "source" if descriptor.input_mode == "sources" else "document"
        return {
            "question": argument_text,
            "resource_type": resource_type,
            "resource_reference": argument_text,
        }


class ConversationWriter(Protocol):
    async def get_conversation(self, conversation_id: UUID) -> ConversationRecord | None: ...

    async def create_conversation(self, conversation: ConversationRecord) -> ConversationRecord: ...


class AssistantCommandService:
    """Map parsed commands to existing Assistant/QA application ports."""

    def __init__(
        self,
        *,
        catalog: AssistantCommandCatalog,
        parser: AssistantCommandParser,
        turns: AssistantTurnApplicationPort,
        runs: ConversationRunRepository,
        conversations: ConversationWriter,
        qa: GroundedQARepository,
        skill_invoker: SkillCommandInvoker | None = None,
        context: ConversationContextService | None = None,
    ) -> None:
        self.catalog = catalog
        self.parser = parser
        self._turns = turns
        self._runs = runs
        self._conversations = conversations
        self._qa = qa
        self._skill_invoker = skill_invoker
        self._context = context

    async def help(self) -> CommandExecutionResult:
        return CommandExecutionResult(
            command="help",
            status="completed",
            content="当前可用 Assistant 指令。",
            commands=self.catalog.list(),
        )

    async def skills(self) -> CommandExecutionResult:
        commands = tuple(
            item for item in self.catalog.list() if item.kind is AssistantCommandKind.SKILL
        )
        return CommandExecutionResult(
            command="skills", status="completed", content="当前 active Skill。", commands=commands
        )

    async def new_conversation(self, conversation_id: UUID) -> CommandExecutionResult:
        current = await self._conversations.get_conversation(conversation_id)
        if current is None or current.archived_at is not None:
            raise CommandParseError("CONVERSATION_NOT_FOUND", "Conversation not found.")
        created = await self._conversations.create_conversation(
            ConversationRecord(space_id=current.space_id, owner_id=current.owner_id)
        )
        return CommandExecutionResult(
            command="new",
            status="completed",
            content="已创建并切换到新会话。",
            conversation_id=created.conversation_id,
        )

    async def compact(
        self, conversation_id: UUID, *, content: str, idempotency_key: str
    ) -> CommandExecutionResult:
        # Step 5 owns summary persistence and execution. Step 4 still emits an explicit,
        # idempotent command intent without pretending a compaction worker exists.
        current = await self._conversations.get_conversation(conversation_id)
        if current is None or current.archived_at is not None:
            raise CommandParseError("CONVERSATION_NOT_FOUND", "Conversation not found.")
        if not content.strip() or not idempotency_key.strip():
            raise CommandParseError(
                "RUN_COMMAND_ARGUMENT_REQUIRED", "Compaction request is incomplete."
            )
        if self._context is None:
            raise CommandParseError("RUN_CONTEXT_COMPACTION_FAILED", "Compaction is unavailable.")
        run = await self._context.request_manual_compaction(
            conversation_id, content=content, idempotency_key=idempotency_key
        )
        return CommandExecutionResult(
            command="compact",
            status=run.status.value,
            content="上下文压缩将在 Step 5 的 Worker 用例中执行。",
            conversation_id=conversation_id,
            run=run,
        )

    async def stop(self, conversation_id: UUID) -> CommandExecutionResult:
        runs = await self._runs.list_conversation_runs(conversation_id)
        active = tuple(
            run
            for run in runs
            if run.status
            in {
                ConversationRunStatus.CREATED,
                ConversationRunStatus.QUEUED,
                ConversationRunStatus.RUNNING,
                ConversationRunStatus.WAITING_CLARIFICATION,
                ConversationRunStatus.WAITING_APPROVAL,
                ConversationRunStatus.CANCEL_REQUESTED,
            }
        )
        if not active:
            return CommandExecutionResult("stop", "completed", content="没有活动 Run。")
        run = active[-1]
        if run.run_kind in {ConversationRunKind.GROUNDED_QA, ConversationRunKind.SKILL}:
            await self._qa.request_cancel(run.run_id)
            updated = await self._runs.get_conversation_run(run.run_id)
            run = updated or run
        else:
            run = await self._turns.cancel(run.run_id)
        return CommandExecutionResult("stop", "cancel_requested", run=run)

    async def invoke_skill(
        self,
        conversation_id: UUID,
        parsed: ParsedAssistantCommand,
        *,
        idempotency_key: str,
    ) -> CommandExecutionResult:
        descriptor = parsed.descriptor
        if (
            descriptor is None
            or descriptor.kind is not AssistantCommandKind.SKILL
            or descriptor.skill_name is None
            or self._skill_invoker is None
        ):
            raise CommandParseError(
                "SKILL_NOT_ACTIVE", "The requested Skill command is not active."
            )
        run = await self._turns.submit(
            AssistantTurnSubmission(
                conversation_id=conversation_id,
                content=parsed.argument_text or parsed.content,
                idempotency_key=idempotency_key,
                selection_source=ConversationRunSelectionSource.COMMAND,
            )
        )
        if (
            run.run_kind in {ConversationRunKind.GROUNDED_QA, ConversationRunKind.SKILL}
            and run.skill is not None
            and run.skill.name == descriptor.skill_name
        ):
            return CommandExecutionResult(
                command=descriptor.name,
                status=run.status.value,
                run=run,
            )
        skill = next(
            (
                item
                for item in self._skill_invoker.catalog.list_active_invocations()
                if item.name == descriptor.skill_name
            ),
            None,
        )
        if skill is None:
            raise CommandParseError(
                "SKILL_NOT_ACTIVE", "The requested Skill command is not active."
            )
        try:
            context = await self._context.snapshot(run) if self._context is not None else None
            if context is None:
                promoted = await self._skill_invoker.invoke(
                    run,
                    skill=skill,
                    arguments=parsed.arguments,
                    selection_source=ConversationRunSelectionSource.COMMAND,
                )
            else:
                promoted = await self._skill_invoker.invoke(
                    run,
                    skill=skill,
                    arguments=parsed.arguments,
                    selection_source=ConversationRunSelectionSource.COMMAND,
                    context=context,
                )
        except ValueError as exc:
            code = str(exc)
            if code not in {"SKILL_NOT_ACTIVE", "RESOURCE_NOT_FOUND", "RESOURCE_CONFLICT"}:
                code = "RUN_AGENT_DECISION_INVALID"
            raise CommandParseError(code, "Skill command could not be completed.") from exc
        return CommandExecutionResult(
            command=descriptor.name,
            status=promoted.status.value,
            run=promoted,
        )


class SkillCommandInvoker(Protocol):
    @property
    def catalog(self) -> SkillCatalogPort: ...

    async def invoke(
        self,
        run: ConversationRun,
        *,
        skill: SkillInvocationView,
        arguments: Mapping[str, object],
        selection_source: ConversationRunSelectionSource,
        context: ConversationContextSnapshot | None = None,
    ) -> ConversationRun: ...


__all__ = [
    "AssistantCommandCatalog",
    "AssistantCommandKind",
    "AssistantCommandParser",
    "AssistantCommandService",
    "CommandCatalogError",
    "CommandDescriptor",
    "CommandExecutionResult",
    "CommandParseError",
    "ParsedAssistantCommand",
    "SkillCommandInvoker",
]
