from __future__ import annotations

import hashlib
from dataclasses import replace
from uuid import UUID

import pytest
from application.qa.evidence import (
    CitationResolver,
    EvidenceBindingService,
    EvidenceVerifier,
    compute_excerpt_sha256,
)
from domain.grounded_qa import (
    Citation,
    CitationContentKind,
    CitationStatus,
    CitationTargetQuery,
    CitationTargetSnapshot,
    Claim,
    GroundedAnswer,
    QAContractError,
)
from domain.parsing import (
    ParsedDocument,
    ParseMetadata,
    ParseSuccess,
    StructNode,
    StructNodeType,
)
from domain.retrieval import LocatorKind, SearchHit, SearchHitSummary, SearchLocator

SPACE_ID = UUID(int=1)
SOURCE_ID = UUID(int=2)
DOCUMENT_ID = UUID(int=3)
VERSION_ID = UUID(int=4)
CHUNK_ID = UUID(int=5)
EVIDENCE_ID = UUID(int=6)
OTHER_VERSION_ID = UUID(int=7)


class FakeTargets:
    def __init__(self, snapshot: CitationTargetSnapshot | None) -> None:
        self.snapshot = snapshot
        self.calls = 0

    async def get_target(self, _query: CitationTargetQuery) -> CitationTargetSnapshot | None:
        self.calls += 1
        return self.snapshot


class FakeBlobStore:
    def __init__(self, data: bytes | None, *, reject_key: bool = False) -> None:
        self.data = data
        self.reject_key = reject_key
        self.retrieve_calls = 0

    async def retrieve(self, _key: str) -> bytes | None:
        self.retrieve_calls += 1
        if self.reject_key:
            raise ValueError("Path traversal detected")
        return self.data

    async def store(self, _key: str, data: bytes) -> None:
        self.data = data

    async def delete(self, _key: str) -> None:
        self.data = None

    async def exists(self, _key: str) -> bool:
        return self.data is not None

    async def store_and_verify(self, _key: str, data: bytes, expected_hash: str) -> None:
        if hashlib.sha256(data).hexdigest() != expected_hash:
            raise ValueError("hash mismatch")
        self.data = data


class FakePdfParser:
    async def parse(self, _raw: bytes, metadata: ParseMetadata) -> ParseSuccess:
        return ParseSuccess(
            ParsedDocument(
                metadata=metadata,
                text="page one\npage two",
                structure=(
                    StructNode(
                        node_type=StructNodeType.RAW_TEXT,
                        text="page one",
                        start_page=1,
                        end_page=1,
                    ),
                    StructNode(
                        node_type=StructNodeType.RAW_TEXT,
                        text="page two",
                        start_page=2,
                        end_page=2,
                    ),
                ),
                total_lines=2,
            )
        )


def _hit(*, context_only: bool = False, rank: int = 1, text: str = "line two") -> SearchHit:
    return SearchHit(
        chunk_id=CHUNK_ID,
        version_id=VERSION_ID,
        document_id=DOCUMENT_ID,
        source_id=SOURCE_ID,
        source_key="fixture/source",
        text=text,
        chunk_hash="b" * 64,
        safe_summary=SearchHitSummary(
            chunk_hash="b" * 64,
            text_length=len(text),
            locator_count=1,
        ),
        locators=(SearchLocator(LocatorKind.LINES, 2, 2),),
        final_rank=rank,
        context_only=context_only,
    )


def _snapshot(
    raw: bytes,
    *,
    locator: SearchLocator | None = None,
    content_kind: CitationContentKind = CitationContentKind.TEXT,
) -> CitationTargetSnapshot:
    return CitationTargetSnapshot(
        query=CitationTargetQuery(
            space_id=SPACE_ID,
            source_id=SOURCE_ID,
            document_id=DOCUMENT_ID,
            version_id=VERSION_ID,
            chunk_id=CHUNK_ID,
        ),
        current_version_id=VERSION_ID,
        locators=(locator or SearchLocator(LocatorKind.LINES, 2, 2),),
        blob_hash=hashlib.sha256(raw).hexdigest(),
        storage_key=f"{SOURCE_ID}/aa/blob",
        content_kind=content_kind,
        metadata=ParseMetadata(
            file_name="fixture.txt",
            file_size=len(raw),
            mime_type="text/plain",
            encoding="utf-8",
        ),
    )


