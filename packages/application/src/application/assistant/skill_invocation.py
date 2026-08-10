"""Server-authoritative Skill selection and projection contracts for Assistant routing."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Protocol

from agent_runtime import FileSystemSkillRegistry
from domain.conversation_run import (
    Clarification,
    ClarificationContinuation,
    ClarificationKind,
    ConversationRun,
    ConversationRunKind,
    ConversationRunRepository,
    ConversationRunSelectionSource,
    ConversationRunStatus,
    ConversationRunUsage,
    FixedSkillIdentity,
)

from application.skills import SkillCatalogPort, SkillInvocationView

from .context import ConversationContextService, ConversationContextSnapshot
from .resources import ResourceResolutionError, ResourceResolutionErrorCode, ResourceResolutionPort


class SkillProjectionPort(Protocol):
    async def create(
        self,
        run: ConversationRun,
        *,
        skill: FixedSkillIdentity,
        arguments: Mapping[str, object],
        resource_scope: object | None,
    ) -> ConversationRun: ...


_FORBIDDEN_ARGUMENT_KEY = re.compile(
    r"(?:^|_)(?:id|ids|space|space_id|version|version_id|document_id|source_id|chunk_id|run_id)$",
    re.IGNORECASE,
)


class AssistantSkillInvocationService:
    """Validate router intent, pin active identity, resolve resources, and delegate execution."""

    def __init__(
        self,
        *,
        runs: ConversationRunRepository,
        catalog: SkillCatalogPort,
        registry: FileSystemSkillRegistry,
        projection: SkillProjectionPort,
        resources: ResourceResolutionPort | None = None,
    ) -> None:
        self._runs = runs
        self._catalog = catalog
        self._registry = registry
        self._projection = projection
        self._resources = resources

    @property
    def catalog(self) -> SkillCatalogPort:
        return self._catalog

    async def invoke(
        self,
        run: ConversationRun,
        *,
        skill: SkillInvocationView,
        arguments: Mapping[str, object],
        selection_source: ConversationRunSelectionSource = ConversationRunSelectionSource.AUTO,
        context: ConversationContextSnapshot | None = None,
    ) -> ConversationRun:
        active_skill = next(
            (
                entry
                for entry in self._catalog.list_active_invocations()
                if entry.name == skill.name
            ),
            None,
        )
        if active_skill is None or active_skill != skill:
            raise ValueError("SKILL_NOT_ACTIVE")
        if not _safe_arguments(arguments):
            raise ValueError("RUN_AGENT_DECISION_INVALID")
        try:
            pin = self._registry.pin(skill.name, skill.version)
        except Exception as exc:
            raise ValueError("SKILL_NOT_ACTIVE") from exc
        if pin.content_sha256 != skill.content_sha256:
            raise ValueError("SKILL_NOT_ACTIVE")
        if skill.input_mode in {"question", "document", "sources"} and (
            not isinstance(arguments.get("question"), str) or not str(arguments["question"]).strip()
        ):
            return await self._runs.publish_clarification(
                run_id=run.run_id,
                clarification=Clarification(
                    clarification_id=f"clarify:{run.run_id.hex}:question",
                    kind=ClarificationKind.INPUT_REQUIRED,
                    message="Please provide the question or task for this Skill.",
                ),
                usage=ConversationRunUsage(),
                model_identity="router",
            )
        resource_scope = None
        resource_type = arguments.get("resource_type")
        reference = arguments.get("resource_reference")
        if skill.input_mode in {"document", "sources"} and (
            not isinstance(resource_type, str) or not isinstance(reference, str)
        ):
            return await self._runs.publish_clarification(
                run_id=run.run_id,
                clarification=Clarification(
                    clarification_id=f"clarify:{run.run_id.hex}:resource",
                    kind=ClarificationKind.INPUT_REQUIRED,
                    message="Please name the document or sources to use.",
                ),
                usage=ConversationRunUsage(),
                model_identity="router",
            )
        if resource_type is not None or reference is not None:
            if (
                self._resources is None
                or not isinstance(resource_type, str)
                or not isinstance(reference, str)
            ):
                raise ValueError(ResourceResolutionErrorCode.NOT_FOUND.value)
            try:
                if context is None:
                    resolved = await self._resources.resolve(
                        space_id=run.space_id,
                        resource_type=resource_type,
                        reference=reference,
                    )
                else:
                    resolved = await self._resources.resolve(
                        space_id=run.space_id,
                        resource_type=resource_type,
                        reference=reference,
                        context=context,
                    )
                resource_scope = resolved.scope
            except ResourceResolutionError as exc:
                kind = (
                    ClarificationKind.RESOURCE_AMBIGUOUS
                    if exc.code is ResourceResolutionErrorCode.CONFLICT
                    else ClarificationKind.RESOURCE_MISSING
                )
                return await self._runs.publish_clarification(
                    run_id=run.run_id,
                    clarification=Clarification(
                        clarification_id=f"clarify:{run.run_id.hex}:resource",
                        kind=kind,
                        message=(
                            "Choose one matching resource to continue."
                            if exc.code is ResourceResolutionErrorCode.CONFLICT
                            else "No matching resource is available in this Space."
                        ),
                        resource_candidates=exc.candidates,
                        continuation=(
                            ClarificationContinuation(
                                skill=FixedSkillIdentity(
                                    skill.name, skill.version, skill.content_sha256
                                ),
                                selection_source=selection_source,
                                question=str(arguments["question"]).strip(),
                                resource_type=resource_type,
                            )
                            if exc.candidates
                            else None
                        ),
                    ),
                    usage=ConversationRunUsage(),
                    model_identity="router",
                )
        return await self._promote_and_project(
            run,
            skill=FixedSkillIdentity(skill.name, skill.version, skill.content_sha256),
            arguments=arguments,
            selection_source=selection_source,
            resource_scope=resource_scope,
            context=context,
        )

    async def resume_resource_clarification(
        self,
        run: ConversationRun,
        *,
        clarification_id: str,
        candidate_id: str,
        context_service: ConversationContextService,
    ) -> ConversationRun:
        clarification = run.result.clarification if run.result is not None else None
        if (
            run.status is not ConversationRunStatus.WAITING_CLARIFICATION
            or clarification is None
            or clarification.clarification_id != clarification_id
            or clarification.continuation is None
            or candidate_id not in {item.candidate_id for item in clarification.resource_candidates}
            or self._resources is None
        ):
            raise ValueError("RUN_CLARIFICATION_INVALID")
        continuation = clarification.continuation
        try:
            pin = self._registry.pin(continuation.skill.name, continuation.skill.version)
        except Exception as exc:
            raise ValueError("SKILL_NOT_ACTIVE") from exc
        if pin.content_sha256 != continuation.skill.content_sha256:
            raise ValueError("SKILL_NOT_ACTIVE")
        try:
            selected = await self._resources.select_candidate(
                space_id=run.space_id,
                resource_type=continuation.resource_type,
                candidate_id=candidate_id,
            )
        except ResourceResolutionError as exc:
            raise ValueError(exc.code.value) from exc
        reopened = await self._runs.reopen_clarification(
            run.run_id, clarification_id=clarification_id
        )
        context = await context_service.snapshot(reopened)
        return await self._promote_and_project(
            reopened,
            skill=continuation.skill,
            arguments={
                "question": continuation.question,
                "resource_type": continuation.resource_type,
                "resource_reference": selected.candidate.label,
            },
            selection_source=continuation.selection_source,
            resource_scope=selected.scope,
            context=context,
        )

    async def _promote_and_project(
        self,
        run: ConversationRun,
        *,
        skill: FixedSkillIdentity,
        arguments: Mapping[str, object],
        selection_source: ConversationRunSelectionSource,
        resource_scope: object | None,
        context: ConversationContextSnapshot | None,
    ) -> ConversationRun:
        run_kind = (
            ConversationRunKind.GROUNDED_QA
            if skill.name == "knowledge_qa"
            else ConversationRunKind.SKILL
        )
        promoted = await self._runs.promote_to_skill(
            run.run_id,
            run_kind=run_kind,
            selection_source=selection_source,
            skill=skill,
            core_prompt_version="assistant-base-prompt-v4",
        )
        assert promoted.skill is not None
        projection_arguments = dict(arguments)
        question = projection_arguments.get("question")
        if isinstance(question, str) and question.strip():
            projection_arguments["standalone_request"] = question.strip()
        if context is not None:
            # The router may inspect bounded conversation context, but the QA retrieval
            # query must remain the Skill's current question. Persisting router_input()
            # here leaked prior assistant answers into later searches.
            projection_arguments["context_sensitivity"] = context.sensitivity.value
        return await self._projection.create(
            promoted,
            skill=promoted.skill,
            arguments=projection_arguments,
            resource_scope=resource_scope,
        )


def _safe_arguments(arguments: Mapping[str, object]) -> bool:
    if len(arguments) > 16:
        return False
    for key, value in arguments.items():
        if not isinstance(key, str) or _FORBIDDEN_ARGUMENT_KEY.search(key):
            return False
        if isinstance(value, Mapping) and not _safe_arguments(value):
            return False
        if isinstance(value, (list, tuple)) and any(
            isinstance(item, Mapping) and not _safe_arguments(item) for item in value
        ):
            return False
    return True


__all__ = ["AssistantSkillInvocationService", "SkillProjectionPort"]
