"""Embedding, index, validate, and publish service.

Orchestrates the EMBED → INDEX → VALIDATE → PUBLISH pipeline for a
single document version.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID, uuid4

from domain.chunking import ChunkOutput as ChunkerOutput
from domain.embedding import EmbeddingIdentity, compute_processing_config_hash
from domain.models import (
    Chunk,
    Document,
    DocumentStatus,
    DocumentVersion,
)
from domain.repositories import (
    ChunkRepository,
    DocumentRepository,
    DocumentVersionRepository,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Port for text embedding
# ---------------------------------------------------------------------------


class TextEmbedder(Protocol):
    """Minimal interface for embedding text into vectors.

    The ``application`` layer cannot depend on the ``model_gateway``
    package directly, so this Protocol defines what the service needs.
    Callers adapt the concrete ModelGateway to this interface.
    """

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        """Embed *texts* and return a vector per input text.

        Each inner tuple has the same dimensionality (768 for the
        ADR-005 fixed schema).  The implementation is responsible for
        batching, error handling, and retries.
        """
        ...


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbeddingConfig:
    """Runtime configuration for the embedding pipeline."""

    batch_size: int = 32
    """Maximum number of texts to send in a single ModelGateway call."""

    max_empty_text_ratio: float = 0.1
    """Maximum allowed ratio of chunks whose text is empty or
    whitespace-only after embedding.  Exceeding this fails validation."""

    embedding_identity: EmbeddingIdentity = field(default_factory=EmbeddingIdentity)

    document_prefix: str = ""
    """Instruction prefix prepended to each chunk text before embedding.

    For instruction-tuned embedding models (e.g. Qwen3-Embedding) this
    should be the asymmetric ``Document:`` prefix that matches the
    ``Query:`` prefix applied at retrieval time.  An empty string means
    no prefix (``none-v1`` behaviour).
    """


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbeddingPipelineResult:
    """Outcome of the full embed → index → validate → publish pipeline."""

    version: DocumentVersion
    """The updated document version with final status."""

    chunk_count: int
    """Number of chunks indexed."""

    total_embedding_tokens: int
    """Total tokens consumed across all embedding calls (0 for fake)."""

    total_latency_ms: float
    """Total wall-clock time spent in ModelGateway embedding calls."""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class EmbeddingService:
    """Application service for the EMBED → INDEX → VALIDATE → PUBLISH pipeline.

    Dependencies (constructor-injected)
    ------------------------------------
    ``text_embedder``
        Adapter to the ModelGateway's ``embed()``.
    ``chunk_repo``
        Persistence for ``Chunk`` entities (batch write / delete).
    ``version_repo``
        Persistence for ``DocumentVersion`` entities (status updates).
    ``document_repo``
        Persistence for ``Document`` entities (``current_version_id``).
    """

    def __init__(
        self,
        text_embedder: TextEmbedder,
        chunk_repo: ChunkRepository,
        version_repo: DocumentVersionRepository,
        document_repo: DocumentRepository,
    ) -> None:
        self._embedder = text_embedder
        self._chunk_repo = chunk_repo
        self._version_repo = version_repo
        self._document_repo = document_repo

    async def embed_and_publish(
        self,
        document: Document,
        version: DocumentVersion,
        chunk_outputs: tuple[ChunkerOutput, ...],
        *,
        config: EmbeddingConfig | None = None,
    ) -> EmbeddingPipelineResult:
        """Run the full embedding pipeline for *chunk_outputs*.

        Parameters
        ----------
        document:
            The ``Document`` that owns this version.  Its
            ``current_version_id`` will be updated on successful publish.
        version:
            The ``DocumentVersion`` to embed, index, and publish.
            The version's status is updated as the pipeline progresses.
        chunk_outputs:
            Ordered chunk outputs from the chunker.
        config:
            Pipeline configuration (batch size, validation thresholds).
            Defaults are used when ``None``.

        Returns
        -------
        EmbeddingPipelineResult
            The published version and pipeline statistics.

        Raises
        ------
        ValueError
            If validation fails (wrong vector dimensions, too many empty
            chunks).
        RuntimeError
            If the ModelGateway call fails non-recoverably.
        """
        cfg = config or EmbeddingConfig()
        prefix = cfg.document_prefix

        # ------------------------------------------------------------------
        # EMBED: call ModelGateway in batches
        # ------------------------------------------------------------------
        chunk_texts = []
        for c in chunk_outputs:
            enriched = c.text
            if c.heading_path:
                enriched = f"[{c.heading_path}]\n{enriched}"
            chunk_texts.append(f"{prefix}{enriched}" if prefix else enriched)
        vectors: list[tuple[float, ...]] = []

        # Model gateway contracts reject every empty request item. Empty
        # chunks indicate a parser/chunker contract violation, regardless of
        # their ratio within the document.
        if not chunk_texts:
            raise ValueError("Embedding input must contain at least one non-empty chunk")
        empty_count = sum(1 for text in chunk_texts if not text.strip())
        if empty_count:
            raise ValueError(
                "Embedding input contains empty-text chunks "
                f"({empty_count}/{len(chunk_texts)} chunks empty)"
            )

        total_tokens = 0
        total_latency = 0.0

        for i in range(0, len(chunk_texts), cfg.batch_size):
            batch = tuple(chunk_texts[i : i + cfg.batch_size])
            batch_vectors = await self._embedder.embed(batch)

            if len(batch_vectors) != len(batch):
                raise RuntimeError(
                    f"Embedding returned {len(batch_vectors)} vectors "
                    f"for {len(batch)} texts (batch offset {i})"
                )

            vectors.extend(cfg.embedding_identity.normalize_vectors(batch_vectors))

        self._validate(vectors, chunk_outputs, cfg)

        # ------------------------------------------------------------------
        # INDEX: build Chunk entities and persist
        # ------------------------------------------------------------------
        domain_chunks = self._build_chunks(version.id, chunk_outputs, vectors)

        # Idempotent write: delete-existing-then-insert (full replacement)
        await self._chunk_repo.delete_by_version(version.id)
        await self._chunk_repo.create_batch(domain_chunks)

        # Update version status to EMBEDDED
        version = version  # already the latest — update status in DB
        processing_config = {
            **version.processing_config,
            **cfg.embedding_identity.processing_config(),
        }
        processing_config_hash = compute_processing_config_hash(processing_config)
        updated_version = await self._version_repo.update(
            DocumentVersion(
                id=version.id,
                document_id=version.document_id,
                blob_hash=version.blob_hash,
                content_hash=version.content_hash,
                parser_version=version.parser_version,
                normalizer_version=version.normalizer_version,
                chunker_version=version.chunker_version,
                embedding_version=cfg.embedding_identity.version,
                processing_config_hash=processing_config_hash,
                processing_config=processing_config,
                status=DocumentStatus.EMBEDDED,
                file_path=version.file_path,
                created_at=version.created_at,
            )
        )

        # ------------------------------------------------------------------
        # PUBLISH: atomic version switch
        # ------------------------------------------------------------------
        published_version = await self._version_repo.update(
            DocumentVersion(
                id=updated_version.id,
                document_id=updated_version.document_id,
                blob_hash=updated_version.blob_hash,
                content_hash=updated_version.content_hash,
                parser_version=updated_version.parser_version,
                normalizer_version=updated_version.normalizer_version,
                chunker_version=updated_version.chunker_version,
                embedding_version=cfg.embedding_identity.version,
                processing_config_hash=updated_version.processing_config_hash,
                processing_config=updated_version.processing_config,
                status=DocumentStatus.PUBLISHED,
                file_path=updated_version.file_path,
                created_at=updated_version.created_at,
            )
        )

        # Update document's current_version_id
        published_doc = Document(
            id=document.id,
            source_id=document.source_id,
            stable_key=document.stable_key,
            current_version_id=published_version.id,
            deleted_at=document.deleted_at,
            created_at=document.created_at,
            updated_at=document.updated_at,
        )
        await self._document_repo.update(published_doc)

        logger.info(
            "Published version %s for document %s (%d chunks, %d tokens)",
            published_version.id,
            document.id,
            len(domain_chunks),
            total_tokens,
        )

        return EmbeddingPipelineResult(
            version=published_version,
            chunk_count=len(domain_chunks),
            total_embedding_tokens=total_tokens,
            total_latency_ms=total_latency,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_chunks(
        version_id: UUID,
        outputs: tuple[ChunkerOutput, ...],
        vectors: list[tuple[float, ...]],
    ) -> list[Chunk]:
        """Map chunker outputs + embedding vectors to domain ``Chunk`` entities."""
        if len(outputs) != len(vectors):
            raise RuntimeError(f"Got {len(outputs)} chunk outputs but {len(vectors)} vectors")

        chunks: list[Chunk] = []
        for co, vec in zip(outputs, vectors, strict=True):
            meta: dict[str, str] = {}
            if co.heading_path:
                meta["heading_path"] = co.heading_path
            if co.start_line:
                meta["start_line"] = str(co.start_line)
            if co.end_line:
                meta["end_line"] = str(co.end_line)
            if co.start_page is not None:
                meta["start_page"] = str(co.start_page)
            if co.end_page is not None:
                meta["end_page"] = str(co.end_page)
            if co.node_type:
                meta["node_type"] = co.node_type
            if co.parent_ordinal is not None:
                meta["parent_ordinal"] = str(co.parent_ordinal)
            if co.prev_ordinal is not None:
                meta["prev_ordinal"] = str(co.prev_ordinal)
            if co.next_ordinal is not None:
                meta["next_ordinal"] = str(co.next_ordinal)

            chunks.append(
                Chunk(
                    id=uuid4(),
                    version_id=version_id,
                    ordinal=co.ordinal,
                    chunk_hash=co.chunk_hash,
                    text=co.text,
                    meta=meta,
                    embedding=list(vec),
                )
            )
        return chunks

    @staticmethod
    def _validate(
        vectors: list[tuple[float, ...]],
        outputs: tuple[ChunkerOutput, ...],
        config: EmbeddingConfig,
    ) -> None:
        """Validate embedding outputs before publishing.

        Raises ``ValueError`` on any validation failure.
        """
        # --- Vector dimension ---
        for i, vec in enumerate(vectors):
            if len(vec) != config.embedding_identity.dimensions:
                raise ValueError(
                    f"Chunk {i} embedding has {len(vec)} dimensions, "
                    f"expected {config.embedding_identity.dimensions} "
                    f"(fixed by ADR-005)"
                )

        # --- Empty text ratio ---
        if outputs:
            empty_count = sum(1 for c in outputs if not c.text.strip())
            empty_ratio = empty_count / len(outputs)
            if empty_ratio > config.max_empty_text_ratio:
                raise ValueError(
                    f"Empty-text chunk ratio {empty_ratio:.1%} exceeds "
                    f"threshold {config.max_empty_text_ratio:.1%} "
                    f"({empty_count}/{len(outputs)} chunks empty)"
                )

        # --- Chunk count consistency ---
        if len(vectors) != len(outputs):
            raise ValueError(
                f"Vector count ({len(vectors)}) does not match chunk output count ({len(outputs)})"
            )