def _citation(
    *,
    locator: SearchLocator | None = None,
    excerpt: str = "line two",
) -> Citation:
    return Citation(
        evidence_id=EVIDENCE_ID,
        space_id=SPACE_ID,
        source_id=SOURCE_ID,
        document_id=DOCUMENT_ID,
        version_id=VERSION_ID,
        chunk_id=CHUNK_ID,
        locator=locator or SearchLocator(LocatorKind.LINES, 2, 2),
        excerpt_sha256=compute_excerpt_sha256(excerpt),
    )


def test_binding_assigns_server_ids_and_preserves_matched_context_semantics() -> None:
    ids = iter((EVIDENCE_ID, UUID(int=8)))
    second_hit = replace(_hit(context_only=True, rank=2), chunk_id=UUID(int=9))
    bound = EvidenceBindingService(id_factory=lambda: next(ids)).bind(
        space_id=SPACE_ID,
        hits=(_hit(), second_hit),
    )

    assert tuple(item.candidate.evidence_id for item in bound) == (EVIDENCE_ID, UUID(int=8))
    assert bound[0].candidate.matched is True
    assert bound[1].candidate.context_only is True


def test_binding_rejects_duplicate_server_evidence_ids() -> None:
    second_hit = replace(_hit(context_only=True, rank=2), chunk_id=UUID(int=9))
    with pytest.raises(QAContractError, match="duplicate ID"):
        EvidenceBindingService(id_factory=lambda: EVIDENCE_ID).bind(
            space_id=SPACE_ID,
            hits=(_hit(), second_hit),
        )


def test_binding_rejects_blank_evidence_text() -> None:
    with pytest.raises(QAContractError, match="non-blank"):
        EvidenceBindingService(id_factory=lambda: EVIDENCE_ID).bind(
            space_id=SPACE_ID,
            hits=(_hit(text="  "),),
        )


@pytest.mark.asyncio
async def test_generation_and_publication_recheck_current_target() -> None:
    raw = b"line one\nline two\nline three\n"
    targets = FakeTargets(_snapshot(raw))
    bound = EvidenceBindingService(id_factory=lambda: EVIDENCE_ID).bind(
        space_id=SPACE_ID, hits=(_hit(),)
    )
    verifier = EvidenceVerifier(targets)
    evidence = tuple(item.candidate for item in bound)
    await verifier.validate_for_generation(space_id=SPACE_ID, evidence=evidence)

    targets.snapshot = replace(targets.snapshot, current_version_id=OTHER_VERSION_ID)
    answer = GroundedAnswer(
        text="Supported",
        claims=(Claim("c1", "Supported", (EVIDENCE_ID,)),),
        citations=(_citation(),),
    )
    with pytest.raises(QAContractError, match="not currently publishable"):
        await verifier.validate_for_publication(
            space_id=SPACE_ID,
            answer=answer,
            evidence=evidence,
        )
    assert targets.calls == 2


@pytest.mark.asyncio
async def test_context_only_evidence_cannot_be_the_only_claim_support() -> None:
    raw = b"line one\nline two\n"
    bound = EvidenceBindingService(id_factory=lambda: EVIDENCE_ID).bind(
        space_id=SPACE_ID, hits=(_hit(context_only=True),)
    )
    evidence = tuple(item.candidate for item in bound)
    answer = GroundedAnswer(
        text="Unsupported by a matched hit",
        claims=(Claim("c1", "Unsupported by a matched hit", (EVIDENCE_ID,)),),
        citations=(_citation(),),
    )

    with pytest.raises(QAContractError, match="context_only"):
        await EvidenceVerifier(FakeTargets(_snapshot(raw))).validate_for_publication(
            space_id=SPACE_ID,
            answer=answer,
            evidence=evidence,
        )


