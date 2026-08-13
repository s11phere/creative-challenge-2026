"""Dramatiq actor for durable product-level Assistant turns."""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import dramatiq
from agent_runtime import (
    FileToolPolicy,
    FileWritePolicy,
    InMemoryToolRegistry,
    JSONValue,
    PersonalSkillRegistry,
    ReadOnlyFileTools,
    ShellExecutionPolicy,
    SideEffectTools,
    ToolDefinition,
    ToolExecutionContext,
    ToolHandler,
    ToolRef,
    register_read_only_file_tools,
    register_side_effect_tools,
)
from application.assistant import (
    AssistantMetrics,
    AssistantSkillContext,
    AutonomousAssistantLoopService,
    ConversationCompactionService,
    ConversationContextService,
    ConversationFinalizer,
)
from application.skills import (
    NATIVE_KNOWLEDGE_AGENT_V2_INSTRUCTIONS,
    DraftSkillEvalRunner,
    KnowledgeLoopTools,
    KnowledgeLoopToolsConfig,
    NativeKnowledgeTools,
    NativeKnowledgeToolsConfig,
    PersonalSkillStore,
    SkillCreatorTools,
    SkillDraftStore,
    register_skill_creator_tools,
)
from domain.agent_runtime import ToolPermission
from domain.assistant_sse import AssistantEventType
from domain.conversation_run import ConversationRunKind, ConversationRunStatus
from domain.grounded_qa import QAAttempt, QAEvent, QAStatus
from domain.qa_persistence import QARetrievalScope, QARunRecord
from infrastructure.agent_events import PostgresAgentRunEventStore
from infrastructure.assistant_events import PostgresAssistantEventStore
from infrastructure.assistant_resources import PostgresAssistantResourceResolver
from infrastructure.config import settings
from infrastructure.conversation_runs import PostgresConversationRunRepository
from infrastructure.database import Database
from infrastructure.qa import DatabaseSearchService
from infrastructure.qa_debug_trace import QADebugTrace, TracingModelGateway, TracingToolRegistry
from infrastructure.qa_execution import (
    GroundedQAExecutor,
    StructuredAssistantLoopGateway,
    StructuredFakeGateway,
    StructuredNativeAssistantLoopGateway,
    assistant_skill_registry,
    qa_execution_versions,
)
from infrastructure.qa_persistence import PostgresGroundedQARepository, PostgresQAEventStore
from infrastructure.runtime_approval import PostgresApprovalPort
from infrastructure.runtime_state import PostgresRuntimeStateStore
from infrastructure.skill_catalog import FileSystemNativeSkillCatalog, FileSystemSkillCatalog
from infrastructure.skill_lifecycle import PostgresSkillActivationStore
from infrastructure.telemetry_context import (
    bind_observability_context,
    new_trace_id,
    normalize_trace_id,
    trace_parent_context,
)
from infrastructure.workspaces import WorkspacePathError, WorkspaceRoot
from model_gateway import ModelGateway
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from sqlalchemy.pool import NullPool

from worker.broker import broker
from worker.qa_tasks import _create_gateway
from worker.usage_traces import record_usage_trace

logger = logging.getLogger(__name__)
tracer = trace.get_tracer("worker.assistant")
database = Database(settings.database_url, poolclass=NullPool)
_TERMINAL = frozenset(
    {
        ConversationRunStatus.COMPLETED,
        ConversationRunStatus.REFUSED,
        ConversationRunStatus.FAILED,
        ConversationRunStatus.CANCELLED,
        ConversationRunStatus.TIMED_OUT,
        ConversationRunStatus.WAITING_CLARIFICATION,
    }
)


def _workspace_model_visibility_allowed(gateway: ModelGateway) -> bool:
    """Allow workspace Tools only for fake models or explicit user consent."""
    return (
        gateway.status.provider.value == "fake" or settings.agent_workspace_model_visibility_consent
    )


