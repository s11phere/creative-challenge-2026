"""Ingestion use‑case orchestration.

Modules in this package wire together domain entities, repository ports, and
infrastructure adapters to deliver complete ingestion workflows.
"""

from .embedding import EmbeddingConfig, EmbeddingPipelineResult, EmbeddingService
from .orchestrator import (
    CancelledError,
    IngestionConfig,
    IngestionOrchestrator,
    IngestionResult,
)
from .source_registration import RegisteredSource, RegistrationResult, SourceRegistrationService

__all__ = [
    "CancelledError",
    "EmbeddingConfig",
    "EmbeddingPipelineResult",
    "EmbeddingService",
    "IngestionConfig",
    "IngestionOrchestrator",
    "IngestionResult",
    "RegisteredSource",
    "RegistrationResult",
    "SourceRegistrationService",
]
