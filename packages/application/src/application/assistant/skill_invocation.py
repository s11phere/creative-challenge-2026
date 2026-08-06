"""Server-authoritative Skill selection and projection contracts for Assistant routing."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Protocol

from agent_runtime import FileSystemSkillRegistry
from domain.conversation_run import (
    Clarification,
    ClarificationKind,
    ConversationRun,
    ConversationRunKind,
    ConversationRunRepository,
    ConversationRunSelectionSource,
    ConversationRunUsage,
    FixedSkillIdentity,
)

from application.skills import SkillCatalogPort, SkillInvocationView

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

    async def invoke(
        self,
        run: ConversationRun,
        *,
        skill: SkillInvocationView,
        arguments: Mapping[str, object],
    ) -> ConversationRun:
        if skill.name not in {entry.name for entry in self._catalog.list_active_invocations()}:
            raise ValueError("SKILL_NOT_ACTIVE")
        if not _safe_arguments(arguments):
            raise ValueError("RUN_AGENT_DECISION_INVALID")
        try:
            pin = self._registry.pin(skill.name, skill.version)
        except Exception as exc:
            raise ValueError("SKILL_NOT_ACTIVE") from exc
        if pin.content_sha256 != skill.content_sha256:
            raise ValueError("SKILL_NOT_ACTIVE")
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
                resolved = await self._resources.resolve(
                    space_id=run.space_id,
                    resource_type=resource_type,
                    reference=reference,
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
                    ),
                    usage=ConversationRunUsage(),
                    model_identity="router",
                )
        run_kind = (
            ConversationRunKind.GROUNDED_QA
            if skill.name == "knowledge_qa"
            else ConversationRunKind.SKILL
        )
        promoted = await self._runs.promote_to_skill(
            run.run_id,
            run_kind=run_kind,
            selection_source=ConversationRunSelectionSource.AUTO,
            skill=FixedSkillIdentity(skill.name, skill.version, skill.content_sha256),
            core_prompt_version="assistant-base-prompt-v2",
        )
        assert promoted.skill is not None
        return await self._projection.create(
            promoted,
            skill=promoted.skill,
            arguments=arguments,
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
