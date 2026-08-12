"""Trusted, immutable Skill package loading and version registration."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any, cast
from urllib.parse import urlsplit

import yaml
from domain.agent_runtime import RunBudget, RunCheckpoint, ToolPermission, ToolRegistry
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from yaml.events import AliasEvent

from .tools import JSONValue, ToolRef

_TRANSIENT_NAMES = frozenset({".DS_Store", "Thumbs.db"})
_TRANSIENT_SUFFIXES = frozenset({".pyc", ".pyo"})
_TRANSIENT_DIRS = frozenset({".git", "__pycache__"})
_MAX_FILE_BYTES = 2 * 1024 * 1024
_MAX_PACKAGE_BYTES = 16 * 1024 * 1024

SKILL_MANIFEST_SCHEMA: dict[str, JSONValue] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "manifest_version",
        "name",
        "version",
        "description",
        "input_schema",
        "output_schema",
        "required_tools",
        "required_capabilities",
        "permissions",
        "budgets",
        "entrypoint",
        "compatibility",
        "prompts",
        "evals",
    ],
    "properties": {
        "manifest_version": {"enum": ["1", "2"]},
        "name": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
        "version": {
            "type": "string",
            "pattern": "^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$",
        },
        "description": {"type": "string", "minLength": 1},
        "input_schema": {"type": "string", "minLength": 1},
        "output_schema": {"type": "string", "minLength": 1},
        "required_tools": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "version"],
                "properties": {
                    "name": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"},
                    "version": {
                        "type": "string",
                        "pattern": "^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)$",
                    },
                },
            },
            "uniqueItems": True,
        },
        "required_capabilities": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "permissions": {
            "type": "array",
            "items": {"enum": [permission.value for permission in ToolPermission]},
            "uniqueItems": True,
        },
        "budgets": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "max_steps",
                "max_tool_calls",
                "max_input_tokens",
                "max_output_tokens",
                "timeout_seconds",
            ],
            "properties": {
                "max_steps": {"type": "integer", "minimum": 1},
                "max_tool_calls": {"type": "integer", "minimum": 1},
                "max_input_tokens": {"type": "integer", "minimum": 1},
                "max_output_tokens": {"type": "integer", "minimum": 1},
                "timeout_seconds": {"type": "integer", "minimum": 1},
            },
        },
        "entrypoint": {"type": "string", "minLength": 1},
        "compatibility": {
            "type": "object",
            "additionalProperties": False,
            "required": ["runtime", "checkpoint_schema_versions"],
            "properties": {
                "runtime": {"type": "string", "minLength": 1},
                "checkpoint_schema_versions": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "integer", "minimum": 1},
                    "uniqueItems": True,
                },
            },
        },
        "prompts": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "evals": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "invocation": {
            "type": "object",
            "additionalProperties": False,
            "required": ["command", "aliases", "argument_hint", "trigger", "input_mode"],
            "properties": {
                "command": {"type": "string", "pattern": "^[a-z][a-z0-9-]*$", "maxLength": 32},
                "aliases": {
                    "type": "array",
                    "maxItems": 8,
                    "uniqueItems": True,
                    "items": {"type": "string", "pattern": "^[a-z][a-z0-9-]*$", "maxLength": 32},
                },
                "argument_hint": {"type": "string", "maxLength": 160},
                "trigger": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["summary", "when", "avoid_when", "examples"],
                    "properties": {
                        "summary": {"type": "string", "minLength": 1, "maxLength": 280},
                        "when": {
                            "type": "array",
                            "maxItems": 4,
                            "items": {"type": "string", "minLength": 1, "maxLength": 200},
                        },
                        "avoid_when": {
                            "type": "array",
                            "maxItems": 4,
                            "items": {"type": "string", "minLength": 1, "maxLength": 200},
                        },
                        "examples": {
                            "type": "array",
                            "maxItems": 4,
                            "items": {"type": "string", "minLength": 1, "maxLength": 240},
                        },
                    },
                },
                "input_mode": {"enum": ["none", "question", "document", "sources"]},
                "execution_mode": {"enum": ["projected", "agent_loop"]},
            },
        },
    },
}


EVAL_CHECK_TYPES: tuple[str, ...] = (
    "output_matches_schema",
    "output_has_key",
    "cites_sources",
    "trace_tool_called",
    "finalized",
)

EVAL_CASE_SCHEMA: dict[str, JSONValue] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    # Existing eval cases carry survey-only labels (scenario/mode/expected_question_count),
    # so only case_id is required and additional properties stay allowed.
    "required": ["case_id"],
    "properties": {
        "case_id": {"type": "string", "minLength": 1},
        "input": {"type": ["object", "null"]},
        "expected": {"type": ["string", "object"]},
        "fixture": {"type": "string", "minLength": 1},
        "checks": {
            "type": "array",
            "items": {"$ref": "#/$defs/check"},
        },
    },
    "$defs": {
        "check": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type"],
            "properties": {
                "type": {"enum": list(EVAL_CHECK_TYPES)},
                "key": {"type": "string", "minLength": 1},
                "tool": {"type": "string", "minLength": 1},
                "min": {"type": "integer", "minimum": 1},
            },
            "allOf": [
                {
                    "if": {"properties": {"type": {"const": "output_has_key"}}},
                    "then": {"required": ["key"]},
                },
                {
                    "if": {"properties": {"type": {"const": "trace_tool_called"}}},
                    "then": {"required": ["tool"]},
                },
            ],
        }
    },
}


class SkillRegistryErrorCode(StrEnum):
    INVALID_MANIFEST = "SKILL_INVALID_MANIFEST"
    INVALID_PACKAGE = "SKILL_INVALID_PACKAGE"
    INVALID_SCHEMA = "SKILL_INVALID_SCHEMA"
    PATH_OUTSIDE_TRUSTED_ROOT = "SKILL_PATH_OUTSIDE_TRUSTED_ROOT"
    LINK_NOT_ALLOWED = "SKILL_LINK_NOT_ALLOWED"
    NAME_CONFLICT = "SKILL_NAME_CONFLICT"
    VERSION_CONFLICT = "SKILL_VERSION_CONFLICT"
    NOT_FOUND = "SKILL_NOT_FOUND"
    ACTIVE_VERSION_MISSING = "SKILL_ACTIVE_VERSION_MISSING"
    INCOMPATIBLE = "SKILL_INCOMPATIBLE"
    DIGEST_MISMATCH = "SKILL_DIGEST_MISMATCH"
    CHECKPOINT_INCOMPATIBLE = "SKILL_CHECKPOINT_INCOMPATIBLE"
    TOOL_INCOMPATIBLE = "SKILL_TOOL_INCOMPATIBLE"
    CLEANUP_BLOCKED = "SKILL_CLEANUP_BLOCKED"


class SkillRegistryError(Exception):
    def __init__(self, code: SkillRegistryErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class SkillRegistryEventType(StrEnum):
    INSTALLED = "installed"
    ACTIVATED = "activated"
    ROLLED_BACK = "rolled_back"


@dataclass(frozen=True)
class SkillRegistryEvent:
    event_version: int
    event_type: SkillRegistryEventType
    name: str
    version: str
    content_sha256: str
    previous_version: str | None = None


@dataclass(frozen=True)
class SkillCompatibility:
    runtime: str
    checkpoint_schema_versions: tuple[int, ...]


@dataclass(frozen=True)
class SkillInvocation:
    """Safe, bounded metadata used by the product-level Assistant router."""

    command: str
    aliases: tuple[str, ...]
    argument_hint: str
    trigger_summary: str
    trigger_when: tuple[str, ...]
    trigger_avoid_when: tuple[str, ...]
    trigger_examples: tuple[str, ...]
    input_mode: str
    execution_mode: str = "projected"

    @property
    def commands(self) -> tuple[str, ...]:
        return (self.command, *self.aliases)


@dataclass(frozen=True)
class SkillManifest:
    manifest_version: str
    name: str
    version: str
    description: str
    input_schema: str
    output_schema: str
    required_tools: tuple[ToolRef, ...]
    required_capabilities: frozenset[str]
    permissions: frozenset[ToolPermission]
    budgets: RunBudget
    entrypoint: str
    compatibility: SkillCompatibility
    prompts: tuple[str, ...]
    evals: tuple[str, ...]
    invocation: SkillInvocation | None = None


_SENSITIVE_INVOCATION_TEXT = re.compile(
    (
        r"(?:[0-9a-f]{32,64}|[0-9a-f]{8}-[0-9a-f-]{27,}|"
        r"(?:^|[ _-])(?:space_id|version_id|document_id|source_id|chunk_id|run_id|message_id|uuid)"
        r"(?:$|[ _-]))"
    ),
    re.IGNORECASE,
)


def _parse_invocation(value: dict[str, Any]) -> SkillInvocation:
    trigger = cast(dict[str, Any], value["trigger"])
    command = cast(str, value["command"])
    aliases = tuple(cast(list[str], value["aliases"]))
    commands = (command, *aliases)
    if len(commands) != len(set(commands)):
        raise SkillRegistryError(
            SkillRegistryErrorCode.INVALID_MANIFEST,
            "Skill invocation command and aliases must be unique.",
        )
    examples = tuple(cast(list[str], trigger["examples"]))
    if any(_SENSITIVE_INVOCATION_TEXT.search(text) for text in examples):
        raise SkillRegistryError(
            SkillRegistryErrorCode.INVALID_MANIFEST,
            "Skill invocation examples contain sensitive resource metadata.",
        )
    return SkillInvocation(
        command=command,
        aliases=aliases,
        argument_hint=cast(str, value["argument_hint"]),
        trigger_summary=cast(str, trigger["summary"]),
        trigger_when=tuple(cast(list[str], trigger["when"])),
        trigger_avoid_when=tuple(cast(list[str], trigger["avoid_when"])),
        trigger_examples=examples,
        input_mode=cast(str, value["input_mode"]),
        execution_mode=cast(str, value.get("execution_mode", "projected")),
    )


def _validate_invocation_schema(
    manifest: SkillManifest, input_schema: Mapping[str, JSONValue]
) -> None:
    invocation = manifest.invocation
    if invocation is None:
        return
    properties = input_schema.get("properties")
    property_names = set(properties) if isinstance(properties, dict) else set()
    if invocation.input_mode == "question" and "question" not in property_names:
        raise SkillRegistryError(
            SkillRegistryErrorCode.INVALID_MANIFEST,
            "Question invocation mode requires a question input.",
        )
    if invocation.input_mode in {"document", "sources"} and "question" not in property_names:
        raise SkillRegistryError(
            SkillRegistryErrorCode.INVALID_MANIFEST,
            "Resource invocation modes require a question input.",
        )


@dataclass(frozen=True)
class SkillPackage:
    manifest: SkillManifest
    root: Path
    content_sha256: str
    file_digests: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class PinnedSkill:
    name: str
    version: str
    content_sha256: str
    manifest_version: str
    entrypoint_sha256: str
    input_schema_sha256: str
    output_schema_sha256: str
    prompt_digests: tuple[tuple[str, str], ...]
    compatibility: SkillCompatibility


class FileSystemSkillRegistry:
    """Registry that loads declaration-only Skill packages from trusted roots.

    The primary ``trusted_root`` is the immutable, deployment-owned set. An
    optional ``personal_root`` is a writable, user-owned set: it is loaded at
    runtime but a personal Skill can never shadow a built-in name (ADR-018).
    """

    def __init__(
        self,
        trusted_root: Path,
        *,
        personal_root: Path | None = None,
        runtime_version: str = "0.1.0",
        checkpoint_schema_version: int = 1,
    ) -> None:
        try:
            self._runtime_version = Version(runtime_version)
        except InvalidVersion as exc:
            raise ValueError("runtime version is invalid") from exc
        if checkpoint_schema_version < 1:
            raise ValueError("checkpoint schema version must be positive")
        if not trusted_root.exists() or not trusted_root.is_dir():
            raise SkillRegistryError(
                SkillRegistryErrorCode.INVALID_PACKAGE,
                "Trusted Skill root must be an existing directory.",
            )
        self._reject_link(trusted_root)
        self._trusted_root = trusted_root.resolve(strict=True)
        self._personal_root: Path | None = None
        if personal_root is not None:
            self._reject_link(personal_root)
            self._personal_root = personal_root.resolve()
        self._checkpoint_schema_version = checkpoint_schema_version
        self._packages: dict[tuple[str, str], SkillPackage] = {}
        self._active_versions: dict[str, str] = {}
        self._builtin_names: set[str] = set()
        self._origins: dict[tuple[str, str], Path] = {}
        self._events: list[SkillRegistryEvent] = []
        self._lock = RLock()

    def _roots(self) -> tuple[Path, ...]:
        roots = [self._trusted_root]
        if self._personal_root is not None and self._personal_root.is_dir():
            roots.append(self._personal_root)
        return tuple(roots)

    def _within_any_root(self, path: Path) -> bool:
        if path.is_relative_to(self._trusted_root):
            return True
        return self._personal_root is not None and path.is_relative_to(self._personal_root)

    def _root_for(self, path: Path) -> Path | None:
        for root in self._roots():
            if path.is_relative_to(root):
                return root
        return None

    def load(self, relative_path: str | Path) -> SkillPackage:
        package_root = self._resolve_package_root(relative_path)
        files = self._collect_files(package_root)
        manifest_file = package_root / "skill.yaml"
        if manifest_file not in files:
            raise SkillRegistryError(
                SkillRegistryErrorCode.INVALID_PACKAGE,
                "Skill package must contain skill.yaml.",
            )
        manifest = self._load_manifest(manifest_file)
        referenced_files = {
            manifest.input_schema,
            manifest.output_schema,
            manifest.entrypoint,
            *manifest.prompts,
            *manifest.evals,
        }
        for reference in referenced_files:
            self._resolve_package_file(package_root, reference)
        for eval_reference in manifest.evals:
            self._validate_eval_cases(self._resolve_package_file(package_root, eval_reference))
        if PurePosixPath(manifest.entrypoint).suffix not in {".yaml", ".yml", ".json"}:
            raise SkillRegistryError(
                SkillRegistryErrorCode.INVALID_MANIFEST,
                "Skill entrypoint must be a declarative YAML or JSON workflow.",
            )
        input_schema = self._load_json_schema(package_root, manifest.input_schema)
        self._load_json_schema(package_root, manifest.output_schema)
        _validate_invocation_schema(manifest, input_schema)
        self._validate_compatibility(manifest.compatibility)
        file_digests, content_sha256 = self._digest_files(package_root, files)
        return SkillPackage(
            manifest=manifest,
            root=package_root,
            content_sha256=content_sha256,
            file_digests=file_digests,
        )

    def register(self, package: SkillPackage) -> SkillPackage:
        with self._lock:
            package = self._revalidate_package(package)
            key = (package.manifest.name, package.manifest.version)
            existing = self._packages.get(key)
            if existing is not None and existing.content_sha256 != package.content_sha256:
                raise SkillRegistryError(
                    SkillRegistryErrorCode.VERSION_CONFLICT,
                    "Skill name and version already exist with another content digest.",
                )
            if existing is None:
                self._packages[key] = package
                self._events.append(self._event(SkillRegistryEventType.INSTALLED, package))
            return self._packages[key]

    def load_all(self) -> tuple[SkillPackage, ...]:
        return self.reload()

    def reload(self) -> tuple[SkillPackage, ...]:
        """Validate a complete scan before atomically publishing any new versions."""
        with self._lock:
            candidates: list[SkillPackage] = []
            self._builtin_names = set()
            self._origins = {}
            for root in self._roots():
                for child in sorted(root.iterdir(), key=lambda path: path.name):
                    if child.name.startswith("_"):
                        continue
                    self._reject_link(child)
                    if child.is_dir():
                        package = self._revalidate_package(self.load(child.name))
                        candidates.append(package)
                        self._origins[(package.manifest.name, package.manifest.version)] = root
                        if root is self._trusted_root:
                            self._builtin_names.add(package.manifest.name)

            if self._personal_root is not None:
                for package in candidates:
                    if (
                        self._origins.get((package.manifest.name, package.manifest.version))
                        == self._personal_root
                        and package.manifest.name in self._builtin_names
                    ):
                        raise SkillRegistryError(
                            SkillRegistryErrorCode.NAME_CONFLICT,
                            "Personal Skill cannot shadow a built-in Skill name.",
                        )

            staged = dict(self._packages)
            newly_installed: list[SkillPackage] = []
            for package in candidates:
                key = (package.manifest.name, package.manifest.version)
                existing = staged.get(key)
                if existing is not None and existing.content_sha256 != package.content_sha256:
                    raise SkillRegistryError(
                        SkillRegistryErrorCode.VERSION_CONFLICT,
                        "Skill reload found a conflicting immutable version.",
                    )
                if existing is None:
                    staged[key] = package
                    newly_installed.append(package)

            self._packages = staged
            self._events.extend(
                self._event(SkillRegistryEventType.INSTALLED, package)
                for package in newly_installed
            )
            return tuple(candidates)

    def activate(self, name: str, version: str) -> SkillPackage:
        return self._set_active(name, version, SkillRegistryEventType.ACTIVATED)

    def rollback(self, name: str, version: str) -> SkillPackage:
        return self._set_active(name, version, SkillRegistryEventType.ROLLED_BACK)

    def get(self, name: str, version: str | None = None) -> SkillPackage:
        with self._lock:
            selected_version = version
            if selected_version is None:
                try:
                    selected_version = self._active_versions[name]
                except KeyError as exc:
                    raise SkillRegistryError(
                        SkillRegistryErrorCode.ACTIVE_VERSION_MISSING,
                        "Skill has no active version.",
                    ) from exc
            try:
                return self._packages[(name, selected_version)]
            except KeyError as exc:
                raise SkillRegistryError(
                    SkillRegistryErrorCode.NOT_FOUND,
                    "Requested Skill version is not installed.",
                ) from exc

    def is_available(self, name: str, version: str, content_sha256: str) -> bool:
        with self._lock:
            package = self._packages.get((name, version))
            return package is not None and package.content_sha256 == content_sha256

    def pin(self, name: str, version: str | None = None) -> PinnedSkill:
        with self._lock:
            package = self._revalidate_package(self.get(name, version))
            return self._pin_package(package)

    def _pin_package(self, package: SkillPackage) -> PinnedSkill:
        digests = dict(package.file_digests)
        manifest = package.manifest
        return PinnedSkill(
            name=manifest.name,
            version=manifest.version,
            content_sha256=package.content_sha256,
            manifest_version=manifest.manifest_version,
            entrypoint_sha256=digests[manifest.entrypoint],
            input_schema_sha256=digests[manifest.input_schema],
            output_schema_sha256=digests[manifest.output_schema],
            prompt_digests=tuple((path, digests[path]) for path in manifest.prompts),
            compatibility=manifest.compatibility,
        )

    def validate_pin(self, pin: PinnedSkill) -> SkillPackage:
        with self._lock:
            package = self.get(pin.name, pin.version)
            fresh_package = self._revalidate_package(package)
            if self._pin_package(fresh_package) != pin:
                raise SkillRegistryError(
                    SkillRegistryErrorCode.DIGEST_MISMATCH,
                    "Installed Skill does not match the fixed run version.",
                )
            return package

    def prompt_instructions(self, pin: PinnedSkill) -> str:
        """Read only hash-pinned prompt files from one trusted Skill package."""
        with self._lock:
            package = self.validate_pin(pin)
            instructions = "\n\n".join(
                self._normalized_content(self._resolve_package_file(package.root, path))
                .decode("utf-8")
                .strip()
                for path in package.manifest.prompts
            ).strip()
            if not instructions:
                raise SkillRegistryError(
                    SkillRegistryErrorCode.INVALID_PACKAGE,
                    "Skill prompt instructions cannot be blank.",
                )
            return instructions

    def validate_checkpoint_compatibility(
        self,
        pin: PinnedSkill,
        checkpoint: RunCheckpoint,
        *,
        tool_registry: ToolRegistry | None = None,
    ) -> SkillPackage:
        with self._lock:
            package = self.validate_pin(pin)
            if not checkpoint.verified or (
                checkpoint.skill_name,
                checkpoint.skill_version,
                checkpoint.skill_content_sha256,
            ) != (pin.name, pin.version, pin.content_sha256):
                raise SkillRegistryError(
                    SkillRegistryErrorCode.CHECKPOINT_INCOMPATIBLE,
                    "Checkpoint does not match the fixed Skill version.",
                )
            if checkpoint.schema_version not in pin.compatibility.checkpoint_schema_versions:
                raise SkillRegistryError(
                    SkillRegistryErrorCode.CHECKPOINT_INCOMPATIBLE,
                    "Checkpoint schema is outside the Skill compatibility range.",
                )
            if package.manifest.required_tools and tool_registry is None:
                raise SkillRegistryError(
                    SkillRegistryErrorCode.TOOL_INCOMPATIBLE,
                    "Recovery requires a Tool Registry.",
                )
            if tool_registry is not None and any(
                not tool_registry.is_available(tool.name, tool.version)
                for tool in package.manifest.required_tools
            ):
                raise SkillRegistryError(
                    SkillRegistryErrorCode.TOOL_INCOMPATIBLE,
                    "A Tool version required for recovery is unavailable.",
                )
            return package

    def versions(self, name: str) -> tuple[str, ...]:
        with self._lock:
            versions = [version for package_name, version in self._packages if package_name == name]
            return tuple(sorted(versions, key=Version))

    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted({name for name, _version in self._packages}))

    def builtin_names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._builtin_names))

    def personal_names(self) -> tuple[str, ...]:
        with self._lock:
            if self._personal_root is None:
                return ()
            return tuple(
                sorted(
                    {
                        name
                        for (name, _version), root in self._origins.items()
                        if root == self._personal_root
                    }
                )
            )

    def is_personal(self, name: str) -> bool:
        with self._lock:
            return any(
                root == self._personal_root
                for (candidate, _version), root in self._origins.items()
                if candidate == name
            )

    def is_builtin(self, name: str) -> bool:
        with self._lock:
            return name in self._builtin_names

    def remove(self, name: str, version: str, *, content_sha256: str) -> SkillPackage:
        """Remove an unreferenced non-active package from the live registry."""
        with self._lock:
            if self._active_versions.get(name) == version:
                raise SkillRegistryError(
                    SkillRegistryErrorCode.CLEANUP_BLOCKED,
                    "The active Skill version cannot be removed.",
                )
            package = self._packages.get((name, version))
            if package is None:
                raise SkillRegistryError(
                    SkillRegistryErrorCode.NOT_FOUND,
                    "Requested Skill version is not installed.",
                )
            if package.content_sha256 != content_sha256:
                raise SkillRegistryError(
                    SkillRegistryErrorCode.DIGEST_MISMATCH,
                    "Skill digest does not match the requested cleanup identity.",
                )
            return self._packages.pop((name, version))

    def active_version(self, name: str) -> str:
        with self._lock:
            try:
                return self._active_versions[name]
            except KeyError as exc:
                raise SkillRegistryError(
                    SkillRegistryErrorCode.ACTIVE_VERSION_MISSING,
                    "Skill has no active version.",
                ) from exc

    @property
    def events(self) -> tuple[SkillRegistryEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def _set_active(
        self, name: str, version: str, event_type: SkillRegistryEventType
    ) -> SkillPackage:
        with self._lock:
            package = self._revalidate_package(self.get(name, version))
            self._validate_active_invocation(name, version)
            previous = self._active_versions.get(name)
            if previous == version:
                return package
            self._active_versions[name] = version
            self._events.append(self._event(event_type, package, previous))
            return package

    def _validate_active_invocation(self, candidate_name: str, candidate_version: str) -> None:
        commands: dict[str, tuple[str, str]] = {}
        selected = dict(self._active_versions)
        selected[candidate_name] = candidate_version
        for name, version in selected.items():
            package = self._packages.get((name, version))
            if package is None:
                continue
            invocation = package.manifest.invocation
            if invocation is None:
                continue
            for command in invocation.commands:
                previous = commands.get(command)
                identity = (package.manifest.name, package.manifest.version)
                if previous is not None and previous != identity:
                    raise SkillRegistryError(
                        SkillRegistryErrorCode.INVALID_MANIFEST,
                        "Skill invocation command or alias is not globally unique.",
                    )
                commands[command] = identity

    @staticmethod
    def _event(
        event_type: SkillRegistryEventType,
        package: SkillPackage,
        previous_version: str | None = None,
    ) -> SkillRegistryEvent:
        return SkillRegistryEvent(
            event_version=1,
            event_type=event_type,
            name=package.manifest.name,
            version=package.manifest.version,
            content_sha256=package.content_sha256,
            previous_version=previous_version,
        )

    def _resolve_package_root(self, relative_path: str | Path) -> Path:
        relative = Path(relative_path)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise SkillRegistryError(
                SkillRegistryErrorCode.PATH_OUTSIDE_TRUSTED_ROOT,
                "Skill package path must stay inside a trusted root.",
            )
        for root in self._roots():
            current = root
            for part in relative.parts:
                current = current / part
                if current.is_symlink() or current.is_junction():
                    raise SkillRegistryError(
                        SkillRegistryErrorCode.LINK_NOT_ALLOWED,
                        "Symbolic links and junctions are not allowed in Skill packages.",
                    )
            try:
                resolved = current.resolve(strict=True)
            except OSError:
                continue
            if resolved.is_relative_to(root) and resolved.is_dir():
                return resolved
        raise SkillRegistryError(
            SkillRegistryErrorCode.PATH_OUTSIDE_TRUSTED_ROOT,
            "Skill package path must stay inside a trusted root.",
        )

    def _collect_files(self, package_root: Path) -> tuple[Path, ...]:
        files: list[Path] = []
        pending = [package_root]
        while pending:
            directory = pending.pop()
            for child in directory.iterdir():
                self._reject_link(child)
                if child.is_dir():
                    if child.name not in _TRANSIENT_DIRS:
                        pending.append(child)
                elif child.is_file():
                    if (
                        child.name not in _TRANSIENT_NAMES
                        and child.suffix not in _TRANSIENT_SUFFIXES
                    ):
                        files.append(child)
                else:
                    raise SkillRegistryError(
                        SkillRegistryErrorCode.INVALID_PACKAGE,
                        "Skill package contains an unsupported filesystem entry.",
                    )
        return tuple(sorted(files, key=lambda path: path.relative_to(package_root).as_posix()))

    def _load_manifest(self, path: Path) -> SkillManifest:
        manifest_text = self._normalized_content(path).decode("utf-8")
        try:
            if any(isinstance(event, AliasEvent) for event in yaml.parse(manifest_text)):
                raise SkillRegistryError(
                    SkillRegistryErrorCode.INVALID_MANIFEST,
                    "Skill manifest YAML aliases are not allowed.",
                )
            loaded: Any = yaml.safe_load(manifest_text)
        except yaml.YAMLError as exc:
            raise SkillRegistryError(
                SkillRegistryErrorCode.INVALID_MANIFEST,
                "Skill manifest YAML is invalid.",
            ) from exc
        if not isinstance(loaded, dict):
            raise SkillRegistryError(
                SkillRegistryErrorCode.INVALID_MANIFEST,
                "Skill manifest must be an object.",
            )
        data = cast(dict[str, Any], loaded)
        error = next(Draft202012Validator(SKILL_MANIFEST_SCHEMA).iter_errors(data), None)
        if error is not None:
            location = "/".join(str(part) for part in error.absolute_path) or "root"
            raise SkillRegistryError(
                SkillRegistryErrorCode.INVALID_MANIFEST,
                f"Skill manifest validation failed at {location}.",
            )
        manifest_version = cast(str, data["manifest_version"])
        raw_invocation = data.get("invocation")
        if manifest_version == "2" and not isinstance(raw_invocation, dict):
            raise SkillRegistryError(
                SkillRegistryErrorCode.INVALID_MANIFEST,
                "Manifest v2 requires invocation metadata.",
            )
        invocation = _parse_invocation(raw_invocation) if raw_invocation is not None else None
        tools = cast(list[dict[str, str]], data["required_tools"])
        budgets = cast(dict[str, int], data["budgets"])
        compatibility = cast(dict[str, Any], data["compatibility"])
        return SkillManifest(
            manifest_version=manifest_version,
            name=cast(str, data["name"]),
            version=cast(str, data["version"]),
            description=cast(str, data["description"]),
            input_schema=cast(str, data["input_schema"]),
            output_schema=cast(str, data["output_schema"]),
            required_tools=tuple(ToolRef(tool["name"], tool["version"]) for tool in tools),
            required_capabilities=frozenset(cast(list[str], data["required_capabilities"])),
            permissions=frozenset(
                ToolPermission(value) for value in cast(list[str], data["permissions"])
            ),
            budgets=RunBudget(**budgets),
            entrypoint=cast(str, data["entrypoint"]),
            compatibility=SkillCompatibility(
                runtime=cast(str, compatibility["runtime"]),
                checkpoint_schema_versions=tuple(
                    cast(list[int], compatibility["checkpoint_schema_versions"])
                ),
            ),
            prompts=tuple(cast(list[str], data["prompts"])),
            evals=tuple(cast(list[str], data["evals"])),
            invocation=invocation,
        )

    @classmethod
    def _validate_eval_cases(cls, path: Path) -> None:
        """Validate a JSONL eval case file against EVAL_CASE_SCHEMA at package load."""
        content = cls._normalized_content(path).decode("utf-8")
        validator = Draft202012Validator(EVAL_CASE_SCHEMA)
        for line_number, line in enumerate(content.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                loaded: Any = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SkillRegistryError(
                    SkillRegistryErrorCode.INVALID_MANIFEST,
                    f"Skill eval case JSON is invalid on line {line_number}: {path.name}",
                ) from exc
            if not isinstance(loaded, dict):
                raise SkillRegistryError(
                    SkillRegistryErrorCode.INVALID_MANIFEST,
                    f"Skill eval cases must be JSON objects on line {line_number}: {path.name}",
                )
            error = next(validator.iter_errors(loaded), None)
            if error is not None:
                location = "/".join(str(part) for part in error.absolute_path) or "root"
                raise SkillRegistryError(
                    SkillRegistryErrorCode.INVALID_MANIFEST,
                    f"Skill eval case validation failed at {location}: {path.name}",
                )

    def _load_json_schema(
        self,
        package_root: Path,
        reference: str,
        seen: set[Path] | None = None,
    ) -> Mapping[str, JSONValue]:
        path = self._resolve_package_file(package_root, reference)
        visited = seen if seen is not None else set()
        if path in visited:
            return {}
        visited.add(path)
        try:
            loaded: Any = json.loads(self._normalized_content(path))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise SkillRegistryError(
                SkillRegistryErrorCode.INVALID_SCHEMA,
                "Skill JSON Schema is not valid UTF-8 JSON.",
            ) from exc
        if not isinstance(loaded, dict):
            raise SkillRegistryError(
                SkillRegistryErrorCode.INVALID_SCHEMA,
                "Skill JSON Schema must be an object.",
            )
        schema = cast(dict[str, JSONValue], loaded)
        try:
            Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise SkillRegistryError(
                SkillRegistryErrorCode.INVALID_SCHEMA,
                "Skill JSON Schema is invalid.",
            ) from exc
        for schema_ref in self._schema_references(schema):
            parsed = urlsplit(schema_ref)
            if parsed.scheme or parsed.netloc:
                raise SkillRegistryError(
                    SkillRegistryErrorCode.PATH_OUTSIDE_TRUSTED_ROOT,
                    "Remote JSON Schema references are not allowed.",
                )
            if parsed.path:
                parent = path.parent.relative_to(package_root).as_posix()
                nested_reference = f"{parent}/{parsed.path}" if parent != "." else parsed.path
                self._load_json_schema(package_root, nested_reference, visited)
        return schema

    def _revalidate_package(self, package: SkillPackage) -> SkillPackage:
        try:
            resolved = package.root.resolve(strict=True)
        except OSError as exc:
            raise SkillRegistryError(
                SkillRegistryErrorCode.PATH_OUTSIDE_TRUSTED_ROOT,
                "Skill package is outside the configured trusted roots.",
            ) from exc
        root = self._root_for(resolved)
        if root is None:
            raise SkillRegistryError(
                SkillRegistryErrorCode.PATH_OUTSIDE_TRUSTED_ROOT,
                "Skill package is outside the configured trusted roots.",
            )
        relative = resolved.relative_to(root)
        fresh = self.load(relative)
        if (
            fresh.manifest.name != package.manifest.name
            or fresh.manifest.version != package.manifest.version
            or fresh.content_sha256 != package.content_sha256
        ):
            raise SkillRegistryError(
                SkillRegistryErrorCode.DIGEST_MISMATCH,
                "Skill package content changed after validation.",
            )
        return fresh

    @classmethod
    def _schema_references(cls, value: JSONValue) -> tuple[str, ...]:
        found: list[str] = []
        if isinstance(value, dict):
            reference = value.get("$ref")
            if isinstance(reference, str):
                found.append(reference)
            for child in value.values():
                found.extend(cls._schema_references(child))
        elif isinstance(value, list):
            for child in value:
                found.extend(cls._schema_references(child))
        return tuple(found)

    def _resolve_package_file(self, package_root: Path, reference: str) -> Path:
        if "\\" in reference:
            raise SkillRegistryError(
                SkillRegistryErrorCode.PATH_OUTSIDE_TRUSTED_ROOT,
                "Skill file references must use package-relative POSIX paths.",
            )
        relative = PurePosixPath(reference)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise SkillRegistryError(
                SkillRegistryErrorCode.PATH_OUTSIDE_TRUSTED_ROOT,
                "Skill file reference escapes the package.",
            )
        candidate = package_root.joinpath(*relative.parts)
        current = package_root
        for part in relative.parts:
            current = current / part
            self._reject_link(current)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise SkillRegistryError(
                SkillRegistryErrorCode.INVALID_PACKAGE,
                "Skill references a missing package file.",
            ) from exc
        if not resolved.is_relative_to(package_root) or not resolved.is_file():
            raise SkillRegistryError(
                SkillRegistryErrorCode.PATH_OUTSIDE_TRUSTED_ROOT,
                "Skill file reference escapes the package.",
            )
        return resolved

    def _validate_compatibility(self, compatibility: SkillCompatibility) -> None:
        try:
            runtime_range = SpecifierSet(compatibility.runtime)
        except InvalidSpecifier as exc:
            raise SkillRegistryError(
                SkillRegistryErrorCode.INVALID_MANIFEST,
                "Skill runtime compatibility range is invalid.",
            ) from exc
        if self._runtime_version not in runtime_range:
            raise SkillRegistryError(
                SkillRegistryErrorCode.INCOMPATIBLE,
                "Skill is incompatible with this Runtime version.",
            )
        if self._checkpoint_schema_version not in compatibility.checkpoint_schema_versions:
            raise SkillRegistryError(
                SkillRegistryErrorCode.INCOMPATIBLE,
                "Skill is incompatible with this checkpoint schema version.",
            )

    @classmethod
    def _digest_files(
        cls, package_root: Path, files: tuple[Path, ...]
    ) -> tuple[tuple[tuple[str, str], ...], str]:
        package_hasher = hashlib.sha256()
        digests: list[tuple[str, str]] = []
        total_bytes = 0
        for path in files:
            relative = path.relative_to(package_root).as_posix()
            content = cls._normalized_content(path)
            total_bytes += len(content)
            if total_bytes > _MAX_PACKAGE_BYTES:
                raise SkillRegistryError(
                    SkillRegistryErrorCode.INVALID_PACKAGE,
                    "Skill package exceeds the size limit.",
                )
            digest = hashlib.sha256(content).hexdigest()
            digests.append((relative, digest))
            package_hasher.update(relative.encode("utf-8"))
            package_hasher.update(b"\0")
            package_hasher.update(str(len(content)).encode("ascii"))
            package_hasher.update(b"\0")
            package_hasher.update(content)
            package_hasher.update(b"\0")
        return tuple(digests), package_hasher.hexdigest()

    @staticmethod
    def _normalized_content(path: Path) -> bytes:
        data = path.read_bytes()
        if len(data) > _MAX_FILE_BYTES:
            raise SkillRegistryError(
                SkillRegistryErrorCode.INVALID_PACKAGE,
                "Skill package file exceeds the size limit.",
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillRegistryError(
                SkillRegistryErrorCode.INVALID_PACKAGE,
                "Skill package files must be UTF-8 text.",
            ) from exc
        return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")

    @staticmethod
    def _reject_link(path: Path) -> None:
        if path.is_symlink() or path.is_junction():
            raise SkillRegistryError(
                SkillRegistryErrorCode.LINK_NOT_ALLOWED,
                "Symbolic links and junctions are not allowed in Skill packages.",
            )


class PersonalSkillRegistry(FileSystemSkillRegistry):
    """Writable personal-Skill registry reusing all trusted validation rules.

    Personal Skills are low-trust, user-owned packages: they may only compose
    declarative workflows and already-registered handlers/tools, never new
    Python behavior (ADR-018). Write operations validate every file through the
    base loader before publishing, so path traversal, symbolic links, remote
    ``$ref``, and manifest/schema/eval failures are all rejected.

    Phase 4 drafts live in a ``_drafts`` subdirectory of the personal root: the
    underscore prefix keeps them out of ``reload()`` scanning while the shared
    path-safety checks still apply. A draft is promoted to a real personal Skill
    only after it passes the full validation suite.
    """

    _DRAFT_DIR = "_drafts"

    def create_personal(self, name: str, files: Mapping[str, str]) -> SkillPackage:
        with self._lock:
            root = self._require_personal_root()
            _validate_personal_name(name)
            if self.is_builtin(name):
                raise SkillRegistryError(
                    SkillRegistryErrorCode.NAME_CONFLICT,
                    "Personal Skill cannot override a built-in Skill name.",
                )
            if self.is_personal(name):
                raise SkillRegistryError(
                    SkillRegistryErrorCode.NAME_CONFLICT,
                    "Personal Skill name already exists.",
                )
            package_dir = root / name
            package_dir.mkdir(parents=True, exist_ok=False)
            try:
                self._write_package_files(package_dir, files)
                package = self._load_personal_package(name)
                self.register(package)
                self._origins[(package.manifest.name, package.manifest.version)] = root
                return package
            except Exception:
                shutil.rmtree(package_dir, ignore_errors=True)
                raise

    def update_personal(self, name: str, files: Mapping[str, str]) -> SkillPackage:
        with self._lock:
            root = self._require_personal_root()
            self._require_personal_exists(name)
            if self._active_versions.get(name) is not None:
                raise SkillRegistryError(
                    SkillRegistryErrorCode.CLEANUP_BLOCKED,
                    "An active personal Skill cannot be updated.",
                )
            package_dir = root / name
            staging = root / f"_{name}_staging"
            if staging.exists():
                shutil.rmtree(staging)
            staging.mkdir(parents=True, exist_ok=False)
            try:
                self._write_package_files(staging, files)
                staged = self.load(staging.name)
                if staged.manifest.name != name:
                    raise SkillRegistryError(
                        SkillRegistryErrorCode.INVALID_MANIFEST,
                        "Personal Skill manifest name does not match its directory.",
                    )
                self._remove_personal_records(name)
                shutil.rmtree(package_dir, ignore_errors=True)
                staging.replace(package_dir)
                package = self.load(name)
                self.register(package)
                self._origins[(package.manifest.name, package.manifest.version)] = root
                return package
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)

    def delete_personal(self, name: str) -> SkillPackage | None:
        with self._lock:
            root = self._require_personal_root()
            self._require_personal_exists(name)
            if self._active_versions.get(name) is not None:
                raise SkillRegistryError(
                    SkillRegistryErrorCode.CLEANUP_BLOCKED,
                    "An active personal Skill cannot be deleted.",
                )
            removed = self._remove_personal_records(name)
            shutil.rmtree(root / name, ignore_errors=True)
            return removed[-1] if removed else None

    def create_draft(self, name: str, files: Mapping[str, str]) -> None:
        """Scaffold a writable draft under ``_drafts/<name>`` (no full validation).

        Drafts are the Phase 4 creator's in-progress packages: they may be
        incomplete, so only path safety is enforced here. Built-in and personal
        Skill names stay reserved, matching the ADR-018 priority rule.
        """
        with self._lock:
            root = self._require_personal_root()
            _validate_personal_name(name)
            if self.is_builtin(name):
                raise SkillRegistryError(
                    SkillRegistryErrorCode.NAME_CONFLICT,
                    "A draft cannot shadow a built-in Skill name.",
                )
            if self.is_personal(name):
                raise SkillRegistryError(
                    SkillRegistryErrorCode.NAME_CONFLICT,
                    "A draft cannot shadow an installed personal Skill name.",
                )
            draft_dir = self._draft_dir(root, name)
            if draft_dir.exists():
                raise SkillRegistryError(
                    SkillRegistryErrorCode.NAME_CONFLICT,
                    "A draft with this name already exists.",
                )
            draft_dir.mkdir(parents=True, exist_ok=False)
            try:
                self._write_draft_files(draft_dir, files)
            except Exception:
                shutil.rmtree(draft_dir, ignore_errors=True)
                raise

    def update_draft(self, name: str, files: Mapping[str, str]) -> None:
        """Write or overwrite files inside an existing draft, path-safe only."""
        with self._lock:
            root = self._require_personal_root()
            draft_dir = self._require_draft_dir(root, name)
            self._write_draft_files(draft_dir, files)

    def draft_names(self) -> tuple[str, ...]:
        if self._personal_root is None:
            return ()
        draft_root = self._personal_root / self._DRAFT_DIR
        if not draft_root.is_dir():
            return ()
        return tuple(
            sorted(
                child.name
                for child in draft_root.iterdir()
                if child.is_dir() and not child.name.startswith("_")
            )
        )

    def is_draft(self, name: str) -> bool:
        if self._personal_root is None:
            return False
        return (self._personal_root / self._DRAFT_DIR / name).is_dir()

    def read_draft_files(self, name: str) -> dict[str, str]:
        with self._lock:
            root = self._require_personal_root()
            draft_dir = self._require_draft_dir(root, name)
            files: dict[str, str] = {}
            for path in sorted(draft_dir.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(draft_dir).as_posix()
                files[relative] = path.read_text(encoding="utf-8")
            return files

    def delete_draft(self, name: str) -> None:
        with self._lock:
            root = self._require_personal_root()
            draft_dir = self._require_draft_dir(root, name)
            for candidate, version in list(self._packages):
                if candidate == name:
                    self._packages.pop((candidate, version), None)
                    self._origins.pop((candidate, version), None)
            shutil.rmtree(draft_dir, ignore_errors=True)

    def validate_draft(self, name: str) -> SkillPackage:
        """Run the full trusted validation suite over one draft package.

        A validated draft is registered in-process (never persisted as a
        personal Skill) so the runtime executor can pin and run it for the
        deterministic eval gate.
        """
        with self._lock:
            root = self._require_personal_root()
            self._require_draft_dir(root, name)
            package = self._revalidate_package(self.load(f"{self._DRAFT_DIR}/{name}"))
            return self.register(package)

    def promote_draft(self, name: str) -> SkillPackage:
        """Publish a validated draft as an installed personal Skill.

        The draft must pass the full trusted validation suite; the resulting
        package is registered through ``create_personal`` (which re-checks the
        built-in/personal name rules) and the draft directory is removed.
        """
        with self._lock:
            root = self._require_personal_root()
            self._require_draft_dir(root, name)
            # Drop any transient in-process registration from prior draft
            # validation so the published package owns the (name, version) slot
            # and points at the personal root, not the soon-deleted draft dir.
            for candidate, version in list(self._packages):
                if candidate == name and self._origins.get((candidate, version)) != root:
                    self._packages.pop((candidate, version), None)
                    self._origins.pop((candidate, version), None)
            files = self.read_draft_files(name)
            package = self.create_personal(name, files)
            shutil.rmtree(root / self._DRAFT_DIR / name, ignore_errors=True)
            return package

    def _require_draft_dir(self, root: Path, name: str) -> Path:
        draft_dir = self._draft_dir(root, name)
        if not draft_dir.is_dir():
            raise SkillRegistryError(
                SkillRegistryErrorCode.NOT_FOUND,
                "Skill draft is not installed.",
            )
        return draft_dir

    @staticmethod
    def _draft_dir(root: Path, name: str) -> Path:
        return root / PersonalSkillRegistry._DRAFT_DIR / name

    def _write_draft_files(self, draft_dir: Path, files: Mapping[str, str]) -> None:
        if not files:
            raise SkillRegistryError(
                SkillRegistryErrorCode.INVALID_PACKAGE,
                "A Skill draft requires at least one file.",
            )
        for relative, content in files.items():
            path = self._safe_personal_relative(draft_dir, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def activate_all(self, activations: Mapping[str, str]) -> None:
        """Apply a persisted (name, version) mapping for personal Skills."""
        with self._lock:
            for name, version in activations.items():
                if not self.is_personal(name):
                    continue
                if version not in self.versions(name):
                    raise SkillRegistryError(
                        SkillRegistryErrorCode.NOT_FOUND,
                        "Persisted personal Skill activation is not installed.",
                    )
                self.activate(name, version)

    def _require_personal_root(self) -> Path:
        if self._personal_root is None:
            raise SkillRegistryError(
                SkillRegistryErrorCode.INVALID_PACKAGE,
                "Personal Skill root is not configured.",
            )
        self._personal_root.mkdir(parents=True, exist_ok=True)
        return self._personal_root

    def _require_personal_exists(self, name: str) -> None:
        if not self.is_personal(name):
            raise SkillRegistryError(
                SkillRegistryErrorCode.NOT_FOUND,
                "Personal Skill is not installed.",
            )

    def _load_personal_package(self, name: str) -> SkillPackage:
        package = self.load(name)
        if package.manifest.name != name:
            raise SkillRegistryError(
                SkillRegistryErrorCode.INVALID_MANIFEST,
                "Personal Skill manifest name does not match its directory.",
            )
        return package

    def _remove_personal_records(self, name: str) -> list[SkillPackage]:
        removed: list[SkillPackage] = []
        for (candidate, version), root in list(self._origins.items()):
            if candidate == name and root == self._personal_root:
                self._origins.pop((candidate, version))
                package = self._packages.pop((candidate, version), None)
                if package is not None:
                    removed.append(package)
        return removed

    def _write_package_files(self, package_dir: Path, files: Mapping[str, str]) -> None:
        if not files or "skill.yaml" not in files:
            raise SkillRegistryError(
                SkillRegistryErrorCode.INVALID_PACKAGE,
                "Personal Skill package requires skill.yaml.",
            )
        for relative, content in files.items():
            path = self._safe_personal_relative(package_dir, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    @staticmethod
    def _safe_personal_relative(package_dir: Path, relative: str) -> Path:
        if "\\" in relative:
            raise SkillRegistryError(
                SkillRegistryErrorCode.PATH_OUTSIDE_TRUSTED_ROOT,
                "Personal Skill file paths must use POSIX separators.",
            )
        pure = PurePosixPath(relative)
        if pure.is_absolute() or not pure.parts or ".." in pure.parts:
            raise SkillRegistryError(
                SkillRegistryErrorCode.PATH_OUTSIDE_TRUSTED_ROOT,
                "Personal Skill file path escapes the package.",
            )
        candidate = package_dir.joinpath(*pure.parts)
        if not candidate.is_relative_to(package_dir):
            raise SkillRegistryError(
                SkillRegistryErrorCode.PATH_OUTSIDE_TRUSTED_ROOT,
                "Personal Skill file path escapes the package.",
            )
        return candidate


def _validate_personal_name(name: str) -> None:
    if re.fullmatch(r"[a-z][a-z0-9_]*", name) is None:
        raise SkillRegistryError(
            SkillRegistryErrorCode.INVALID_MANIFEST,
            "Personal Skill name is invalid.",
        )
