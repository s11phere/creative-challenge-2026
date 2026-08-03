"""Trusted, immutable Skill package loading and version registration."""

from __future__ import annotations

import hashlib
import json
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
        "manifest_version": {"const": "1"},
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
    },
}


class SkillRegistryErrorCode(StrEnum):
    INVALID_MANIFEST = "SKILL_INVALID_MANIFEST"
    INVALID_PACKAGE = "SKILL_INVALID_PACKAGE"
    INVALID_SCHEMA = "SKILL_INVALID_SCHEMA"
    PATH_OUTSIDE_TRUSTED_ROOT = "SKILL_PATH_OUTSIDE_TRUSTED_ROOT"
    LINK_NOT_ALLOWED = "SKILL_LINK_NOT_ALLOWED"
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
    """Registry that loads declaration-only Skill packages from one trusted root."""

    def __init__(
        self,
        trusted_root: Path,
        *,
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
        self._checkpoint_schema_version = checkpoint_schema_version
        self._packages: dict[tuple[str, str], SkillPackage] = {}
        self._active_versions: dict[str, str] = {}
        self._events: list[SkillRegistryEvent] = []
        self._lock = RLock()

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
        if PurePosixPath(manifest.entrypoint).suffix not in {".yaml", ".yml", ".json"}:
            raise SkillRegistryError(
                SkillRegistryErrorCode.INVALID_MANIFEST,
                "Skill entrypoint must be a declarative YAML or JSON workflow.",
            )
        self._load_json_schema(package_root, manifest.input_schema)
        self._load_json_schema(package_root, manifest.output_schema)
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
            for child in sorted(self._trusted_root.iterdir(), key=lambda path: path.name):
                if child.name.startswith("_"):
                    continue
                self._reject_link(child)
                if child.is_dir():
                    candidates.append(self._revalidate_package(self.load(child.name)))

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
            previous = self._active_versions.get(name)
            if previous == version:
                return package
            self._active_versions[name] = version
            self._events.append(self._event(event_type, package, previous))
            return package

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
                "Skill package path must stay inside the trusted root.",
            )
        candidate = self._trusted_root.joinpath(relative)
        current = self._trusted_root
        for part in relative.parts:
            current = current / part
            self._reject_link(current)
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise SkillRegistryError(
                SkillRegistryErrorCode.INVALID_PACKAGE,
                "Skill package path does not exist.",
            ) from exc
        if not resolved.is_relative_to(self._trusted_root) or not resolved.is_dir():
            raise SkillRegistryError(
                SkillRegistryErrorCode.PATH_OUTSIDE_TRUSTED_ROOT,
                "Skill package path must stay inside the trusted root.",
            )
        return resolved

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
        tools = cast(list[dict[str, str]], data["required_tools"])
        budgets = cast(dict[str, int], data["budgets"])
        compatibility = cast(dict[str, Any], data["compatibility"])
        return SkillManifest(
            manifest_version=cast(str, data["manifest_version"]),
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
            relative = package.root.resolve(strict=True).relative_to(self._trusted_root)
        except (OSError, ValueError) as exc:
            raise SkillRegistryError(
                SkillRegistryErrorCode.PATH_OUTSIDE_TRUSTED_ROOT,
                "Skill package is outside the configured trusted root.",
            ) from exc
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
