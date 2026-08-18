"""Native helper Tools for research and course-project Skills."""

from __future__ import annotations

import hashlib
from typing import Protocol, cast
from uuid import UUID

from agent_runtime import JSONValue, ToolDefinition, ToolExecutionContext, ToolHandler
from domain.agent_runtime import ToolPermission
from domain.retrieval import RetrievalProfileV1, SearchFilters, SearchRequest, SearchResult


class WorkflowSearchPort(Protocol):
    async def search(self, request: SearchRequest, profile: RetrievalProfileV1) -> SearchResult: ...


class WorkflowNativeTools:
    def __init__(self, *, search: WorkflowSearchPort, profile: RetrievalProfileV1) -> None:
        self._search = search
        self._profile = profile

    def handlers(self) -> dict[str, ToolHandler]:
        return {
            "research_discover": self.research_discover,
            "research_prepare": self.research_prepare,
            "project_analyze": self.project_analyze,
            "project_checkpoint": self.project_checkpoint,
        }

    async def research_discover(
        self, arguments: dict[str, JSONValue], context: ToolExecutionContext
    ) -> JSONValue:
        result = await self._search.search(
            SearchRequest(
                query=_text(arguments, "query", 512),
                space_id=context.run.space_id,
                filters=SearchFilters(),
            ),
            self._profile,
        )
        candidates = _versions(result, 8)
        for candidate in candidates:
            if isinstance(candidate, dict):
                candidate["relevance_reason"] = "Matched the current research topic in this Space."
        return cast(
            JSONValue,
            {
                "status": "confirmation_required",
                "candidate_count": len(candidates),
                "candidates": candidates,
                "recommended_next": "ask_user_to_confirm_sources",
            },
        )

    async def research_prepare(
        self, arguments: dict[str, JSONValue], context: ToolExecutionContext
    ) -> JSONValue:
        question = _text(arguments, "question", 2000)
        mode = str(arguments.get("mode", "deep_read"))
        if mode not in {"deep_read", "literature_review"}:
            raise ValueError("RESEARCH_MODE_INVALID")
        selected = arguments.get("selected_version_ids")
        selected_ids: frozenset[UUID] = frozenset()
        if selected is not None:
            if not isinstance(selected, list) or any(
                not isinstance(item, str) for item in selected
            ):
                raise ValueError("RESEARCH_SOURCE_SCOPE_INVALID")
            selected_values = cast(list[str], selected)
            try:
                selected_ids = frozenset(UUID(item) for item in selected_values)
            except ValueError as exc:
                raise ValueError("RESEARCH_SOURCE_SCOPE_INVALID") from exc
        result = await self._search.search(
            SearchRequest(query=question, space_id=context.run.space_id, filters=SearchFilters()),
            self._profile,
        )
        versions = _versions(result, 1 if mode == "deep_read" else 8)
        if selected_ids:
            result = await self._search.search(
                SearchRequest(
                    query=question,
                    space_id=context.run.space_id,
                    filters=SearchFilters(version_ids=selected_ids),
                ),
                self._profile,
            )
            versions = _versions(result, len(selected_ids))
        if len(versions) < (1 if mode == "deep_read" else 2):
            raise ValueError("RESEARCH_SOURCE_SCOPE_INSUFFICIENT")
        return cast(
            JSONValue,
            {
                "status": "scope_prepared",
                "mode": mode,
                "source_count": len(versions),
                "source_versions": versions,
                "scope_locked": True,
                "recommended_next": "knowledge_retrieve",
            },
        )

    async def project_analyze(
        self, arguments: dict[str, JSONValue], context: ToolExecutionContext
    ) -> JSONValue:
        result = await self._search.search(
            SearchRequest(
                query=_text(arguments, "goal", 2000),
                space_id=context.run.space_id,
                filters=SearchFilters(),
            ),
            self._profile,
        )
        versions = _versions(result, 8)
        return cast(
            JSONValue,
            {
                "status": "analysis_ready" if versions else "evidence_required",
                "source_count": len(versions),
                "source_versions": versions,
                "unknown_requirements_preserved": True,
                "recommended_next": "knowledge_retrieve" if versions else "ask_for_requirements",
            },
        )

    async def project_checkpoint(
        self, arguments: dict[str, JSONValue], context: ToolExecutionContext
    ) -> JSONValue:
        stage = _text(arguments, "stage", 64)
        facts = arguments.get("confirmed_facts")
        if (
            not isinstance(facts, list)
            or not facts
            or any(not isinstance(item, str) or not item.strip() for item in facts)
        ):
            raise ValueError("PROJECT_CONFIRMED_FACTS_REQUIRED")
        normalized = [item.strip() for item in facts if isinstance(item, str)]
        checkpoint_key = hashlib.sha256(
            f"{context.run.run_id}:{stage}:{'|'.join(sorted(normalized))}".encode()
        ).hexdigest()[:24]
        return cast(
            JSONValue,
            {
                "status": "checkpoint_recorded",
                "stage": stage,
                "checkpoint_key": checkpoint_key,
                "confirmed_fact_count": len(normalized),
                "confirmed_facts": normalized,
                "idempotent": True,
                "unverified_claims_promoted": 0,
                "recommended_next": "advance_current_stage",
            },
        )


