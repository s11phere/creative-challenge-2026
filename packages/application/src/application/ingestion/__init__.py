"""Ingestion use‑case orchestration.

Modules in this package wire together domain entities, repository ports, and
infrastructure adapters to deliver complete ingestion workflows.
"""

from .deletion import DocumentDeletionService
from .embedding import EmbeddingConfig, EmbeddingPipelineResult, EmbeddingService
from .orchestrator import (
    CancelledError,
    IngestionConfig,
    IngestionOrchestrator,
    IngestionResult,
)
from .rebuild import (
    EmbeddingRebuildService,
    RebuildPlanItem,
    RebuildPreparation,
)
from .source_registration import RegisteredSource, RegistrationResult, SourceRegistrationService

__all__ = [
    "CancelledError",
    "DocumentDeletionService",
    "EmbeddingConfig",
    "EmbeddingPipelineResult",
    "EmbeddingService",
    "EmbeddingRebuildService",
    "IngestionConfig",
    "IngestionOrchestrator",
    "IngestionResult",
    "RegisteredSource",
    "RebuildPlanItem",
    "RebuildPreparation",
    "RegistrationResult",
    "SourceRegistrationService",
]