@dramatiq.actor(
    broker=broker,
    actor_name="assistant_run",
    queue_name="qa",
    max_retries=settings.qa_task_max_retries,
    time_limit=settings.qa_task_timeout_ms,
    notify_shutdown=True,
)
def assistant_run(*, run_id: str, trace_id: str, event_version: int) -> None:
    """Execute one persisted direct conversation turn using control metadata only."""
    uid = UUID(run_id)
    if event_version != 2:
        raise ValueError("Unsupported Assistant task event version")
    canonical_trace_id = normalize_trace_id(trace_id)
    if canonical_trace_id is None:
        raise ValueError("Invalid trace ID")
    with (
        tracer.start_as_current_span(
            "assistant_run.process",
            context=trace_parent_context(canonical_trace_id),
            kind=SpanKind.CONSUMER,
            attributes={
                "messaging.system": "redis",
                "messaging.operation.name": "process",
                "assistant.run_id": run_id,
            },
        ),
        bind_observability_context(trace_id=canonical_trace_id, task_id=run_id),
    ):
        if not _run_assistant_sync(uid, canonical_trace_id):
            raise dramatiq.Retry(
                message="Assistant Run lease is active",
                delay=settings.qa_task_retry_delay_ms,
            )


def _run_assistant_sync(run_id: UUID, trace_id: str) -> bool:
    loop = asyncio.new_event_loop()
    gateway = _create_gateway()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(_run_assistant_async(run_id, gateway, trace_id=trace_id))
    finally:
        try:
            loop.run_until_complete(gateway.aclose())
        finally:
            loop.close()


async def _apply_personal_skill_activations(registry: PersonalSkillRegistry) -> None:
    """Re-apply durable personal-Skill activations; failures never break the Run."""
    try:
        store = PostgresSkillActivationStore(database)
        persisted = await store.list()
        activations = {
            item.name: item.version
            for item in persisted
            if registry.is_personal(item.name) and item.version in registry.versions(item.name)
        }
        registry.activate_all(activations)
    except Exception:
        logger.exception("personal_skill_activation_apply_failed")


async def _run_assistant_async(run_id: UUID, gateway: ModelGateway, *, trace_id: str) -> bool:
    runs = PostgresConversationRunRepository(database)
    lease_owner = str(uuid4())
    claimed = await runs.claim_conversation_run(
        run_id,
        lease_owner=lease_owner,
        lease_seconds=settings.qa_task_lease_seconds,
    )
    if claimed is None:
        return False
    registry = assistant_skill_registry()
    await _apply_personal_skill_activations(registry)
    qa_repository = PostgresGroundedQARepository(database)
    context = ConversationContextService(data=qa_repository, runs=runs)
    metrics = AssistantMetrics()

    service = await _autonomous_loop_service(
        gateway=gateway,
        registry=registry,
        runs=runs,
        qa_repository=qa_repository,
        context=context,
        metrics=metrics,
        run_id=run_id,
        trace_id=trace_id,
    )
    compaction = ConversationCompactionService(
        context=context,
        data=qa_repository,
        runs=runs,
        gateway=gateway,
        metrics=metrics,
    )
    events = PostgresAssistantEventStore(database)
    if claimed.run_kind is ConversationRunKind.CONTEXT_COMPACTION:
        completed = await _run_compaction_with_lease(
            compaction=compaction,
            events=events,
            runs=runs,
            run_id=run_id,
            lease_owner=lease_owner,
        )
        await record_usage_trace(run_id)
        return completed
    if claimed.status in _TERMINAL:
        if isinstance(service, AutonomousAssistantLoopService):
            await service.execute(run_id, trace_id=trace_id)
        else:
            await service.execute(run_id)
        await record_usage_trace(run_id)
        return True

    stop = asyncio.Event()
    lease_lost = asyncio.Event()
    heartbeat = asyncio.create_task(
        _heartbeat(runs, run_id, lease_owner, stop, lease_lost),
        name=f"assistant-heartbeat-{run_id}",
    )
    try:
        execution = asyncio.create_task(
            (
                service.execute(run_id, trace_id=trace_id)
                if isinstance(service, AutonomousAssistantLoopService)
                else service.execute(run_id)
            ),
            name=f"assistant-execution-{run_id}",
        )
        return await _wait_for_execution(execution, lease_lost, run_id=run_id)
    finally:
        stop.set()
        await heartbeat
        await runs.release_conversation_run_lease(run_id, lease_owner=lease_owner)
        await record_usage_trace(run_id)


