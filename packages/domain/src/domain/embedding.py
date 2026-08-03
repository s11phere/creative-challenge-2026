"""Stable identity for document and query embedding outputs."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class EmbeddingIdentity:
    """All settings that can change an embedding vector.

    The short ``version`` is stored on ``DocumentVersion`` while the complete
    values are persisted in ``processing_config`` and covered by its hash.
    """

    model_revision: str = "fake-sha256-v1"
    dimensions: int = 768
    query_instruction_version: str = "none-v1"
    document_instruction_version: str = "none-v1"
    normalization: str = "none"
    precision: str = "float32"

    def __post_init__(self) -> None:
        values = (
            self.model_revision,
            self.query_instruction_version,
            self.document_instruction_version,
            self.normalization,
            self.precision,
        )
        if any(not value.strip() for value in values):
            raise ValueError("Embedding identity values must not be empty")
        if self.dimensions != 768:
            raise ValueError("Embedding dimensions are fixed at 768 by ADR-005")
        if self.normalization not in {"none", "l2"}:
            raise ValueError("Embedding normalization must be 'none' or 'l2'")

    def processing_config(self) -> dict[str, str]:
        """Return the canonical fields persisted with a document version."""
        return {
            "embedding_dimensions": str(self.dimensions),
            "embedding_document_instruction_version": self.document_instruction_version,
            "embedding_model_revision": self.model_revision,
            "embedding_normalization": self.normalization,
            "embedding_precision": self.precision,
            "embedding_query_instruction_version": self.query_instruction_version,
        }

    @classmethod
    def from_processing_config(
        cls,
        config: Mapping[str, object],
    ) -> EmbeddingIdentity | None:
        """Restore an identity persisted on a document version.

        Older pending versions may not have embedding fields yet.  A partial
        identity is rejected because silently falling back to the live
        environment would make a queued rebuild non-deterministic.
        """
        keys = (
            "embedding_dimensions",
            "embedding_document_instruction_version",
            "embedding_model_revision",
            "embedding_normalization",
            "embedding_precision",
            "embedding_query_instruction_version",
        )
        present = [key in config for key in keys]
        if not any(present):
            return None
        if not all(present):
            raise ValueError("Persisted embedding identity is incomplete")
        try:
            return cls(
                dimensions=int(str(config["embedding_dimensions"])),
                document_instruction_version=str(config["embedding_document_instruction_version"]),
                model_revision=str(config["embedding_model_revision"]),
                normalization=str(config["embedding_normalization"]),
                precision=str(config["embedding_precision"]),
                query_instruction_version=str(config["embedding_query_instruction_version"]),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("Persisted embedding identity is invalid") from exc

    @property
    def version(self) -> str:
        """Return a stable, bounded identifier for the full embedding identity."""
        canonical = json.dumps(
            self.processing_config(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"embedding-v1-{digest[:24]}"

    def normalize_vectors(
        self,
        vectors: tuple[tuple[float, ...], ...],
    ) -> tuple[tuple[float, ...], ...]:
        """Apply the configured deterministic normalization to model output."""
        if self.normalization == "none":
            if any(not math.isfinite(value) for vector in vectors for value in vector):
                raise ValueError("Embedding vectors must contain finite values")
            return vectors
        normalized: list[tuple[float, ...]] = []
        for vector in vectors:
            if any(not math.isfinite(value) for value in vector):
                raise ValueError("Embedding vectors must contain finite values")
            norm = math.sqrt(sum(value * value for value in vector))
            if norm == 0:
                raise ValueError("Cannot l2-normalize a zero embedding vector")
            normalized.append(tuple(value / norm for value in vector))
        return tuple(normalized)


def compute_processing_config_hash(config: dict[str, str]) -> str:
    """Hash a complete parser/chunker/embedding processing configuration."""
    canonical = json.dumps(
        config,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
