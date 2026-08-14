"""API entry point for the Agent Knowledge Repository."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Literal, cast

from agent_runtime import PersonalSkillRegistry
from application.assistant import (
    AssistantCommandCatalog,
    AssistantCommandParser,
    AssistantCommandService,
    AssistantMetrics,
    AssistantSkillInvocationService,
    ConversationContextDataPort,
    ConversationContextService,
    ConversationReader,
    ConversationRunService,
    ConversationWorkspaceService,
    ReasoningProfileResolver,
)
from application.assistant.workspace import ConversationWorkspaceRepository
from application.exam_preparation import ExamArtifactGenerator, ExamPreparationService
from application.qa import (
    CitationResolver,
    PublishedCitationApplicationPort,
    PublishedCitationService,
)
from application.skills import (
    DerivedKnowledgeWriter,
    DraftSkillEvalRunner,
    PersonalSkillStore,
    SkillActivationStore,
    SkillCatalogPort,
    SkillDraftStore,
    SkillLifecycleService,
    SkillSuggestionService,
)
from domain.agent_runtime import ApprovalPort
from domain.agent_sse import AgentRunEventLog, AgentRunEventStore
from domain.assistant_sse import AssistantEventLog, AssistantEventStore
from domain.conversation_run import ConversationRunRepository
from domain.grounded_qa import CitationContentKind
from domain.qa_persistence import GroundedQARepository, QARunVersions
from domain.qa_sse import QAEventStore
from domain.retrieval import RetrievalProfileV1
from fastapi import FastAPI, Response
from infrastructure.agent_events import PostgresAgentRunEventStore
from infrastructure.assistant_events import PostgresAssistantEventStore
from infrastructure.assistant_resources import PostgresAssistantResourceResolver
from infrastructure.assistant_skill_projection import AssistantQASkillProjection
from infrastructure.blob_store import LocalFileBlobStore
from infrastructure.config import settings
from infrastructure.conversation_runs import PostgresConversationRunRepository
from infrastructure.database import Database
from infrastructure.exam_preparation import PostgresExamSessionRepository
from infrastructure.organization import PostgresKnowledgeOrganizationScope
from infrastructure.parsers import MarkdownParser, PdfParser
from infrastructure.qa import DatabaseSearchService, PostgresCitationTargetPort
from infrastructure.qa_execution import (
    assistant_skill_registry,
    qa_execution_versions,
    qa_skill_registry,
)
from infrastructure.qa_persistence import PostgresGroundedQARepository, PostgresQAEventStore
from infrastructure.runtime_approval import PostgresApprovalPort, PostgresDerivedKnowledgeStore
from infrastructure.skill_catalog import FileSystemSkillCatalog
from infrastructure.skill_lifecycle import (
    PostgresSkillActivationStore,
)
from infrastructure.skill_references import PostgresSkillReferenceChecker
from infrastructure.telemetry import configure_observability
from infrastructure.usage_traces import PostgresUsagePatternRepository
from infrastructure.workspaces import WorkspaceRoot
from model_gateway import (
    GatewayConfig,
    ModelGateway,
    ModelProvider,
    create_model_gateway,
)
from opentelemetry import trace
from opentelemetry.trace import SpanKind
from pydantic import BaseModel

from .assistant_runtime import AssistantWorkerDispatcher
from .errors import ErrorResponse, register_error_handlers
from .observability import TraceMiddleware
from .qa_runtime import QAWorkerDispatcher
from .routers import (
    agent_events,
    assistant,
    exam_preparation,
    personal_skills,
    qa,
    search,
    skill_drafts,
    skills,
    sources,
)


class LiveResponse(BaseModel):
    status: Literal["alive"]


class DependencyCheck(BaseModel):
    healthy: bool
    code: str


class ModelDependencyCheck(DependencyCheck):
    capabilities: dict[str, DependencyCheck]


class ReadinessChecks(BaseModel):
    postgresql: DependencyCheck
    redis: DependencyCheck
    model: ModelDependencyCheck


class ReadyResponse(BaseModel):
    status: Literal["ready", "degraded"]
    checks: ReadinessChecks


class LimitsResponse(BaseModel):
    max_upload_size_mb: int
    max_upload_size_bytes: int


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    500: {
        "model": ErrorResponse,
        "description": "Unexpected server error",
    }
}


def _active_skill_versions() -> dict[str, str]:
    """Return the Skills that may receive new durable activations."""
    return {
        "knowledge_agent": settings.knowledge_agent_skill_version,
        "summarize_document": "1.0.0",
        "compare_sources": "1.0.0",
        "create_review_cards": "1.0.0",
        "research_reading_workflow": "1.1.0",
        "exam_preparation_workflow": "1.2.1",
    }


def create_app(
    model_gateway: ModelGateway | None = None,
    *,
    database: Database | None = None,
    enable_qa_execution: bool = True,
    qa_repository: GroundedQARepository | None = None,
    conversation_run_repository: ConversationRunRepository | None = None,
    qa_event_store: QAEventStore | None = None,
    assistant_event_store: AssistantEventStore | None = None,
    agent_event_store: AgentRunEventStore | None = None,
    assistant_runtime: AssistantWorkerDispatcher | None = None,
    qa_citation_service: PublishedCitationApplicationPort | None = None,
    skill_catalog: SkillCatalogPort | None = None,
    skill_activation_store: SkillActivationStore | None = None,
    approval_port: ApprovalPort | None = None,
    derived_knowledge_store: DerivedKnowledgeWriter | None = None,
) -> FastAPI:
    """Application factory. Call once at process start."""

    database = database or Database(settings.database_url)
    uses_external_qa_repository = qa_repository is not None
    gateway = model_gateway or _create_configured_model_gateway()
    reasoning = ReasoningProfileResolver(gateway=gateway)
    qa_repository = qa_repository or PostgresGroundedQARepository(database)
    if conversation_run_repository is None:
        parent_methods = (
            "create_turn",
            "create_context_compaction_run",
            "get_conversation_run",
            "list_conversation_runs",
            "request_conversation_cancel",
            "reopen_clarification",
            "prepare_conversation_recovery",
            "prepare_assistant_recovery",
            "prepare_context_compaction_recovery",
            "claim_conversation_run",
            "renew_conversation_run_lease",
            "release_conversation_run_lease",
            "promote_to_skill",
            "publish_direct_message",
            "publish_clarification",
            "fail_conversation_run",
            "cancel_conversation_run",
            "complete_context_compaction",
        )
        conversation_run_repository = (
            cast(ConversationRunRepository, qa_repository)
            if all(hasattr(qa_repository, method) for method in parent_methods)
            else PostgresConversationRunRepository(database)
        )
    assistant_turn_service = ConversationRunService(
        conversations=cast(ConversationReader, qa_repository),
        runs=conversation_run_repository,
        reasoning=reasoning,
    )
    assistant_metrics = AssistantMetrics()
    skill_registry = qa_skill_registry()
    active_skill_versions = _active_skill_versions()
    activation_store = skill_activation_store or PostgresSkillActivationStore(database)
    skill_lifecycle = SkillLifecycleService(
        registry=skill_registry,
        store=activation_store,
        defaults=active_skill_versions,
    )
    skill_catalog = skill_catalog or FileSystemSkillCatalog(
        skill_registry,
        include_manifest_v2=True,
        visible_names=frozenset(
            {
                "knowledge_agent",
                "summarize_document",
                "compare_sources",
                "create_review_cards",
                "research_reading_workflow",
                "exam_preparation_workflow",
            }
        ),
    )
    assistant_registry = assistant_skill_registry()
    assistant_catalog = FileSystemSkillCatalog(assistant_registry, include_manifest_v2=True)
    exam_repository = PostgresExamSessionRepository(database)
    app_exam_service = ExamPreparationService(
        exam_repository,
        ExamArtifactGenerator(
            search=DatabaseSearchService(database, gateway),
            gateway=gateway,
            profile=RetrievalProfileV1(
                embedding_version=settings.active_embedding_identity(
                    allow_unconfigured=True
                ).version
            ),
        ),
    )
    qa_event_log = qa_event_store or PostgresQAEventStore(database)
    if assistant_event_store is not None:
        assistant_event_log = assistant_event_store
    elif id(conversation_run_repository) == id(qa_repository):
        assistant_event_log = AssistantEventLog()
    else:
        assistant_event_log = PostgresAssistantEventStore(database)
    if agent_event_store is not None:
        agent_event_log = agent_event_store
    elif id(conversation_run_repository) == id(qa_repository):
        agent_event_log = AgentRunEventLog()
    else:
        agent_event_log = PostgresAgentRunEventStore(database)
    assistant_runtime = assistant_runtime or AssistantWorkerDispatcher(
        repository=conversation_run_repository
    )
    conversation_context = ConversationContextService(
        data=cast(ConversationContextDataPort, qa_repository),
        runs=conversation_run_repository,
        reasoning=reasoning,
    )
    workspace_service = ConversationWorkspaceService(
        conversations=cast(ConversationWorkspaceRepository, qa_repository),
        resolve_path=WorkspaceRoot(settings.agent_workspace_root),
        list_runs=conversation_run_repository.list_conversation_runs,
    )
    qa_runtime = QAWorkerDispatcher(
        repository=qa_repository,
        skill_registry=skill_registry,
        skill_lifecycle=skill_lifecycle,
    )

    async def _assistant_versions(skill_name: str) -> QARunVersions:
        return qa_execution_versions(assistant_registry, skill_name=skill_name)

    projection = AssistantQASkillProjection(
        repository=qa_repository,
        parent_runs=conversation_run_repository,
        versions=_assistant_versions,
        start=qa_runtime.start if enable_qa_execution else lambda _run_id: False,
    )
    assistant_skill_invoker = AssistantSkillInvocationService(
        runs=conversation_run_repository,
        catalog=assistant_catalog,
        registry=assistant_registry,
        projection=projection,
        resources=PostgresAssistantResourceResolver(database),
    )
    personal_skill_store = PersonalSkillStore(
        registry=assistant_registry,
        store=activation_store,
    )
    skill_draft_store = SkillDraftStore(
        registry=assistant_registry,
        personal_store=personal_skill_store,
        eval_runner=DraftSkillEvalRunner(registry=assistant_registry),
    )
    skill_suggestion_service = SkillSuggestionService(
        patterns=PostgresUsagePatternRepository(database)
    )
    assistant_command_catalog = AssistantCommandCatalog(assistant_catalog)
    assistant_command_service = AssistantCommandService(
        catalog=assistant_command_catalog,
        parser=AssistantCommandParser(assistant_command_catalog),
        turns=assistant_turn_service,
        runs=conversation_run_repository,
        conversations=qa_repository,
        qa=qa_repository,
        skill_invoker=assistant_skill_invoker,
        context=conversation_context,
        metrics=assistant_metrics,
        reasoning=reasoning,
        workspace=workspace_service,
    )
    qa_citation_service = qa_citation_service or PublishedCitationService(
        runs=qa_repository,
        resolver=CitationResolver(
            targets=PostgresCitationTargetPort(database),
            blob_store=LocalFileBlobStore(),
            parsers={
                CitationContentKind.TEXT: MarkdownParser(),
                CitationContentKind.PDF: PdfParser(),
            },
        ),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        settings.validate_secrets()
        observability = configure_observability(settings, service_name="api")
        database.instrument()
        try:
            if enable_qa_execution:
                for active_skill in active_skill_versions:
                    await skill_lifecycle.current(active_skill)
                await _activate_personal_skills(assistant_registry, activation_store)
                await qa_runtime.recover()
                await assistant_runtime.recover()
            yield
        finally:
            await database.dispose()
            await gateway.aclose()
            await asyncio.to_thread(
                observability.provider.force_flush,
                int(settings.otel_export_timeout_seconds * 1000),
            )

    app = FastAPI(
        title="Agent Knowledge Repository",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.database = database
    app.state.model_gateway = gateway
    app.state.qa_repository = qa_repository
    app.state.conversation_run_repository = conversation_run_repository
    app.state.assistant_turn_service = assistant_turn_service
    app.state.assistant_command_service = assistant_command_service
    app.state.assistant_skill_invoker = assistant_skill_invoker
    app.state.conversation_context_service = conversation_context
    app.state.workspace_service = workspace_service
    app.state.assistant_event_log = assistant_event_log
    app.state.agent_event_log = agent_event_log
    app.state.assistant_metrics = assistant_metrics
    app.state.assistant_runtime = assistant_runtime
    app.state.qa_event_log = qa_event_log
    app.state.qa_runtime = qa_runtime
    app.state.qa_citation_service = qa_citation_service
    app.state.qa_execution_enabled = enable_qa_execution
    app.state.skill_catalog = skill_catalog
    app.state.skill_registry = skill_registry
    app.state.assistant_registry = assistant_registry
    app.state.exam_repository = exam_repository
    app.state.exam_continuation_enabled = not uses_external_qa_repository
    app.state.exam_service = app_exam_service
    app.state.skill_reference_checker = PostgresSkillReferenceChecker(database)
    app.state.skill_lifecycle = skill_lifecycle
    app.state.personal_skill_store = personal_skill_store
    app.state.skill_draft_store = skill_draft_store
    app.state.skill_suggestion_service = skill_suggestion_service
    app.state.organization_scope = PostgresKnowledgeOrganizationScope(database)
    app.state.approval_port = approval_port or PostgresApprovalPort(database)
    app.state.derived_knowledge_store = derived_knowledge_store or PostgresDerivedKnowledgeStore(
        database
    )

    app.add_middleware(TraceMiddleware)
    register_error_handlers(app)
    _register_routes(app)
    return app


def _register_routes(app: FastAPI) -> None:
    app.include_router(sources.router)
    app.include_router(search.router)
    app.include_router(qa.router)
    app.include_router(assistant.router)
    app.include_router(agent_events.router)
    app.include_router(skills.router)
    app.include_router(exam_preparation.router)
    app.include_router(exam_preparation.v4_router)
    # Draft routes must precede the personal `/{name}` routes so a draft named
    # "drafts" or the "/suggestions" path is never captured as a skill name.
    app.include_router(skill_drafts.router)
    app.include_router(personal_skills.router)

    @app.get(
        "/api/v1/health/live",
        response_model=LiveResponse,
        responses=ERROR_RESPONSES,
    )
    async def live() -> LiveResponse:
        """Liveness probe — process event loop is responsive."""
        return LiveResponse(status="alive")

    @app.get(
        "/api/v1/health/ready",
        response_model=ReadyResponse,
        responses={
            503: {
                "model": ReadyResponse,
                "description": "One or more required dependencies are unavailable",
            },
            **ERROR_RESPONSES,
        },
    )
    async def ready(response: Response) -> ReadyResponse:
        """Readiness probe — concurrent dependency check with stable machine codes."""

        async def _check_postgres() -> DependencyCheck:
            if await app.state.database.is_available(timeout_seconds=3):
                return DependencyCheck(healthy=True, code="POSTGRESQL_OK")
            return DependencyCheck(healthy=False, code="POSTGRESQL_UNREACHABLE")

        async def _check_redis() -> DependencyCheck:
            import redis.asyncio as aioredis  # noqa: PLC0415

            tracer = trace.get_tracer("api.dependencies")
            with tracer.start_as_current_span(
                "redis.ping",
                kind=SpanKind.CLIENT,
                attributes={"db.system.name": "redis", "server.address": settings.redis_host},
            ):
                try:
                    r = aioredis.from_url(settings.redis_url, socket_timeout=3)
                    await r.ping()
                    await r.aclose()
                    return DependencyCheck(healthy=True, code="REDIS_OK")
                except Exception:
                    return DependencyCheck(healthy=False, code="REDIS_UNREACHABLE")

        pg_result, redis_result = await asyncio.gather(_check_postgres(), _check_redis())
        model_status = app.state.model_gateway.status
        model_result = ModelDependencyCheck(
            healthy=model_status.available,
            code=model_status.code,
            capabilities={
                capability.capability.value: DependencyCheck(
                    healthy=capability.available,
                    code=capability.code,
                )
                for capability in model_status.capability_statuses
            },
        )

        all_healthy = pg_result.healthy and redis_result.healthy
        if not all_healthy:
            response.status_code = 503

        return ReadyResponse(
            status="ready" if all_healthy else "degraded",
            checks=ReadinessChecks(
                postgresql=pg_result,
                redis=redis_result,
                model=model_result,
            ),
        )

    @app.get(
        "/api/v1/config/limits",
        response_model=LimitsResponse,
        responses=ERROR_RESPONSES,
    )
    async def config_limits() -> LimitsResponse:
        """Client-facing upload limits from the live configuration."""
        return LimitsResponse(
            max_upload_size_mb=settings.max_upload_size_mb,
            max_upload_size_bytes=settings.max_upload_size_mb * 1024 * 1024,
        )


async def _activate_personal_skills(
    registry: PersonalSkillRegistry,
    activation_store: SkillActivationStore,
) -> None:
    """Re-apply persisted personal-Skill activations after a fresh registry scan."""
    persisted = await activation_store.list()
    activations = {
        item.name: item.version
        for item in persisted
        if registry.is_personal(item.name) and item.version in registry.versions(item.name)
    }
    registry.activate_all(activations)


def _create_configured_model_gateway() -> ModelGateway:
    api_key = settings.model_api_key.get_secret_value() if settings.model_api_key else None
    fast_chat_api_key = (
        settings.fast_chat_api_key.get_secret_value() if settings.fast_chat_api_key else None
    )
    embedding_api_key = (
        settings.embedding_api_key.get_secret_value() if settings.embedding_api_key else None
    )
    reranker_api_key = (
        settings.reranker_api_key.get_secret_value() if settings.reranker_api_key else None
    )
    return create_model_gateway(
        GatewayConfig(
            provider=ModelProvider(settings.model_provider),
            endpoint=settings.model_endpoint,
            api_key=api_key,
            fast_chat_endpoint=settings.fast_chat_endpoint,
            fast_chat_api_key=fast_chat_api_key,
            fast_chat_model=settings.fast_chat_model,
            embedding_endpoint=settings.embedding_endpoint,
            embedding_api_key=embedding_api_key,
            embedding_model=settings.embedding_model,
            reranker_endpoint=settings.reranker_endpoint,
            reranker_api_key=reranker_api_key,
            reranker_model=settings.reranker_model,
            embedding_protocol=settings.embedding_protocol,
            embedding_provider=(
                ModelProvider.TEXT_EMBEDDINGS_INFERENCE
                if settings.embedding_provider == "text-embeddings-inference"
                else None
            ),
            fake_embedding=settings.embedding_provider == "fake",
            fake_reranker=settings.reranker_provider == "fake",
            allow_external=settings.model_allow_external,
            timeout_seconds=settings.model_timeout_seconds,
            fast_chat_timeout_seconds=settings.fast_chat_timeout_seconds,
            fast_chat_reasoning_enabled=settings.fast_chat_reasoning_enabled,
            fast_chat_prompt_caching=settings.fast_chat_prompt_caching,
            max_retries=settings.model_max_retries,
            retry_backoff_seconds=settings.model_retry_backoff_seconds,
            reranker_batch_size=settings.reranker_batch_size,
        )
    )


app = create_app()