async def _autonomous_loop_service(
    *,
    gateway: ModelGateway,
    registry: object,
    runs: PostgresConversationRunRepository,
    qa_repository: PostgresGroundedQARepository,
    context: ConversationContextService,
    metrics: AssistantMetrics,
    run_id: UUID,
    trace_id: str,
) -> AutonomousAssistantLoopService:
    """Build the top-level Loop from existing QA ports and trusted Skill packages."""
    from agent_runtime import SkillRegistryError

    skill_registry = registry
    assert isinstance(skill_registry, PersonalSkillRegistry)
    resources = PostgresAssistantResourceResolver(database)
    assistant_pin = skill_registry.pin("assistant_agent", "1.0.0")
    assistant_package = skill_registry.validate_pin(assistant_pin)
    knowledge_pin = skill_registry.pin("knowledge_agent")
    active_skill_contexts: list[AssistantSkillContext] = []
    for skill_name in skill_registry.names():
        try:
            active_pin = skill_registry.pin(skill_name)
        except SkillRegistryError:
            # The assistant package is pinned explicitly and is not an active
            # user-facing Skill; only expose activated catalog entries.
            continue
        active_package = skill_registry.validate_pin(active_pin)
        active_instructions = "\n\n".join(
            (active_package.root / prompt).read_text(encoding="utf-8")
            for prompt in active_package.manifest.prompts
        )
        active_skill_contexts.append(
            AssistantSkillContext(
                name=active_pin.name,
                version=active_pin.version,
                description=active_package.manifest.description,
                instructions=active_instructions,
            )
        )
    trace = QADebugTrace.from_settings(run_id=run_id, trace_id=trace_id, settings=settings)
    await trace.record(
        "run_started",
        skill_name=assistant_pin.name,
        skill_version=assistant_pin.version,
        knowledge_skill_version=knowledge_pin.version,
        active_skill_names=tuple(context.name for context in active_skill_contexts),
    )
    qa_executor = GroundedQAExecutor(
        database=database,
        gateway=gateway,
        repository=qa_repository,
        events=PostgresQAEventStore(database),
        agent_events=PostgresAgentRunEventStore(database),
        skill_registry=skill_registry,
    )
    qa_service = qa_executor.build_service(
        gateway,
        generation_gateway=TracingModelGateway(
            StructuredFakeGateway(gateway), trace, phase="grounded_generation"
        ),
    )
    versions = qa_execution_versions(skill_registry, skill_name="knowledge_agent")
    parent = await runs.get_conversation_run(run_id)
    if parent is None:
        raise ValueError("RUN_ASSISTANT_PARENT_MISSING")
    conversation = await qa_repository.get_conversation(parent.conversation_id)
    if conversation is None:
        raise ValueError("CONVERSATION_NOT_FOUND")
    creator_drafts = SkillDraftStore(
        registry=skill_registry,
        personal_store=PersonalSkillStore(
            registry=skill_registry,
            store=PostgresSkillActivationStore(database),
        ),
        eval_runner=DraftSkillEvalRunner(registry=skill_registry),
    )
    creator_tools = SkillCreatorTools(draft_store=creator_drafts)
    extra_handlers: dict[str, ToolHandler] = dict(creator_tools.handlers())
    extra_permissions: frozenset[ToolPermission] = frozenset(
        {ToolPermission.READ_KNOWLEDGE, ToolPermission.WRITE_KNOWLEDGE}
    )

    def register_creator_tools(
        registry: InMemoryToolRegistry,
    ) -> tuple[ToolDefinition, ...]:
        return register_skill_creator_tools(registry)

    extra_tool_registrar: Callable[[InMemoryToolRegistry], tuple[ToolDefinition, ...]] = (
        register_creator_tools
    )
    workspace_context: dict[str, JSONValue] = {"selected": False, "tools_enabled": False}
    if conversation.workspace_path is not None:
        workspace = None
        try:
            workspace = WorkspaceRoot(settings.agent_workspace_root).resolve(
                conversation.workspace_path
            )
        except WorkspacePathError:
            workspace_context = {
                "selected": True,
                "path": conversation.workspace_path,
                "tools_enabled": False,
                "status": "unavailable",
            }
        if workspace is not None and _workspace_model_visibility_allowed(gateway):
            cancellation_probe = _workspace_cancellation_probe(runs)
            file_tools = ReadOnlyFileTools(
                FileToolPolicy(
                    roots={"workspace": workspace.root},
                    files_by_space={},
                    workspace_root_by_space={parent.space_id: "workspace"},
                ),
                cancellation_probe=cancellation_probe,
            )
            aliases = _workspace_executables(settings.agent_workspace_command_aliases)
            side_effect_tools = SideEffectTools(
                FileWritePolicy(
                    roots={"workspace": workspace.root},
                    allowed_paths_by_space={},
                    workspace_root_by_space={parent.space_id: "workspace"},
                ),
                ShellExecutionPolicy(
                    executables=aliases,
                    cwd_roots={"workspace": workspace.root},
                    allowed_cwds_by_space={},
                    workspace_root_by_space={parent.space_id: "workspace"},
                    environment={"PYTHONIOENCODING": "utf-8"},
                ),
                cancellation_probe=cancellation_probe,
            )
            extra_handlers = {
                **file_tools.handlers(),
                **side_effect_tools.handlers(),
                **creator_tools.handlers(),
            }

            def register_all_tools(
                registry: InMemoryToolRegistry,
            ) -> tuple[ToolDefinition, ...]:
                return (
                    *register_read_only_file_tools(registry),
                    *register_side_effect_tools(registry),
                    *register_skill_creator_tools(registry),
                )

            extra_tool_registrar = register_all_tools
            extra_permissions = frozenset(
                {
                    ToolPermission.READ_KNOWLEDGE,
                    ToolPermission.WRITE_KNOWLEDGE,
                    ToolPermission.EXECUTE_PROCESS,
                }
            )
            workspace_context = {
                "selected": True,
                "path": workspace.path,
                "tools_enabled": True,
                "path_convention": (
                    "All file paths and command working directories are relative "
                    "to this workspace; the selected workspace root is '.', never "
                    "the displayed workspace name."
                ),
                "workspace_root_reference": ".",
                "command_cwd_example": ".",
                "command_aliases": [cast(JSONValue, alias) for alias in sorted(aliases)],
            }
        elif workspace is not None:
            workspace_context = {
                "selected": True,
                "path": conversation.workspace_path,
                "tools_enabled": False,
                "status": "model_visibility_consent_required",
            }

    async def ensure_qa_run(tool_context: object, retrieval_scope: QARetrievalScope) -> QARunRecord:
        from agent_runtime import ToolExecutionContext

        assert isinstance(tool_context, ToolExecutionContext)
        existing = await qa_repository.get_run(tool_context.run.run_id)
        if existing is not None:
            return existing
        parent = await runs.get_conversation_run(tool_context.run.run_id)
        if parent is None:
            raise ValueError("RUN_ASSISTANT_PARENT_MISSING")
        created = await qa_repository.create_run(
            QARunRecord(
                run_id=parent.run_id,
                attempt=QAAttempt(run_id=parent.run_id),
                conversation_id=parent.conversation_id,
                question_message_id=parent.user_message_id,
                space_id=parent.space_id,
                caller_id=parent.caller_id,
                idempotency_key=parent.idempotency_key,
                versions=versions,
                retrieval_scope=retrieval_scope,
            )
        )
        if created.status is QAStatus.CREATED:
            return await qa_repository.transition_run(created.run_id, QAEvent.QUEUE)
        return created

    tools = KnowledgeLoopTools(
        qa=qa_service,
        search=DatabaseSearchService(database, gateway),
        config=KnowledgeLoopToolsConfig(
            profile=qa_executor.profile,
            versions=versions,
            tool_version="1.1.0",
            resource_resolver=resources,
        ),
        result_reader=qa_repository.get_run,
        ensure_qa_run=ensure_qa_run,
        extra_handlers=extra_handlers,
        extra_tool_registrar=extra_tool_registrar,
        approval_port=PostgresApprovalPort(database),
    )
    tools.replace_tool_registry(TracingToolRegistry(tools.tool_registry, trace))
    knowledge_tool_skill = ToolRef(knowledge_pin.name, knowledge_pin.version)
    tool_skill_refs = {
        tools.search_tool.ref: knowledge_tool_skill,
        tools.inspect_tool.ref: knowledge_tool_skill,
        tools.answer_tool.ref: knowledge_tool_skill,
        tools.verify_tool.ref: knowledge_tool_skill,
        tools.finalize_tool.ref: knowledge_tool_skill,
    }
    summary_context = next(
        (context for context in active_skill_contexts if context.name == "summarize_document"),
        None,
    )
    if tools.summary_tool is not None:
        tool_skill_refs[tools.summary_tool.ref] = (
            ToolRef(summary_context.name, summary_context.version)
            if summary_context is not None
            else knowledge_tool_skill
        )
    research_context = next(
        (
            context
            for context in active_skill_contexts
            if context.name == "research_reading_workflow"
        ),
        None,
    )
    if research_context is not None:
        research_skill = ToolRef(research_context.name, research_context.version)
        if tools.research_discover_tool is not None:
            tool_skill_refs[tools.research_discover_tool.ref] = research_skill
        if tools.research_prepare_tool is not None:
            tool_skill_refs[tools.research_prepare_tool.ref] = research_skill
    native_knowledge: NativeKnowledgeTools | None = None
    native_catalog: FileSystemNativeSkillCatalog | None = None
    native_registry: InMemoryToolRegistry | None = None
    native_allowed_tools: tuple[ToolRef, ...] = tools.allowed_tools
    native_base_tools: tuple[ToolRef, ...] = ()
    if settings.fast_chat_native_tool_use:
        native_knowledge = NativeKnowledgeTools(
            qa=qa_service,
            search=DatabaseSearchService(database, gateway),
            config=NativeKnowledgeToolsConfig(
                profile=qa_executor.profile,
                versions=versions,
                retrieval_scope=QARetrievalScope(),
                result_reader=qa_repository.get_run,
                ensure_qa_run=ensure_qa_run,
                instructions=NATIVE_KNOWLEDGE_AGENT_V2_INSTRUCTIONS,
            ),
        )
        native_registry = InMemoryToolRegistry(
            handlers={
                "knowledge_retrieve": native_knowledge._retrieve_handler,
                "knowledge_answer": native_knowledge._answer_handler,
                **extra_handlers,
            },
            approval_port=PostgresApprovalPort(database),
        )
        native_registry.register(native_knowledge.retrieve_tool)
        native_registry.register(native_knowledge.answer_tool)
        extra_native_tools = extra_tool_registrar(native_registry)
        native_base_tools = tuple(
            definition.ref
            for definition in extra_native_tools
            if definition.name in {"fs_list", "fs_read", "fs_write", "shell_exec"}
        )
        native_allowed_tools = (
            *native_knowledge.allowed_tools(),
            *(definition.ref for definition in extra_native_tools),
        )
        native_catalog = FileSystemNativeSkillCatalog(
            skill_registry,
            FileSystemSkillCatalog(skill_registry, include_manifest_v2=True),
            tool_adapters={
                ToolRef(knowledge_pin.name, knowledge_pin.version): native_allowed_tools,
            },
            prompt_overrides={
                knowledge_pin.name: NATIVE_KNOWLEDGE_AGENT_V2_INSTRUCTIONS,
            },
        )
    return AutonomousAssistantLoopService(
        runs=runs,
        messages=qa_repository,
        gateway=TracingModelGateway(
            (
                StructuredNativeAssistantLoopGateway(gateway)
                if settings.fast_chat_native_tool_use
                else StructuredAssistantLoopGateway(gateway)
            ),
            trace,
            phase="assistant_agent_decision",
        ),
        events=PostgresAssistantEventStore(database),
        agent_events=PostgresAgentRunEventStore(database),
        runtime_state=PostgresRuntimeStateStore(database),
        pin=assistant_pin,
        budget=assistant_package.manifest.budgets,
        tool_registry=tools.tool_registry,
        allowed_tools=tools.allowed_tools,
        native_tool_registry=native_registry,
        native_allowed_tools=native_allowed_tools,
        native_base_tools=native_base_tools,
        qa_results=qa_repository.get_run,
        conversation_finalizer=ConversationFinalizer(
            runs=runs,
            gateway=TracingModelGateway(gateway, trace, phase="assistant_finalization"),
        ),
        native_skill_catalog=native_catalog,
        native_server_tools=native_knowledge,
        decision_policy=tools.decision_policy,
        tool_skill_refs=tool_skill_refs,
        skill_contexts=tuple(active_skill_contexts),
        context=context,
        metrics=metrics,
        workspace_context=workspace_context,
        additional_permissions=extra_permissions,
        approval_port=PostgresApprovalPort(database),
        debug_trace=trace,
    )