def workflow_tool_definitions() -> tuple[ToolDefinition, ...]:
    output: dict[str, JSONValue] = {"type": "object", "additionalProperties": True}
    source_versions: dict[str, JSONValue] = {
        "type": "array",
        "maxItems": 8,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "label": {"type": "string", "maxLength": 200},
                "source_id": {"type": "string", "format": "uuid"},
                "document_id": {"type": "string", "format": "uuid"},
                "version_id": {"type": "string", "format": "uuid"},
                "relevance_reason": {"type": "string", "maxLength": 240},
            },
        },
    }
    observation: dict[str, JSONValue] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "status": {"type": "string", "maxLength": 64},
            "mode": {"type": "string", "maxLength": 64},
            "stage": {"type": "string", "maxLength": 64},
            "candidate_count": {"type": "integer", "minimum": 0, "maximum": 8},
            "source_count": {"type": "integer", "minimum": 0, "maximum": 8},
            "candidates": source_versions,
            "source_versions": source_versions,
            "scope_locked": {"type": "boolean"},
            "unknown_requirements_preserved": {"type": "boolean"},
            "checkpoint_key": {"type": "string", "maxLength": 64},
            "idempotent": {"type": "boolean"},
            "confirmed_fact_count": {"type": "integer", "minimum": 0, "maximum": 50},
            "confirmed_facts": {
                "type": "array",
                "maxItems": 50,
                "items": {"type": "string", "maxLength": 1000},
            },
            "unverified_claims_promoted": {"type": "integer", "minimum": 0},
            "recommended_next": {"type": "string", "maxLength": 160},
        },
    }
    read = frozenset({ToolPermission.READ_KNOWLEDGE})
    return (
        ToolDefinition(
            name="research_discover",
            version="2.0.0",
            description="Find current-Space paper candidates for user confirmation.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["query"],
                "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 512}},
            },
            output_schema=output,
            permissions=read,
            handler_name="research_discover",
            model_visible=True,
            model_observation_schema=observation,
        ),
        ToolDefinition(
            name="research_prepare",
            version="2.0.0",
            description="Lock one paper or two to eight papers for this research Run.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["question", "mode"],
                "properties": {
                    "question": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "mode": {"enum": ["deep_read", "literature_review"]},
                    "selected_version_ids": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 8,
                        "uniqueItems": True,
                        "items": {"type": "string", "format": "uuid"},
                    },
                },
            },
            output_schema=output,
            permissions=read,
            handler_name="research_prepare",
            model_visible=True,
            model_observation_schema=observation,
        ),
        ToolDefinition(
            name="project_analyze",
            version="2.0.0",
            description="Inspect project requirements without inventing missing rubric items.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["goal"],
                "properties": {"goal": {"type": "string", "minLength": 1, "maxLength": 2000}},
            },
            output_schema=output,
            permissions=read,
            handler_name="project_analyze",
            model_visible=True,
            model_observation_schema=observation,
        ),
        ToolDefinition(
            name="project_checkpoint",
            version="2.0.0",
            description="Record user-confirmed project facts in persisted Tool history.",
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["stage", "confirmed_facts"],
                "properties": {
                    "stage": {"type": "string", "minLength": 1, "maxLength": 64},
                    "confirmed_facts": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 50,
                        "items": {"type": "string", "minLength": 1, "maxLength": 1000},
                    },
                },
            },
            output_schema=output,
            permissions=read,
            handler_name="project_checkpoint",
            model_visible=True,
            model_observation_schema=observation,
        ),
    )


def _text(arguments: dict[str, JSONValue], key: str, maximum: int) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{key.upper()}_INVALID")
    return value.strip()


def _versions(result: SearchResult, limit: int) -> list[JSONValue]:
    unique: dict[tuple[str, str, str], dict[str, JSONValue]] = {}
    for hit in result.hits:
        if hit.context_only:
            continue
        key = (str(hit.source_id), str(hit.document_id), str(hit.version_id))
        unique.setdefault(
            key,
            {
                "label": hit.source_key[:200],
                "source_id": key[0],
                "document_id": key[1],
                "version_id": key[2],
            },
        )
        if len(unique) >= limit:
            break
    return list(unique.values())


__all__ = ["WorkflowNativeTools", "workflow_tool_definitions"]
