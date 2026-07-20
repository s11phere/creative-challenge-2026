"""Source registration and FINGERPRINT stage orchestration.

This module provides the application-level use case for registering a
source file for ingestion.  Responsibilities:

- Create a ``Source`` entity (or accept an existing one).
- Compute ``blob_hash`` (SHA-256 of raw bytes).
- Store raw bytes in a ``BlobStore``, verifying the hash on write.
- Normalise a caller-supplied URI into a ``stable_key``.
- Look up an existing ``Document`` by ``(source_id, stable_key)``.
- If none exists, create a new ``Document``.
- Check whether the target ``Document`` already has a ``DocumentVersion``
  with the same ``blob_hash`` (the FINGERPRINT stage) so repeated imports of
  that document can skip re-processing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from domain.blob_store import BlobStore
from domain.fingerprinting import (
    compute_storage_key,
    normalize_stable_key,
    normalize_stable_key_v1,
)
from domain.models import (
    Document,
    DocumentVersion,
    Source,
    SourceType,
)
from domain.parsing import compute_blob_hash
from domain.repositories import (
    DocumentRepository,
    DocumentVersionRepository,
    SourceRepository,
)

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegistrationResult:
    """Outcome of registering a file for ingestion.

    Attributes
    ----------
    source:
        The ``Source`` the file was registered under (created or existing).
    document:
        The ``Document`` entity the file maps to (created or existing).
    is_new_document:
        ``True`` when a brand‑new ``Document`` was created.
    blob_hash:
        SHA-256 of the raw bytes.
    storage_key:
        BlobStore key where the raw bytes were persisted.
    existing_version:
        If a ``DocumentVersion`` with the same ``blob_hash`` already exists,
        this is that version — downstream stages can short‑circuit.
    version_id:
        The version ID to bind the ingestion task to.  Either a newly
        created ``DocumentVersion`` (for new content) or the matching
        *existing_version* (for unchanged content).
    """

    source: Source = field(default_factory=lambda: Source())
    document: Document = field(default_factory=lambda: Document())
    is_new_document: bool = False
    blob_hash: str = ""
    storage_key: str = ""
    existing_version: DocumentVersion | None = None
    version_id: UUID | None = None


@dataclass(frozen=True)
class RegisteredSource:
    """Minimal result when only a Source is needed (not file registration)."""

    source: Source = field(default_factory=lambda: Source())
    is_new: bool = False


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class SourceRegistrationService:
    """Application service for creating sources and registering files.

    Dependencies (constructor‑injected)
    ------------------------------------
    ``source_repo``
        Persistence for ``Source`` entities.
    ``document_repo``
        Persistence for ``Document`` entities.
    ``version_repo``
        Persistence for ``DocumentVersion`` entities — needed for the
        FINGERPRINT stage's ``blob_hash`` lookup.
    """

    def __init__(
        self,
        source_repo: SourceRepository,
        document_repo: DocumentRepository,
        version_repo: DocumentVersionRepository,
    ) -> None:
        self._source_repo = source_repo
        self._document_repo = document_repo
        self._version_repo = version_repo

    # ------------------------------------------------------------------
    # Source creation
    # ------------------------------------------------------------------

    async def create_source(
        self,
        space_id: UUID,
        *,
        source_type: SourceType = SourceType.UPLOAD,
        uri: str = "",
        source_id: UUID | None = None,
    ) -> RegisteredSource:
        """Create a new ``Source``, or return an existing one if *source_id* is given.

        When *source_id* is provided the method looks up that source and
        returns it as‑is — this lets callers reuse an existing source
        across multiple file registrations.
        """
        if source_id is not None:
            existing = await self._source_repo.get(source_id)
            if existing is not None:
                # Verify space boundary
                if existing.space_id != space_id:
                    raise ValueError(
                        f"Source {source_id} belongs to space {existing.space_id}, "
                        f"not the requested space {space_id}"
                    )
                return RegisteredSource(source=existing, is_new=False)

        source = Source(
            id=source_id or uuid4(),
            space_id=space_id,
            source_type=source_type,
            uri=uri,
        )
        created = await self._source_repo.create(source)
        return RegisteredSource(source=created, is_new=True)

    # ------------------------------------------------------------------
    # File registration (FINGERPRINT stage)
    # ------------------------------------------------------------------

    async def register_file(
        self,
        source: Source,
        raw_bytes: bytes,
        blob_store: BlobStore,
        *,
        file_stable_key: str | None = None,
        file_path: str | None = None,
    ) -> RegistrationResult:
        """Register raw file bytes for ingestion (FINGERPRINT stage).

        Steps
        -----
        1. Compute the ``blob_hash`` of *raw_bytes*.
        2. Derive a server‑side ``storage_key`` and persist the bytes.
        3. Derive a ``stable_key`` from *file_stable_key* (or *file_path*).
        4. Look up an existing ``Document`` under ``(source.id, stable_key)``.
        5. If none exists, create a new ``Document``.
        6. Look up an existing version of that document with the same
           ``blob_hash`` — if found, downstream stages can short-circuit.
        """
        # --- Step 1: hash raw bytes ---
        blob_hash = compute_blob_hash(raw_bytes)

        # --- Step 2: persist blob ---
        storage_key = compute_storage_key(source.id, blob_hash)
        await blob_store.store_and_verify(storage_key, raw_bytes, blob_hash)

        # --- Step 3: derive stable key ---
        stable_key = self._resolve_stable_key(file_stable_key, file_path)

        # --- Step 4: find existing document by stable key ---
        existing_doc = await self._document_repo.get_by_stable_key(source.id, stable_key)

        # Upgrade an ASCII-only v1 key only when the raw bytes prove this is
        # the same logical document. This preserves identity for existing
        # Chinese-named uploads without adopting an unrelated legacy row.
        if existing_doc is None:
            legacy_key = self._resolve_stable_key(
                file_stable_key,
                file_path,
                normalizer=normalize_stable_key_v1,
            )
            if legacy_key != stable_key:
                legacy_doc = await self._document_repo.get_by_stable_key(source.id, legacy_key)
                if legacy_doc is not None:
                    legacy_version = await self._find_version_by_blob_hash(legacy_doc.id, blob_hash)
                    if legacy_version is not None:
                        existing_doc = await self._document_repo.update(
                            replace(legacy_doc, stable_key=stable_key)
                        )

        is_new_doc = False
        if existing_doc is None:
            document = Document(
                source_id=source.id,
                stable_key=stable_key,
            )
            document = await self._document_repo.create(document)
            is_new_doc = True
        else:
            document = existing_doc

        # --- Step 6: fingerprint — find or create version with same blob_hash ---
        existing_version = await self._find_version_by_blob_hash(document.id, blob_hash)
        resolved_version_id: UUID | None = None

        if existing_version is None:
            # Create a DocumentVersion so the ingestion pipeline finds a
            # version with the correct blob_hash (instead of an empty-hash
            # placeholder).  current_version_id is NOT set here — only the
            # PUBLISH stage (in EmbeddingService) makes a version current,
            # preserving ADR-005's atomic‑publish constraint.
            version = DocumentVersion(
                document_id=document.id,
                blob_hash=blob_hash,
                file_path=file_path,
            )
            version = await self._version_repo.create(version)
            resolved_version_id = version.id
        else:
            # Early upload flows did not persist the original file name. Fill
            # that source metadata on a byte-identical re-upload without
            # changing version identity or creating duplicate work.
            if existing_version.file_path is None and file_path:
                existing_version = await self._version_repo.update(
                    replace(existing_version, file_path=file_path)
                )
            resolved_version_id = existing_version.id

        return RegistrationResult(
            source=source,
            document=document,
            is_new_document=is_new_doc,
            blob_hash=blob_hash,
            storage_key=storage_key,
            existing_version=existing_version,
            version_id=resolved_version_id,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_stable_key(
        file_stable_key: str | None,
        file_path: str | None,
        *,
        normalizer: Callable[[str], str] = normalize_stable_key,
    ) -> str:
        """Derive a stable key, preferring an explicit key over a path."""
        if file_stable_key:
            return normalizer(file_stable_key)
        if file_path:
            return normalizer(file_path)
        # Last resort: a timestamp-based key.  This should not happen in
        # normal usage — callers should always provide at least one.
        return normalizer(f"unnamed_{datetime.now(UTC).isoformat()}")

    async def _find_version_by_blob_hash(
        self,
        document_id: UUID,
        blob_hash: str,
    ) -> DocumentVersion | None:
        """Search for an existing ``DocumentVersion`` with the same *blob_hash*.

        ``blob_hash`` is source-scoped for BlobStore reuse, but a
        ``DocumentVersion`` belongs to exactly one logical document. Reusing
        another document's version would bind the ingestion task to the wrong
        owner and violate the ADR-005 processing identity.
        """
        versions = await self._version_repo.get_by_document(document_id)
        for version in versions:
            if version.blob_hash == blob_hash:
                return version
        return None