def _workspace_cancellation_probe(
    runs: PostgresConversationRunRepository,
) -> Callable[[ToolExecutionContext], Awaitable[bool]]:
    async def is_cancelled(context: ToolExecutionContext) -> bool:
        parent = await runs.get_conversation_run(context.run.run_id)
        return parent is None or parent.cancellation_requested

    return is_cancelled


def _workspace_executables(configured_aliases: str) -> dict[str, Path]:
    aliases: dict[str, Path] = {}
    for alias in (item.strip() for item in configured_aliases.split(",")):
        if not alias or alias in aliases:
            continue
        executable = Path(sys.executable) if alias == "python" else shutil.which(alias)
        if executable is None:
            continue
        try:
            resolved = Path(executable).resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file():
            aliases[alias] = resolved
    return aliases


async def _run_compaction_with_lease(
    *,
    compaction: ConversationCompactionService,
    events: PostgresAssistantEventStore,
    runs: PostgresConversationRunRepository,
    run_id: UUID,
    lease_owner: str,
) -> bool:
    stop = asyncio.Event()
    lease_lost = asyncio.Event()
    heartbeat = asyncio.create_task(
        _heartbeat(runs, run_id, lease_owner, stop, lease_lost),
        name=f"context-compaction-heartbeat-{run_id}",
    )
    try:
        await events.append(
            run_id,
            AssistantEventType.PHASE,
            {"status": ConversationRunStatus.RUNNING.value, "phase": "context_compaction"},
        )
        execution = asyncio.create_task(
            compaction.execute(run_id), name=f"context-compaction-{run_id}"
        )
        completed = await _wait_for_execution(execution, lease_lost, run_id=run_id)
        result = await runs.get_conversation_run(run_id)
        if result is None:
            return completed
        if result.status is ConversationRunStatus.COMPLETED:
            await events.append(
                run_id,
                AssistantEventType.COMPLETED,
                {"status": result.status.value, "action": "compact"},
            )
        elif result.status is ConversationRunStatus.CANCELLED:
            await events.append(
                run_id, AssistantEventType.CANCELLED, {"status": result.status.value}
            )
        elif result.status is ConversationRunStatus.FAILED:
            await events.append(
                run_id,
                AssistantEventType.FAILED,
                {"status": result.status.value, "error_code": result.error_code},
            )
        return completed
    finally:
        stop.set()
        await heartbeat
        await runs.release_conversation_run_lease(run_id, lease_owner=lease_owner)


