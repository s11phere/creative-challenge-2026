"""Evidence binding, citation verification, and minimal excerpt resolution."""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from uuid import UUID, uuid4

from domain.blob_store import BlobStore
from domain.grounded_qa import (
    Citation,
    CitationContentKind,
    CitationResolution,
    CitationStatus,
    CitationTargetPort,
    CitationTargetQuery,
    CitationTargetSnapshot,
    EvidenceCandidate,
    GroundedAnswer,
    QAContractError,
    QAError,
    QAErrorCode,
    validate_answer_citations,
)
from domain.parsing import ParseError, Parser, ParseSuccess, StructNode, StructNodeType
from domain.retrieval import LocatorKind, SearchHit, SearchLocator


def compute_excerpt_sha256(text: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", text).split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BoundEvidence:
    candidate: EvidenceCandidate
    text: str
    final_rank: int


class EvidenceBindingService:
    """Assign server-owned Evidence IDs while preserving SearchHit identity."""

    def __init__(self, *, id_factory: Callable[[], UUID] = uuid4) -> None:
        self._id_factory = id_factory

    def bind(self, *, space_id: UUID, hits: tuple[SearchHit, ...]) -> tuple[BoundEvidence, ...]:
        if any(not hit.text.strip() for hit in hits):
            raise QAContractError("Search hits must contain non-blank Evidence text")
        chunk_ids = tuple(hit.chunk_id for hit in hits)
        ranks = tuple(hit.final_rank for hit in hits)
        if len(chunk_ids) != len(set(chunk_ids)):
            raise QAContractError("Search hits must have unique chunk IDs before Evidence binding")
        if len(ranks) != len(set(ranks)) or any(rank < 1 for rank in ranks):
            raise QAContractError("Search hits must have unique positive final ranks")

        bound = tuple(
            BoundEvidence(
                candidate=EvidenceCandidate(
                    evidence_id=self._id_factory(),
                    space_id=space_id,
                    source_id=hit.source_id,
                    document_id=hit.document_id,
                    version_id=hit.version_id,
                    chunk_id=hit.chunk_id,
                    source_key=hit.source_key,
                    locators=hit.locators,
                    excerpt_sha256=compute_excerpt_sha256(hit.text),
                    matched=not hit.context_only,
                    context_only=hit.context_only,
                ),
                text=hit.text,
                final_rank=hit.final_rank,
            )
            for hit in sorted(hits, key=lambda item: (item.final_rank, str(item.chunk_id)))
        )
        evidence_ids = tuple(item.candidate.evidence_id for item in bound)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise QAContractError("Evidence ID factory returned a duplicate ID")
        return bound


class EvidenceVerifier:
    """Recheck mutable publication boundaries before generation and publication."""

    def __init__(self, targets: CitationTargetPort) -> None:
        self._targets = targets

    async def validate_for_generation(
        self, *, space_id: UUID, evidence: tuple[EvidenceCandidate, ...]
    ) -> None:
        await self._validate_current_targets(space_id=space_id, evidence=evidence)

    async def validate_for_publication(
        self,
        *,
        space_id: UUID,
        answer: GroundedAnswer,
        evidence: tuple[EvidenceCandidate, ...],
    ) -> None:
        validate_answer_citations(answer, evidence, space_id=space_id)
        evidence_by_id = {candidate.evidence_id: candidate for candidate in evidence}
        for claim in answer.claims:
            if not any(evidence_by_id[evidence_id].matched for evidence_id in claim.evidence_ids):
                raise QAContractError(
                    "context_only Evidence cannot be the sole support for a claim"
                )
        await self._validate_current_targets(space_id=space_id, evidence=evidence)

    async def _validate_current_targets(
        self, *, space_id: UUID, evidence: tuple[EvidenceCandidate, ...]
    ) -> None:
        for candidate in evidence:
            if candidate.space_id != space_id:
                raise QAContractError("Evidence does not belong to the requested Space")
            snapshot = await self._targets.get_target(_target_query(candidate))
            if snapshot is None:
                raise QAContractError("Evidence target is unavailable")
            _validate_snapshot_identity(snapshot, _target_query(candidate))
            if _classify_snapshot(snapshot) is not CitationStatus.VALID:
                raise QAContractError("Evidence target is not currently publishable")
            if any(locator not in snapshot.locators for locator in candidate.locators):
                raise QAContractError("Evidence locator does not belong to the current target")


class CitationResolver:
    """Resolve a fixed citation to a minimal excerpt without redirecting versions."""

    def __init__(
        self,
        *,
        targets: CitationTargetPort,
        blob_store: BlobStore,
        parsers: Mapping[CitationContentKind, Parser] | None = None,
    ) -> None:
        self._targets = targets
        self._blob_store = blob_store
        self._parsers = dict(parsers or {})

    async def resolve(self, citation: Citation) -> CitationResolution:
        query = CitationTargetQuery(
            space_id=citation.space_id,
            source_id=citation.source_id,
            document_id=citation.document_id,
            version_id=citation.version_id,
            chunk_id=citation.chunk_id,
        )
        snapshot = await self._targets.get_target(query)
        if snapshot is None:
            return CitationResolution(citation, CitationStatus.UNAVAILABLE)
        try:
            _validate_snapshot_identity(snapshot, query)
        except QAContractError:
            return CitationResolution(citation, CitationStatus.INVALID)

        status = _classify_snapshot(snapshot)
        if status not in {CitationStatus.VALID, CitationStatus.SOURCE_UPDATED}:
            return CitationResolution(citation, status)
        if citation.locator not in snapshot.locators:
            return CitationResolution(citation, CitationStatus.INVALID)

        try:
            raw = await self._blob_store.retrieve(snapshot.storage_key)
        except ValueError:
            return CitationResolution(citation, CitationStatus.INVALID)
        except OSError as exc:
            raise QAError(
                QAErrorCode.STORAGE_FAILED,
                "Citation blob storage is unavailable.",
                retryable=True,
            ) from exc
        if raw is None:
            return CitationResolution(citation, CitationStatus.UNAVAILABLE)
        if hashlib.sha256(raw).hexdigest() != snapshot.blob_hash:
            return CitationResolution(citation, CitationStatus.INVALID)

        excerpts = await self._extract(snapshot, citation.locator, raw)
        excerpt = next(
            (item for item in excerpts if compute_excerpt_sha256(item) == citation.excerpt_sha256),
            None,
        )
        if excerpt is None:
            return CitationResolution(citation, CitationStatus.INVALID)
        return CitationResolution(citation, status, excerpt)

    async def _extract(
        self, snapshot: CitationTargetSnapshot, locator: SearchLocator, raw: bytes
    ) -> tuple[str, ...]:
        if snapshot.content_kind is CitationContentKind.TEXT and locator.kind is LocatorKind.LINES:
            try:
                text = raw.decode(snapshot.metadata.encoding)
            except (LookupError, UnicodeDecodeError):
                return ()
            lines = text.splitlines()
            if locator.end > len(lines):
                return ()
            raw_excerpt = "\n".join(lines[locator.start - 1 : locator.end])
            parser = self._parsers.get(CitationContentKind.TEXT)
            if parser is None:
                return (raw_excerpt,)
            parsed = await parser.parse(raw, snapshot.metadata)
            if isinstance(parsed, ParseError):
                return (raw_excerpt,)
            assert isinstance(parsed, ParseSuccess)
            node_text = _texts_within_lines(parsed.document.structure, locator)
            structured_excerpt = "\n\n".join(" ".join(item.split()) for item in node_text)
            return tuple(dict.fromkeys((raw_excerpt, structured_excerpt)))

        if (
            snapshot.content_kind is not CitationContentKind.PDF
            or locator.kind is not LocatorKind.PDF_PAGE
            or locator.start != locator.end
        ):
            return ()
        parser = self._parsers.get(CitationContentKind.PDF)
        if parser is None:
            return ()
        parsed = await parser.parse(raw, snapshot.metadata)
        if isinstance(parsed, ParseError):
            return ()
        assert isinstance(parsed, ParseSuccess)
        page_text = tuple(
            node.text
            for node in parsed.document.structure
            if node.start_page == locator.start and node.end_page == locator.end and node.text
        )
        excerpt = "\n".join(page_text)
        return (excerpt,) if excerpt else ()


_CITABLE_NODE_TYPES = frozenset(
    {
        StructNodeType.PARAGRAPH,
        StructNodeType.CODE_BLOCK,
        StructNodeType.LIST_ITEM,
        StructNodeType.QUOTE_BLOCK,
        StructNodeType.TABLE,
        StructNodeType.RAW_TEXT,
        StructNodeType.THEMATIC_BREAK,
    }
)


def _texts_within_lines(nodes: tuple[StructNode, ...], locator: SearchLocator) -> tuple[str, ...]:
    texts: list[str] = []
    for node in nodes:
        if (
            node.node_type in _CITABLE_NODE_TYPES
            and node.text.strip()
            and node.start_line >= locator.start
            and node.end_line <= locator.end
        ):
            texts.append(node.text)
        if node.children:
            texts.extend(_texts_within_lines(node.children, locator))
    return tuple(texts)


def _target_query(candidate: EvidenceCandidate) -> CitationTargetQuery:
    return CitationTargetQuery(
        space_id=candidate.space_id,
        source_id=candidate.source_id,
        document_id=candidate.document_id,
        version_id=candidate.version_id,
        chunk_id=candidate.chunk_id,
    )


def _validate_snapshot_identity(
    snapshot: CitationTargetSnapshot, query: CitationTargetQuery
) -> None:
    if snapshot.query != query:
        raise QAContractError("Citation target identity does not match the requested target")


def _classify_snapshot(snapshot: CitationTargetSnapshot) -> CitationStatus:
    if snapshot.retention_expired:
        return CitationStatus.RETENTION_EXPIRED
    if snapshot.document_deleted:
        return CitationStatus.DELETED
    if snapshot.source_withdrawn:
        return CitationStatus.WITHDRAWN
    if not snapshot.chunk_available:
        return CitationStatus.UNAVAILABLE
    if snapshot.current_version_id != snapshot.query.version_id:
        return CitationStatus.SOURCE_UPDATED
    return CitationStatus.VALID


__all__ = [
    "BoundEvidence",
    "CitationResolver",
    "EvidenceBindingService",
    "EvidenceVerifier",
    "compute_excerpt_sha256",
]
