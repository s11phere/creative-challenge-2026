"""Controlled embedding rebuild preparation tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4

from application.ingestion.rebuild import EmbeddingRebuildService
from domain.embedding import EmbeddingIdentity, compute_processing_config_hash
from domain.models import (
    Document,
    DocumentStatus,
    DocumentVersion,
    IngestionTask,
    Source,
    TaskOperation,
)


@dataclass
class _SourceRepo:
    source: Source

    async def get(self, source_id: UUID) -> Source | None:
        return self.source if self.source.id == source_id else None


@dataclass
class _DocumentRepo:
    documents: list[Document]

    async def get_by_source(self, source_id: UUID) -> list[Document]:
        return [document for document in self.documents if document.source_id == source_id]


@dataclass
class _VersionRepo:
    versions: dict[UUID, DocumentVersion]

    async def create(self, version: DocumentVersion) -> DocumentVersion:
        self.versions[version.id] = version
        return version

    async def get(self, version_id: UUID) -> DocumentVersion | None:
        return self.versions.get(version_id)

    async def get_by_document(self, document_id: UUID) -> list[DocumentVersion]:
        return [version for version in self.versions.values() if version.document_id == document_id]


@dataclass
class _TaskRepo:
    tasks: list[IngestionTask] = field(default_factory=list)

    async def create(self, task: IngestionTask) -> IngestionTask:
        self.tasks.append(task)
        return task

    async def get_by_source(self, source_id: UUID) -> list[IngestionTask]:
        return [task for task in self.tasks if task.source_id == source_id]


def _service_fixture():
    source = Source()
    current = DocumentVersion(
        document_id=uuid4(),
        content_hash="a" * 64,
        blob_hash="b" * 64,
        embedding_version="legacy-v1",
        processing_config_hash="c" * 64,
        processing_config={"chunk_size": "512"},
        status=DocumentStatus.PUBLISHED,
    )
    document = Document(
        id=current.document_id,
        source_id=source.id,
        stable_key="fixture.md",
        current_version_id=current.id,
    )
    version_repo = _VersionRepo({current.id: current})
    task_repo = _TaskRepo()
    service = EmbeddingRebuildService(
        source_repo=_SourceRepo(source),
        document_repo=_DocumentRepo([document]),
        version_repo=version_repo,
        task_repo=task_repo,
    )
    return service, document, current, version_repo, task_repo


async def test_rebuild_creates_candidate_without_switching_current_version() -> None:
    service, document, current, version_repo, _task_repo = _service_fixture()
    identity = EmbeddingIdentity(
        model_revision="synthetic-model@revision",
        query_instruction_version="query-v1",
        document_instruction_version="document-v1",
        normalization="l2",
        precision="float16",
    )

    prepared = await service.prepare_source(document.source_id, identity)

    assert document.current_version_id == current.id
    assert len(prepared.candidates) == 1
    candidate = prepared.candidates[0]
    assert candidate.id != current.id
    assert candidate.status is DocumentStatus.PENDING
    assert candidate.embedding_version == identity.version
    assert candidate.processing_config["embedding_model_revision"] == ("synthetic-model@revision")
    assert version_repo.versions[current.id].status is DocumentStatus.PUBLISHED
    assert len(prepared.tasks) == 1
    assert prepared.tasks[0].operation is TaskOperation.REBUILD
    assert prepared.tasks[0].target_version_id == candidate.id


async def test_repeated_preparation_reuses_candidate_and_active_task() -> None:
    service, document, _current, version_repo, task_repo = _service_fixture()
    identity = EmbeddingIdentity(model_revision="synthetic-model@revision")

    first = await service.prepare_source(document.source_id, identity)
    second = await service.prepare_source(document.source_id, identity)

    assert second.candidates[0].id == first.candidates[0].id
    assert second.tasks[0].id == first.tasks[0].id
    assert len(version_repo.versions) == 2
    assert len(task_repo.tasks) == 1


async def test_current_matching_identity_is_skipped() -> None:
    service, document, current, version_repo, _task_repo = _service_fixture()
    identity = EmbeddingIdentity(model_revision="synthetic-model@revision")
    config = {**current.processing_config, **identity.processing_config()}
    version_repo.versions[current.id] = DocumentVersion(
        **{
            **current.__dict__,
            "embedding_version": identity.version,
            "processing_config": config,
            "processing_config_hash": compute_processing_config_hash(config),
        }
    )

    prepared = await service.prepare_source(document.source_id, identity)

    assert prepared.candidates == ()
    assert prepared.tasks == ()
    assert prepared.skipped_document_ids == (document.id,)