async def _wait_for_execution(
    execution: asyncio.Task[object], lease_lost: asyncio.Event, *, run_id: UUID
) -> bool:
    lease_guard = asyncio.create_task(
        lease_lost.wait(),
        name=f"assistant-lease-guard-{run_id}",
    )
    done, _pending = await asyncio.wait(
        {execution, lease_guard}, return_when=asyncio.FIRST_COMPLETED
    )
    if lease_guard in done and lease_lost.is_set() and not execution.done():
        execution.cancel()
        with suppress(asyncio.CancelledError):
            await execution
        return False
    lease_guard.cancel()
    with suppress(asyncio.CancelledError):
        await lease_guard
    await execution
    return True


async def _heartbeat(
    runs: PostgresConversationRunRepository,
    run_id: UUID,
    lease_owner: str,
    stop: asyncio.Event,
    lease_lost: asyncio.Event,
) -> None:
    interval = min(settings.qa_task_heartbeat_interval_s, settings.qa_task_lease_seconds / 2)
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            renewed = await runs.renew_conversation_run_lease(
                run_id,
                lease_owner=lease_owner,
                lease_seconds=settings.qa_task_lease_seconds,
            )
            if not renewed:
                lease_lost.set()
                return


def enqueue_assistant_run(
    *, run_id: str, trace_id: str, event_version: int = 2
) -> dramatiq.Message[None]:
    UUID(run_id)
    canonical_trace_id = normalize_trace_id(trace_id)
    if canonical_trace_id is None:
        raise ValueError("Invalid trace ID")
    if event_version != 2:
        raise ValueError("Unsupported Assistant task event version")
    message = assistant_run.send(
        run_id=run_id,
        trace_id=canonical_trace_id,
        event_version=event_version,
    )
    logger.info(
        "assistant_run_enqueued",
        extra={"message_id": message.message_id, "run_id": run_id, "trace_id": canonical_trace_id},
    )
    return message


def recover_assistant_runs_sync() -> tuple[UUID, ...]:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        runs = PostgresConversationRunRepository(database)
        run_ids: tuple[UUID, ...] = loop.run_until_complete(runs.prepare_assistant_recovery())
        context_run_ids: tuple[UUID, ...] = loop.run_until_complete(
            runs.prepare_context_compaction_recovery()
        )
        run_ids = (*run_ids, *context_run_ids)
    finally:
        loop.close()
    for run_id in run_ids:
        enqueue_assistant_run(run_id=str(run_id), trace_id=new_trace_id(), event_version=2)
    if run_ids:
        logger.info("assistant_runs_recovered", extra={"run_count": len(run_ids)})
    return run_ids


__all__ = ["assistant_run", "enqueue_assistant_run", "recover_assistant_runs_sync"]