@pytest.mark.asyncio
async def test_text_citation_resolves_exact_one_based_inclusive_lines() -> None:
    raw = b"line one\nline two\nline three\n"
    resolver = CitationResolver(targets=FakeTargets(_snapshot(raw)), blob_store=FakeBlobStore(raw))

    resolution = await resolver.resolve(_citation())

    assert resolution.status is CitationStatus.VALID
    assert resolution.excerpt == "line two"


@pytest.mark.asyncio
async def test_pdf_citation_resolves_only_the_declared_one_based_page() -> None:
    raw = b"synthetic-pdf-bytes"
    locator = SearchLocator(LocatorKind.PDF_PAGE, 2, 2)
    snapshot = _snapshot(raw, locator=locator, content_kind=CitationContentKind.PDF)
    resolver = CitationResolver(
        targets=FakeTargets(snapshot),
        blob_store=FakeBlobStore(raw),
        parsers={CitationContentKind.PDF: FakePdfParser()},
    )

    resolution = await resolver.resolve(_citation(locator=locator, excerpt="page two"))

    assert resolution.status is CitationStatus.VALID
    assert resolution.excerpt == "page two"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"source_withdrawn": True}, CitationStatus.WITHDRAWN),
        ({"document_deleted": True}, CitationStatus.DELETED),
        ({"retention_expired": True}, CitationStatus.RETENTION_EXPIRED),
        ({"chunk_available": False}, CitationStatus.UNAVAILABLE),
    ],
)
async def test_unavailable_history_returns_status_without_blob_or_excerpt(
    changes: dict[str, bool], expected: CitationStatus
) -> None:
    raw = b"line one\nline two\n"
    blob_store = FakeBlobStore(raw)
    snapshot = replace(_snapshot(raw), **changes)

    resolution = await CitationResolver(
        targets=FakeTargets(snapshot), blob_store=blob_store
    ).resolve(_citation())

    assert resolution.status is expected
    assert resolution.excerpt is None
    assert blob_store.retrieve_calls == 0


@pytest.mark.asyncio
async def test_updated_source_resolves_fixed_old_version_without_redirecting() -> None:
    raw = b"line one\nline two\n"
    snapshot = replace(_snapshot(raw), current_version_id=OTHER_VERSION_ID)

    resolution = await CitationResolver(
        targets=FakeTargets(snapshot), blob_store=FakeBlobStore(raw)
    ).resolve(_citation())

    assert resolution.status is CitationStatus.SOURCE_UPDATED
    assert resolution.citation.version_id is VERSION_ID
    assert resolution.excerpt == "line two"


@pytest.mark.asyncio
async def test_invalid_locator_blob_hash_and_storage_key_never_leak_excerpt() -> None:
    raw = b"line one\nline two\n"
    out_of_bounds = _citation(locator=SearchLocator(LocatorKind.LINES, 8, 8))
    resolver = CitationResolver(targets=FakeTargets(_snapshot(raw)), blob_store=FakeBlobStore(raw))
    assert (await resolver.resolve(out_of_bounds)).status is CitationStatus.INVALID

    corrupt = FakeBlobStore(b"different bytes")
    assert (
        await CitationResolver(targets=FakeTargets(_snapshot(raw)), blob_store=corrupt).resolve(
            _citation()
        )
    ).status is CitationStatus.INVALID

    traversal = FakeBlobStore(raw, reject_key=True)
    resolution = await CitationResolver(
        targets=FakeTargets(_snapshot(raw)), blob_store=traversal
    ).resolve(_citation())
    assert resolution.status is CitationStatus.INVALID
    assert resolution.excerpt is None
