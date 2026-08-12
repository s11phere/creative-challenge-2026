"""Shared Grounded QA execution assembly for the independent Worker."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from uuid import UUID

from agent_runtime import (
    AgentLoopExecutor,
    AgentLoopResult,
    DeterministicWorkflowExecutor,
    FileSystemSkillRegistry,
    PersonalSkillRegistry,
    RuntimeExecutionResult,
    SkillRegistryError,
)
from application.qa import (
    ContextBuilder,
    EvidenceBindingService,
    EvidenceVerifier,
    GroundedAnswerGenerator,
    GroundedQAExecutionProfile,
    GroundedQAService,
    LlmQueryRewriter,
    QAGenerationProfileV1,
    QAPlanningProfileV1,
    QASearchCoordinator,
    QueryPlanner,
    StructuredAnswerParser,
)
from application.skills import (
    DerivedKnowledgeWriter,
    GroundedQASkillAdapter,
    GroundedQASkillConfig,
    KnowledgeLoopTools,
    KnowledgeLoopToolsConfig,
)
from domain.agent_runtime import AgentRun, AgentRunContext, ApprovalPort, RunStatus
from domain.agent_sse import AgentRunEventStore
from domain.grounded_qa import QAErrorCode, QAEvent, QAStatus
from domain.qa_persistence import (
    GroundedQARepository,
    MessageRole,
    QARunRecord,
    QARunVersions,
)
from domain.qa_sse import QAEventStore, QAEventType
from domain.reasoning import ReasoningProfile
from domain.retrieval import RetrievalProfileV1
from model_gateway import (
    CapabilityAlias,
    ChatRequest,
    ChatResponse,
    EmbeddingRequest,
    EmbeddingResponse,
    FakeModelGateway,
    GatewayStatus,
    ModelGateway,
    ModelUsage,
    RerankRequest,
    RerankResponse,
)

from .config import settings
from .conversation_runs import PostgresConversationRunRepository
from .database import Database
from .qa import DatabaseSearchService, PostgresCitationTargetPort
from .qa_debug_trace import QADebugTrace, TracingModelGateway, TracingToolRegistry
from .runtime_state import PostgresRuntimeStateStore

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[4]
_SCHEMA = _ROOT / "cases/evals/configs/grounded-answer-v1.schema.json"
_RESEARCH_SCHEMA = _ROOT / "cases/evals/configs/research-grounded-answer-v2.schema.json"
_PROMPT = _ROOT / "cases/evals/prompts/grounded-qa-v1-provisional.txt"
_EVIDENCE = re.compile(
    r'<evidence id="(?P<evidence_id>[0-9a-f-]+)"(?P<attributes>[^>]*)>\s*'
    r"<<<UNTRUSTED_EVIDENCE>>>\s*(?P<text>.*?)\s*<<<END_UNTRUSTED_EVIDENCE>>>",
    re.DOTALL,
)
_TERMINAL = frozenset(
    {QAStatus.COMPLETED, QAStatus.REFUSED, QAStatus.FAILED, QAStatus.CANCELLED, QAStatus.TIMED_OUT}
)


class StructuredFakeGateway:
    """Make the default fake provider return a minimal grounded extractive answer."""

    def __init__(self, delegate: ModelGateway) -> None:
        self._delegate = delegate

    @property
    def status(self) -> GatewayStatus:
        return self._delegate.status

    async def chat(
        self,
        request: ChatRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.FAST_CHAT,
    ) -> ChatResponse:
        if not isinstance(self._delegate, FakeModelGateway):
            return await self._delegate.chat(request, capability=capability)
        user_content = request.messages[-1].content
        matches = tuple(_EVIDENCE.finditer(user_content))
        if not matches:
            payload: dict[str, object] = {
                "schema_version": "grounded-answer-v1",
                "result_type": "refuse",
                "reason": "insufficient_evidence",
                "message": "No usable evidence was retrieved.",
                "limitations": ["Provisional local answer mode."],
            }
        else:
            evidence_id = matches[0].group("evidence_id")
            raw_text = matches[0].group("text")
            claim = " ".join(raw_text.split())[:1200]
            instructions = "\n".join(item.content for item in request.messages).casefold()
            document_ids = {
                document_match.group(1)
                for item in matches
                if (
                    document_match := re.search(
                        r'document_id="([0-9a-f-]+)"', item.group("attributes")
                    )
                )
            }
            if len(document_ids) >= 2 or "evidence matrix" in instructions:
                payload = _fake_research_answer(matches, mode="literature_review")
            elif "common misconceptions" in instructions:
                payload = _fake_research_answer(matches, mode="deep_read")
            else:
                payload = {
                    "schema_version": "grounded-answer-v1",
                    "result_type": "answer",
                    "answer": claim,
                    "claims": [
                        {
                            "claim_id": "extractive-1",
                            "text": claim,
                            "evidence_ids": [evidence_id],
                        }
                    ],
                    "limitations": [
                        "Deterministic extractive fallback; answer quality is not formally frozen."
                    ],
                }
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return ChatResponse(
            text=text,
            finish_reason="stop",
            usage=ModelUsage(
                input_tokens=sum(max(1, len(item.content.split())) for item in request.messages),
                output_tokens=max(1, len(text.split())),
            ),
            capability=capability,
            latency_ms=0.0,
        )

    async def embed(
        self,
        request: EmbeddingRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.EMBEDDING_ZH,
    ) -> EmbeddingResponse:
        return await self._delegate.embed(request, capability=capability)

    async def rerank(
        self,
        request: RerankRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.RERANKER_MULTILINGUAL,
    ) -> RerankResponse:
        return await self._delegate.rerank(request, capability=capability)

    async def aclose(self) -> None:
        return None


def _fake_research_answer(matches: tuple[re.Match[str], ...], *, mode: str) -> dict[str, object]:
    """Return a readable, citation-backed local preview without claiming model-level quality."""
    excerpts: list[tuple[str, str, str]] = []
    seen_documents: set[str] = set()
    for match in matches:
        document_match = re.search(r'document_id="([0-9a-f-]+)"', match.group("attributes"))
        document_id = document_match.group(1) if document_match else match.group("evidence_id")
        if document_id in seen_documents:
            continue
        seen_documents.add(document_id)
        excerpts.append(
            (
                match.group("evidence_id"),
                document_id,
                " ".join(match.group("text").split())[:900],
            )
        )
    if mode == "deep_read":
        evidence_id, _document_id, excerpt = excerpts[0]
        item = {"text": excerpt, "evidence_ids": [evidence_id]}
        return {
            "schema_version": "research-grounded-answer-v2",
            "result_type": "answer",
            "mode": "research_deep_read",
            "research_question": item,
            "contributions": [item],
            "method_explanation": [
                {
                    "text": "Identify the intervention, baseline, and controlled conditions "
                    "before interpreting the reported result.",
                    "evidence_ids": [evidence_id],
                }
            ],
            "data_and_metrics": [item],
            "results": [item],
            "paper_limitations": [item],
            "misconceptions": [
                {
                    "text": "A retrieval improvement is not automatically an improvement in "
                    "final-answer quality.",
                    "evidence_ids": [evidence_id],
                }
            ],
            "follow_up_questions": [
                "Which assumptions, evaluation settings, and missing evidence should be "
                "checked next?"
            ],
            "limitations": [
                "Deterministic fake-provider preview; development/provisional quality only."
            ],
        }
    evidence_ids = [item[0] for item in excerpts]
    observations = [
        {
            "paper_label": f"Paper {index}",
            "text": excerpt[:500],
            "evidence_ids": [evidence_id],
        }
        for index, (evidence_id, _document_id, excerpt) in enumerate(excerpts, 1)
    ]
    combined = {
        "text": "The selected papers study evidence retrieval under different methods "
        "and evaluation conditions.",
        "evidence_ids": evidence_ids,
    }
    return {
        "schema_version": "research-grounded-answer-v2",
        "result_type": "answer",
        "mode": "research_literature_review",
        "paper_briefs": [
            {
                "paper_label": f"Paper {index}",
                "text": excerpt,
                "evidence_ids": [evidence_id],
            }
            for index, (evidence_id, _document_id, excerpt) in enumerate(excerpts, 1)
        ],
        "evidence_matrix": [
            {
                "dimension": dimension,
                "observations": observations,
                "synthesis": synthesis,
                "evidence_ids": evidence_ids,
                "comparability": comparability,
            }
            for dimension, synthesis, comparability in (
                (
                    "Research question",
                    "Both papers address grounded retrieval behavior.",
                    "comparable",
                ),
                (
                    "Method",
                    "The papers use distinct retrieval interventions.",
                    "conditionally_comparable",
                ),
                (
                    "Evaluation",
                    "Different metrics or datasets must not be directly ranked.",
                    "not_comparable",
                ),
            )
        ],
        "thematic_review": [
            {"theme": "Retrieval strategy", **combined},
            {"theme": "Evaluation boundaries", **combined},
        ],
        "consensus": [combined],
        "apparent_differences": [combined],
        "genuine_conflicts": [],
        "evidence_gaps": ["The selected evidence does not establish downstream answer quality."],
        "limitations": [
            "Deterministic fake-provider preview; development/provisional quality only."
        ],
    }


class StructuredAgentGateway:
    """Return deterministic Agent decisions only for the default fake provider."""

    def __init__(self, delegate: ModelGateway) -> None:
        self._delegate = delegate

    @property
    def status(self) -> GatewayStatus:
        return self._delegate.status

    async def chat(
        self,
        request: ChatRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.FAST_CHAT,
    ) -> ChatResponse:
        if not isinstance(self._delegate, FakeModelGateway):
            return await self._delegate.chat(request, capability=capability)
        has_tool_result = '"tool_result"' in request.messages[-1].content
        payload = (
            {"action": "complete", "reason": "Grounded QA completed."}
            if has_tool_result
            else {"action": "call_tool", "tool_name": "grounded_qa", "arguments": {}}
        )
        text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        return ChatResponse(
            text=text,
            finish_reason="stop",
            usage=ModelUsage(
                input_tokens=sum(max(1, len(item.content.split())) for item in request.messages),
                output_tokens=max(1, len(text.split())),
            ),
            capability=capability,
            latency_ms=0.0,
        )

    async def embed(
        self,
        request: EmbeddingRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.EMBEDDING_ZH,
    ) -> EmbeddingResponse:
        return await self._delegate.embed(request, capability=capability)

    async def rerank(
        self,
        request: RerankRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.RERANKER_MULTILINGUAL,
    ) -> RerankResponse:
        return await self._delegate.rerank(request, capability=capability)

    async def aclose(self) -> None:
        return None


class StructuredKnowledgeLoopGateway(StructuredAgentGateway):
    """Drive the opt-in generic knowledge Loop for the deterministic fake provider only."""

    async def chat(
        self,
        request: ChatRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.FAST_CHAT,
    ) -> ChatResponse:
        if not isinstance(self._delegate, FakeModelGateway):
            return await self._delegate.chat(request, capability=capability)
        payload = _knowledge_loop_decision(request.messages[-1].content)
        text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        return ChatResponse(
            text=text,
            finish_reason="stop",
            usage=ModelUsage(
                input_tokens=sum(max(1, len(item.content.split())) for item in request.messages),
                output_tokens=max(1, len(text.split())),
            ),
            capability=capability,
            latency_ms=0.0,
        )


class StructuredAssistantLoopGateway(StructuredAgentGateway):
    """Provide deterministic multi-turn Tool decisions for local fake Assistant runs."""

    async def chat(
        self,
        request: ChatRequest,
        *,
        capability: CapabilityAlias = CapabilityAlias.FAST_CHAT,
    ) -> ChatResponse:
        if not isinstance(self._delegate, FakeModelGateway):
            return await self._delegate.chat(request, capability=capability)
        payload = _assistant_loop_decision(request.messages[-1].content)
        text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        return ChatResponse(
            text=text,
            finish_reason="stop",
            usage=ModelUsage(
                input_tokens=sum(max(1, len(item.content.split())) for item in request.messages),
                output_tokens=max(1, len(text.split())),
            ),
            capability=capability,
            latency_ms=0.0,
        )


def _knowledge_loop_decision(content: str) -> dict[str, str | dict[str, str]]:
    """Return deterministic decisions for local FakeModelGateway development runs."""
    try:
        request = json.loads(content)
        state = request.get("state", {})
        input_data = request.get("input", {})
        observations = state.get("observations", [])
    except (TypeError, ValueError):
        observations = []
        input_data = {}
    last = observations[-1] if observations else {}
    last_tool = last.get("tool_name") if isinstance(last, dict) else None
    if last_tool is None:
        query = (
            input_data.get("question", "knowledge request")
            if isinstance(input_data, dict)
            else "knowledge request"
        )
        return {
            "action": "call_tool",
            "tool_name": "knowledge_search",
            "arguments": {"query": query},
        }
    next_tools = {
        "knowledge_search": "knowledge_inspect",
        "knowledge_inspect": "grounded_answer",
        "grounded_answer": "verify_answer",
        "verify_answer": "finalize_answer",
    }
    next_tool = next_tools.get(last_tool)
    if next_tool is not None:
        return {"action": "call_tool", "tool_name": next_tool, "arguments": {}}
    output = last.get("output", {}) if isinstance(last, dict) else {}
    outcome = output.get("outcome") if isinstance(output, dict) else None
    if outcome in {"refuse", "conflict"}:
        return {"action": "refuse", "reason": "Grounded QA verified a safe terminal refusal."}
    return {"action": "complete", "reason": "Grounded QA verified the current Run."}


def _assistant_loop_decision(content: str) -> dict[str, object]:
    """Use Tool-provided next-step metadata, while keeping ordinary fake turns direct."""
    try:
        request = json.loads(content)
        state = request.get("state", {})
        input_data = request.get("input", {})
        observations = state.get("observations", [])
    except (TypeError, ValueError):
        observations = []
        input_data = {}
    question = input_data.get("question", "") if isinstance(input_data, dict) else ""
    workspace = input_data.get("workspace") if isinstance(input_data, dict) else None
    workspace_enabled = isinstance(workspace, dict) and workspace.get("tools_enabled") is True
    last = observations[-1] if observations else {}
    if not isinstance(last, dict) or not last.get("tool_name"):
        research = _fake_research_request(question)
        if research is not None:
            return research
        if _requires_fake_knowledge_tool(question):
            return {
                "action": "call_tool",
                "tool_name": "knowledge_search",
                "arguments": {"query": _fake_knowledge_query(question)},
            }
        return {
            "action": "complete",
            "reason": "The request does not require current-Space knowledge.",
            "final_response": "fake-response-autonomous",
        }
    output = last.get("output", {})
    recommended = output.get("recommended_next") if isinstance(output, dict) else None
    last_tool = last.get("tool_name")
    if workspace_enabled and _requires_fake_workspace_artifact(question):
        if (
            last_tool == "finalize_answer"
            and isinstance(output, dict)
            and output.get("ready") is True
            and output.get("outcome") not in {"refuse", "conflict"}
        ):
            return {"action": "call_tool", "tool_name": "fs_list", "arguments": {"path": "."}}
        if last_tool == "fs_list":
            return {
                "action": "call_tool",
                "tool_name": "fs_write",
                "arguments": {
                    "path": _fake_markdown_path(question, output),
                    "content": "{{current_grounded_qa_answer}}",
                },
            }
    if recommended in {
        "knowledge_search",
        "knowledge_inspect",
        "grounded_answer",
        "verify_answer",
        "finalize_answer",
    }:
        arguments: dict[str, str] = {}
        if recommended == "knowledge_search":
            arguments["query"] = _fake_knowledge_query(question)
        return {"action": "call_tool", "tool_name": recommended, "arguments": arguments}
    if recommended == "clarify":
        return {"action": "clarify", "reason": "Please confirm or correct the paper selection."}
    if recommended == "refuse":
        return {"action": "refuse", "reason": "Grounded QA verified a safe terminal refusal."}
    if recommended == "complete":
        return {"action": "complete", "reason": "Grounded QA verified the current Run."}
    next_tools = {
        "knowledge_search": "knowledge_inspect",
        "knowledge_inspect": "grounded_answer",
        "grounded_answer": "verify_answer",
        "verify_answer": "finalize_answer",
    }
    next_tool = next_tools.get(last_tool) if isinstance(last_tool, str) else None
    if next_tool is not None:
        return {"action": "call_tool", "tool_name": next_tool, "arguments": {}}
    return {"action": "complete", "reason": "Grounded QA verified the current Run."}


def _fake_research_request(question: object) -> dict[str, object] | None:
    """Deterministic synthetic routing only; production providers decide from Tool schemas."""
    if not isinstance(question, str):
        return None
    normalized = question.casefold()
    if "/research" not in normalized and not any(
        marker in normalized
        for marker in ("论文精读", "文献综述", "literature review", "deep read")
    ):
        return None
    file_references = tuple(
        dict.fromkeys(
            match.strip("\"'“”‘’()[]{}")
            for match in re.findall(r"(?<![\w./-])([^\s,，、;；]+\.(?:pdf|md|txt))", question, re.I)
            if match.strip("\"'“”‘’()[]{}")
        )
    )
    named_references = tuple(
        dict.fromkeys(
            match.strip()
            for match in re.findall(
                r"(?:paper|论文)\s*([a-z0-9][a-z0-9 ._-]{0,80})", question, re.I
            )
            if match.strip()
        )
    )
    references = file_references or named_references
    if len(references) >= 2:
        return {
            "action": "call_tool",
            "tool_name": "research_prepare",
            "arguments": {
                "mode": "literature_review",
                "document_references": list(references[:8]),
            },
        }
    if len(references) == 1:
        return {
            "action": "call_tool",
            "tool_name": "research_prepare",
            "arguments": {"mode": "deep_read", "document_references": list(references)},
        }
    topic = re.sub(r"^\s*/(?:research|literature)\s*", "", question, flags=re.I).strip()
    return {
        "action": "call_tool",
        "tool_name": "research_discover",
        "arguments": {"topic": topic[:512] or "research topic"},
    }


def _requires_fake_knowledge_tool(question: object) -> bool:
    if not isinstance(question, str):
        return False
    normalized = question.casefold()
    markers = (
        "knowledge base",
        "current space",
        "current workspace",
        "uploaded notes",
        "uploaded file",
        "uploaded document",
        "in the document",
        "from the document",
        "citation",
        "knowledge_agent",
    )
    if any(marker in normalized for marker in markers):
        return True
    informational_markers = (
        "what is",
        "what are",
        "which modules",
        "main modules",
        "overview",
        "describe",
        "explain",
        "介绍",
        "说明",
        "概述",
        "总结",
        "主要模块",
        "有哪些",
    )
    return bool(
        any(marker in normalized for marker in informational_markers)
        and re.search(r"[a-z][a-z0-9.-]{1,}|[\u4e00-\u9fff]{2,}", normalized)
    )


def _fake_knowledge_query(question: object) -> str:
    if not isinstance(question, str):
        return "current Space knowledge request"
    query = re.split(
        r"\s*(?:and then|and|并|然后)?\s*(?:save|write|store|保存|写入|存为)",
        question,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return query.rstrip(" ,，;；。.!！\t\r\n").strip()[:512] or "current Space knowledge request"


def _requires_fake_workspace_artifact(question: object) -> bool:
    if not isinstance(question, str):
        return False
    normalized = question.casefold()
    return bool(
        re.search(r"(?:save|write|store|保存|写入|存为)", normalized)
        and re.search(r"(?:markdown|\.md\b|md文件|md 文件)", normalized)
    )


def _fake_markdown_path(question: object, output: object) -> str:
    normalized = question.casefold() if isinstance(question, str) else ""
    subject = re.search(r"[a-z][a-z0-9.-]{1,}", normalized)
    slug = subject.group(0) if subject is not None else "knowledge"
    suffix = "-modules" if "module" in normalized or "模块" in normalized else "-overview"
    base = f"{slug}{suffix}.md"
    existing: set[str] = set()
    if isinstance(output, dict) and isinstance(output.get("entries"), list):
        for entry in output["entries"]:
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                existing.add(entry["path"].lstrip("./"))
    if base not in existing:
        return base
    stem = base[:-3]
    for index in range(2, 100):
        candidate = f"{stem}-{index}.md"
        if candidate not in existing:
            return candidate
    return f"{slug}-notes.md"


def qa_execution_versions(
    skill_registry: FileSystemSkillRegistry | None = None,
    *,
    skill_name: str = "knowledge_agent",
) -> QARunVersions:
    planning, retrieval, generation = _profiles()
    registry = skill_registry or qa_skill_registry()
    pin = registry.pin(skill_name)
    return QARunVersions(
        skill_name=pin.name,
        skill_version=pin.version,
        skill_content_sha256=pin.content_sha256,
        profile_version=planning.profile_id,
        retrieval_profile_version=retrieval.profile_version,
        model_identity=generation.model_identity,
        prompt_version=generation.prompt_template_id,
        output_schema_version=generation.structured_output_schema,
        corpus_version="local-live-provisional",
        dataset_version="knowledge-qa-v0-provisional",
    )


def qa_skill_registry() -> PersonalSkillRegistry:
    """Load all trusted and personal Skills and activate the knowledge entry point."""
    registry = PersonalSkillRegistry(
        Path(settings.skill_root_path),
        personal_root=Path(settings.personal_skills_dir),
    )
    registry.reload()
    registry.activate("knowledge_agent", settings.knowledge_agent_skill_version)
    return registry


def assistant_skill_registry() -> PersonalSkillRegistry:
    """Build the v2 invocation catalog with knowledge_agent as the sole QA entry."""
    registry = qa_skill_registry()
    for name, version in (
        ("summarize_document", "1.0.0"),
        ("compare_sources", "1.0.0"),
        ("create_review_cards", "1.0.0"),
        ("research_reading_workflow", "1.1.0"),
    ):
        registry.activate(name, version)
    return registry


class GroundedQAExecutor:
    """Run the single QA Application Port and publish privacy-safe terminal events."""

    def __init__(
        self,
        *,
        database: Database,
        gateway: ModelGateway,
        repository: GroundedQARepository,
        events: QAEventStore,
        agent_events: AgentRunEventStore | None = None,
        skill_registry: FileSystemSkillRegistry | None = None,
        approval_port: ApprovalPort | None = None,
        approval_id: str | None = None,
        derived_writer: DerivedKnowledgeWriter | None = None,
    ) -> None:
        self._database = database
        planning, retrieval, generation = _profiles()
        self.profile = GroundedQAExecutionProfile(planning=planning, retrieval=retrieval)
        self._gateway = gateway
        self._repository = repository
        self._events = events
        self._agent_events = agent_events
        self._skill_registry = skill_registry
        self._approval_port = approval_port
        self._approval_id = approval_id
        self._derived_writer = derived_writer
        self._generation = generation

    def build_service(
        self,
        gateway: ModelGateway,
        *,
        generation_gateway: ModelGateway,
    ) -> GroundedQAService:
        return GroundedQAService(
            repository=self._repository,
            planner=QueryPlanner(rewriter=LlmQueryRewriter(generation_gateway)),
            search=QASearchCoordinator(DatabaseSearchService(self._database, gateway)),
            evidence_binding=EvidenceBindingService(),
            context_builder=ContextBuilder(),
            generator=GroundedAnswerGenerator(
                gateway=generation_gateway,
                parser=StructuredAnswerParser(
                    json.loads(_SCHEMA.read_text(encoding="utf-8")),
                    research_schema=json.loads(_RESEARCH_SCHEMA.read_text(encoding="utf-8")),
                ),
                verifier=EvidenceVerifier(PostgresCitationTargetPort(self._database)),
                profile=self._generation,
                prompt_contract=_PROMPT.read_text(encoding="utf-8"),
                corpus_version="local-live-provisional",
                dataset_version="knowledge-qa-v0-provisional",
            ),
        )

    async def execute(self, run_id: UUID, *, trace_id: str) -> QARunRecord | None:
        await self._events.append(run_id, QAEventType.STARTED, {"status": QAStatus.RUNNING.value})
        trace = QADebugTrace.from_settings(run_id=run_id, trace_id=trace_id, settings=settings)
        initial = await self._repository.get_run(run_id)
        await trace.record(
            "run_started",
            skill_name=initial.versions.skill_name if initial is not None else None,
            skill_version=initial.versions.skill_version if initial is not None else None,
        )
        resolved_run: QARunRecord | None
        try:
            persisted = initial
            if persisted is None or persisted.status in _TERMINAL:
                resolved_run = persisted
            else:
                resolved_run = await self._execute_skill(persisted, trace_id=trace_id, trace=trace)
        except SkillRegistryError as error:
            logger.error(
                "qa_run_failed",
                extra={"error_code": "QA_SKILL_INVALID", "error_type": type(error).__name__},
            )
            await trace.record(
                "run_error", error=_safe_error(error), error_code=QAErrorCode.SKILL_INVALID.value
            )
            recovered_run = await self._repository.get_run(run_id)
            if recovered_run is not None and recovered_run.status not in _TERMINAL:
                recovered_run = await self._repository.transition_run(
                    run_id, QAEvent.FAIL, error_code=QAErrorCode.SKILL_INVALID.value
                )
            resolved_run = recovered_run
        except Exception as error:
            logger.error(
                "qa_run_failed",
                extra={"error_code": "QA_RUNTIME_FAILED", "error_type": type(error).__name__},
            )
            await trace.record(
                "run_error", error=_safe_error(error), error_code="QA_RUNTIME_FAILED"
            )
            recovered_run = await self._repository.get_run(run_id)
            if recovered_run is not None and recovered_run.status not in _TERMINAL:
                recovered_run = await self._repository.transition_run(
                    run_id, QAEvent.FAIL, error_code="QA_RUNTIME_FAILED"
                )
            resolved_run = recovered_run
        if resolved_run is None:
            return None
        event_type = {
            QAStatus.COMPLETED: QAEventType.COMPLETED,
            QAStatus.REFUSED: QAEventType.REFUSED,
            QAStatus.CANCELLED: QAEventType.CANCELLED,
            QAStatus.TIMED_OUT: QAEventType.TIMED_OUT,
        }.get(resolved_run.status, QAEventType.FAILED)
        await self._events.append(
            run_id,
            event_type,
            {"status": resolved_run.status.value, "error_code": resolved_run.error_code},
        )
        await trace.record(
            "run_result",
            status=resolved_run.status.value,
            error_code=resolved_run.error_code,
            outcome=resolved_run.result.outcome.value if resolved_run.result is not None else None,
            result=resolved_run.result,
            usage=resolved_run.usage,
        )
        return resolved_run

    async def _execute_skill(
        self, run: QARunRecord, *, trace_id: str, trace: QADebugTrace
    ) -> QARunRecord:
        registry = self._skill_registry or qa_skill_registry()
        pin = registry.pin(run.versions.skill_name, run.versions.skill_version)
        if (
            run.versions.skill_content_sha256 is None
            or pin.content_sha256 != run.versions.skill_content_sha256
        ):
            return await self._repository.transition_run(
                run.run_id, QAEvent.FAIL, error_code=QAErrorCode.SKILL_INVALID.value
            )
        package = registry.validate_pin(pin)
        service = self.build_service(
            self._gateway,
            generation_gateway=TracingModelGateway(
                StructuredFakeGateway(self._gateway), trace, phase="grounded_generation"
            ),
        )
        question = await self._repository.get_message(run.question_message_id)
        if question is None or question.role is not MessageRole.USER:
            return await self._repository.transition_run(
                run.run_id, QAEvent.FAIL, error_code="QA_RUNTIME_FAILED"
            )
        use_generic_knowledge_loop = run.versions.skill_name == "knowledge_agent"
        runtime_gateway: ModelGateway
        state_store = PostgresRuntimeStateStore(self._database)
        if use_generic_knowledge_loop:
            loop_tools = KnowledgeLoopTools(
                qa=service,
                search=DatabaseSearchService(self._database, self._gateway),
                config=KnowledgeLoopToolsConfig(
                    profile=self.profile,
                    versions=run.versions,
                    retrieval_scope=run.retrieval_scope,
                    tool_version="1.1.0",
                ),
                result_reader=self._repository.get_run,
            )
            runtime_gateway = TracingModelGateway(
                StructuredKnowledgeLoopGateway(self._gateway), trace, phase="agent_decision"
            )
            runtime_tool_registry = TracingToolRegistry(loop_tools.tool_registry, trace)
            loop_tools.replace_tool_registry(runtime_tool_registry)
        else:
            qa_adapter = GroundedQASkillAdapter(
                qa=service,
                config=GroundedQASkillConfig(
                    profile=self.profile,
                    versions=run.versions,
                    execute_existing_run=True,
                    skill_name=run.versions.skill_name,
                    output_schema_version=_skill_output_schema(run.versions.skill_name),
                    preview_only_write=(
                        run.versions.skill_name == "create_review_cards"
                        and self._derived_writer is None
                    ),
                    approval_port=self._approval_port,
                    approval_id=self._approval_id,
                    derived_writer=self._derived_writer,
                ),
            )
            runtime_gateway = self._gateway
            runtime_tool_registry = None
            runtime_handlers = qa_adapter.handlers()
        runtime_run = AgentRun(
            context=AgentRunContext(
                run_id=run.run_id,
                space_id=run.space_id,
                skill_name=pin.name,
                skill_version=pin.version,
                skill_content_sha256=pin.content_sha256,
                trace_id=trace_id,
                caller_id=run.caller_id,
                granted_permissions=package.manifest.permissions,
            ),
            budget=package.manifest.budgets,
        )
        runtime_input = {
            "question": question.content,
            "conversation_id": str(run.conversation_id),
        }
        persisted_runtime = await state_store.get_run(run.run_id)
        checkpoint = await state_store.get_latest(run.run_id)
        resumable = (
            persisted_runtime is not None
            and checkpoint is not None
            and persisted_runtime.status
            not in {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.CANCELLED,
                RunStatus.TIMED_OUT,
            }
        )
        result: AgentLoopResult | RuntimeExecutionResult
        if use_generic_knowledge_loop:
            assert runtime_tool_registry is not None
            parent = await PostgresConversationRunRepository(self._database).get_conversation_run(
                run.run_id
            )
            reasoning_profile = (
                parent.reasoning_profile if parent is not None else ReasoningProfile.unresolved()
            )
            loop_executor = AgentLoopExecutor(
                tool_registry=runtime_tool_registry,
                allowed_tools=loop_tools.allowed_tools,
                system_prompt=(package.root / package.manifest.prompts[0]).read_text(
                    encoding="utf-8"
                ),
                model_gateway=runtime_gateway,
                state_store=state_store,
                event_store=self._agent_events,
                reasoning_profile=reasoning_profile,
                finalizer=loop_tools.finalizer(),
                decision_policy=loop_tools.decision_policy,
            )
            if resumable:
                assert persisted_runtime is not None and checkpoint is not None
                result = await loop_executor.resume(
                    persisted_runtime,
                    pin,
                    checkpoint,
                    runtime_input,
                    caller_id=run.caller_id,
                    space_id=run.space_id,
                )
            else:
                result = await loop_executor.execute(
                    runtime_run,
                    pin,
                    runtime_input,
                    goal=question.content,
                )
        else:
            runtime_executor = DeterministicWorkflowExecutor(
                skill_registry=registry,
                model_gateway=runtime_gateway,
                handlers=runtime_handlers,
                tool_registry=runtime_tool_registry,
                state_store=state_store,
            )
            if resumable:
                assert persisted_runtime is not None and checkpoint is not None
                result = await runtime_executor.resume(
                    persisted_runtime,
                    pin,
                    checkpoint,
                    runtime_input,
                    caller_id=run.caller_id,
                    space_id=run.space_id,
                )
            else:
                result = await runtime_executor.execute(runtime_run, pin, runtime_input)
        persisted = await self._repository.get_run(run.run_id)
        if persisted is None:
            raise RuntimeError("Grounded QA run disappeared during Skill execution")
        if result.error is not None and persisted.status not in _TERMINAL:
            error_code = (
                QAErrorCode.SKILL_INVALID.value
                if result.error.code.startswith("SKILL_")
                else "QA_RUNTIME_FAILED"
            )
            return await self._repository.transition_run(
                run.run_id, QAEvent.FAIL, error_code=error_code
            )
        if result.error is not None:
            logger.error(
                "qa_runtime_result_failed",
                extra={
                    "error_code": persisted.error_code or result.error.code,
                    "retryable": result.error.retryable,
                },
            )
            await trace.record(
                "runtime_result",
                status=result.run.status.value,
                error={
                    "error_code": result.error.code,
                    "category": result.error.category.value,
                    "message": result.error.message,
                    "retryable": result.error.retryable,
                },
                qa_error_code=persisted.error_code,
            )
        return persisted


def _profiles() -> tuple[QAPlanningProfileV1, RetrievalProfileV1, QAGenerationProfileV1]:
    identity = settings.active_embedding_identity()
    retrieval = RetrievalProfileV1(embedding_version=identity.version)
    # R4-04: multi-query rewriting is enabled based on the query-expansion pilot
    # (question-only rewrites lift dev recall@10 by ~+2.8pp); when fast_chat is
    # unavailable the QueryPlanner falls back to the original question.
    planning = QAPlanningProfileV1(rewrite_enabled=True, max_subqueries=4)
    generation = QAGenerationProfileV1(
        retrieval_profile_reference=retrieval.profile_version,
        model_identity=(settings.fast_chat_model or "fake-fast-chat-v1"),
        prompt_template_id="grounded-qa-v1-provisional",
    )
    return planning, retrieval, generation


def _skill_output_schema(skill_name: str) -> str:
    schemas = {
        "summarize_document": "summarize-document-skill-output-v1",
        "compare_sources": "compare-sources-skill-output-v1",
        "create_review_cards": "review-cards-skill-output-v1",
        "knowledge_agent": "knowledge-agent-skill-output-v1",
    }
    # Personal Skills (ADR-018) compose the same Grounded QA handlers, so an
    # unlisted Skill name falls back to the generic projected output shape.
    return schemas.get(skill_name, "personal-skill-output-v1")


def _safe_error(error: BaseException) -> dict[str, str]:
    """Keep terminal diagnostics useful without logging request/document bodies."""
    payload = {"error_type": type(error).__name__, "message": str(error)[:2000]}
    code = getattr(error, "code", None)
    if code is not None:
        payload["error_code"] = getattr(code, "value", str(code))
    return payload


__all__ = [
    "GroundedQAExecutor",
    "StructuredAssistantLoopGateway",
    "StructuredFakeGateway",
    "assistant_skill_registry",
    "qa_skill_registry",
    "qa_execution_versions",
]
