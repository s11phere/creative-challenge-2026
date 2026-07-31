"""Structure-aware chunker implementation.

Splits a parsed document into chunks by walking its structural node tree.
Chunk boundaries are placed at semantic breakpoints (headings, code blocks,
paragraphs) before respecting character-size limits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from domain.chunking import (
    ChunkerConfig,
    ChunkingResult,
    ChunkOutput,
    compute_chunk_hash,
    compute_chunker_config_hash,
)
from domain.parsing import ParsedDocument, StructNode, StructNodeType

# ---------------------------------------------------------------------------
# Internal segment model
# ---------------------------------------------------------------------------


@dataclass
class _Segment:
    """A single contiguous text block with structure context."""

    text: str
    start_line: int
    end_line: int
    heading_path: str
    primary_type: str
    start_page: int | None = None
    end_page: int | None = None


# Types that represent leaf content (carry their own text).
_CONTENT_NODE_TYPES = frozenset(
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

_TOC_NUMBER_PREFIX = re.compile(
    r"^\s*(?:"
    r"\d+(?:\.\d+)*(?:[.)、．）]\s*|\s+)"
    r"|[（(]?[一二三四五六七八九十百]+[.、．）)]\s*"
    r")"
)
_TOC_TRAILING_PAGE = re.compile(r"(?:\s*\.{2,}\s*|\s+)\d+\s*$")
_TOC_SCAN_MAX_LINE = 120


def _extract_segments(
    nodes: tuple[StructNode, ...],
    heading_path: list[tuple[str, int]] | None = None,
) -> list[_Segment]:
    """Walk the structure tree and yield text segments with context."""
    if heading_path is None:
        heading_path = []

    segments: list[_Segment] = []

    for node in nodes:
        if node.node_type == StructNodeType.HEADING:
            # Update heading path: trim to the current heading level
            while heading_path and heading_path[-1][1] >= node.level:
                heading_path.pop()
            heading_path.append((node.text, node.level))
            # Recurse into children (content under this heading)
            # Emit the heading text itself as a segment so it appears in
            # the chunk text (not just the heading_path metadata). This
            # strengthens the topic signal in the embedding vector and
            # makes chunk text self-contained for RAG context.
            path_str = " > ".join(h[0] for h in heading_path)
            segments.append(
                _Segment(
                    text=node.text,
                    start_line=node.start_line,
                    end_line=node.end_line,
                    heading_path=path_str,
                    primary_type="heading",
                    start_page=node.start_page,
                    end_page=node.end_page,
                )
            )
            if node.children:
                segments.extend(_extract_segments(node.children, heading_path))

        elif node.node_type == StructNodeType.DOCUMENT:
            # Top-level container — recurse directly
            if node.children:
                segments.extend(_extract_segments(node.children, heading_path))

        elif node.node_type in _CONTENT_NODE_TYPES:
            if not node.text.strip():
                continue
            path_str = " > ".join(h[0] for h in heading_path)
            segments.append(
                _Segment(
                    text=node.text,
                    start_line=node.start_line,
                    end_line=node.end_line,
                    heading_path=path_str,
                    primary_type=node.node_type.value,
                    start_page=node.start_page,
                    end_page=node.end_page,
                )
            )
            # Some content nodes (e.g. list items) can nest children
            if node.children:
                segments.extend(_extract_segments(node.children, heading_path))

    return segments


def _fallback_segments(text: str) -> list[_Segment]:
    """Fallback: split plain text into paragraph segments.

    Used when the document has no structural node tree.
    """
    if not text.strip():
        return []

    segments: list[_Segment] = []
    lines = text.splitlines(keepends=False)
    total = len(lines)
    line = 1

    while line <= total:
        # Collect a paragraph (run of non-blank lines)
        para_lines: list[str] = []
        start = line
        while line <= total:
            raw = lines[line - 1]
            if raw.strip() or not para_lines:
                para_lines.append(raw)
                line += 1
            else:
                break
        # Skip trailing blank lines that ended the paragraph
        while para_lines and not para_lines[-1].strip():
            para_lines.pop()
            line -= 1
        if para_lines:
            segments.append(
                _Segment(
                    text="\n".join(para_lines),
                    start_line=start,
                    end_line=start + len(para_lines) - 1,
                    heading_path="",
                    primary_type="paragraph",
                )
            )
        # Consume inter-paragraph blank lines
        while line <= total and not lines[line - 1].strip():
            line += 1

    return segments


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


class StructureChunker:
    """Structure-aware document chunker.

    Walks the ``ParsedDocument.structure`` tree to extract text segments
    with heading context, then groups them into chunks respecting heading
    boundaries and size limits.

    The chunker is stateless and idempotent: the same document + config
    always produces identical ``ChunkOutput`` sequences (same ordinals,
    same ``chunk_hash`` values).
    """

    CHUNKER_VERSION = "1.3"

    async def chunk(
        self,
        document: ParsedDocument,
        *,
        config: ChunkerConfig | None = None,
    ) -> ChunkingResult:
        cfg = config or ChunkerConfig()
        config_hash = compute_chunker_config_hash(cfg)

        # Extract segments
        segments: list[_Segment]
        if document.structure:
            segments = _extract_segments(document.structure)
        else:
            segments = _fallback_segments(document.text)

        if not segments:
            return ChunkingResult(
                chunks=(),
                chunker_version=self.CHUNKER_VERSION,
                config_hash=config_hash,
                total_ordinals=0,
            )

        # Detect a TOC before minimum-size merging can combine it with the first
        # real heading. TOC and content are grouped independently so neither
        # source text nor the content heading is lost at the boundary.
        toc_range = self._table_of_contents_segment_range(segments)
        if toc_range is not None:
            toc_start, toc_end = toc_range
            preamble_chunks = self._group_segments(segments[:toc_start], cfg)
            toc_chunks = self._group_segments(segments[toc_start:toc_end], cfg)
            content_chunks = self._group_segments(segments[toc_end:], cfg)
            content_chunks = self._merge_leading_small_group_forward(content_chunks, cfg)
            raw_chunks = [*preamble_chunks, *toc_chunks, *content_chunks]
            toc_group_indexes = frozenset(
                range(len(preamble_chunks), len(preamble_chunks) + len(toc_chunks))
            )
        else:
            raw_chunks = self._group_segments(segments, cfg)
            toc_group_indexes = frozenset()

        # Build ChunkOutput list
        outputs: list[ChunkOutput] = []
        current_line = 0
        for i, group in enumerate(raw_chunks):
            text = _join_segments(group)
            heading_path = group[0].heading_path if group else ""
            # If multiple heading paths within a chunk, use the first meaningful one
            if not heading_path:
                for seg in group:
                    if seg.heading_path:
                        heading_path = seg.heading_path
                        break

            start_line = (
                min(s.start_line for s in group if s.start_line > 0)
                if any(s.start_line > 0 for s in group)
                else current_line + 1
            )
            end_line = (
                max(s.end_line for s in group)
                if all(s.end_line > 0 for s in group)
                else current_line + len(text.splitlines())
            )
            start_page = min(
                (s.start_page for s in group if s.start_page is not None), default=None
            )
            end_page = max((s.end_page for s in group if s.end_page is not None), default=None)

            primary_type = (
                "table_of_contents" if i in toc_group_indexes else self._infer_type(group)
            )

            outputs.append(
                ChunkOutput(
                    ordinal=i,
                    text=text,
                    chunk_hash=compute_chunk_hash(text),
                    heading_path=heading_path,
                    start_line=start_line,
                    end_line=end_line,
                    start_page=start_page,
                    end_page=end_page,
                    node_type=primary_type,
                )
            )
            current_line = end_line

        # Link adjacency
        for i, chunk in enumerate(outputs):
            outputs[i] = ChunkOutput(
                ordinal=chunk.ordinal,
                text=chunk.text,
                chunk_hash=chunk.chunk_hash,
                heading_path=chunk.heading_path,
                start_line=chunk.start_line,
                end_line=chunk.end_line,
                start_page=chunk.start_page,
                end_page=chunk.end_page,
                parent_ordinal=chunk.parent_ordinal,
                prev_ordinal=outputs[i - 1].ordinal if i > 0 else None,
                next_ordinal=outputs[i + 1].ordinal if i < len(outputs) - 1 else None,
                node_type=chunk.node_type,
            )

        return ChunkingResult(
            chunks=tuple(outputs),
            chunker_version=self.CHUNKER_VERSION,
            config_hash=config_hash,
            total_ordinals=len(outputs),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _group_segments(
        self,
        segments: list[_Segment],
        config: ChunkerConfig,
    ) -> list[list[_Segment]]:
        """Group consecutive *segments* into chunks.

        New chunks are started at heading boundaries or when the
        accumulated text would exceed ``chunk_size``.
        """
        if not segments:
            return []

        # Pre-process: split any segment that alone exceeds chunk_size
        expanded: list[_Segment] = []
        for seg in segments:
            if len(seg.text) > config.chunk_size:
                expanded.extend(self._split_oversize_segment(seg, config.chunk_size))
            else:
                expanded.append(seg)

        groups: list[list[_Segment]] = []
        current: list[_Segment] = []
        current_len = 0
        prev_chunk_text = ""

        for seg in expanded:
            seg_len = len(seg.text)

            # Start a new chunk at a heading boundary
            if current and self._is_heading_boundary(current[-1], seg):
                groups.append(current)
                prev_chunk_text = _join_segments(current)
                current = []
                current_len = 0

            # If the segment fits in the current chunk, add it
            if current_len + seg_len <= config.chunk_size or not current:
                current.append(seg)
                current_len += seg_len
            else:
                # Current chunk is full — finalize
                groups.append(current)
                prev_chunk_text = _join_segments(current)

                # Start new chunk: prepend overlap text to this segment
                overlap = self._extract_overlap_text(prev_chunk_text, config.chunk_overlap)
                if overlap:
                    seg = _Segment(
                        text=overlap + "\n\n" + seg.text,
                        start_line=seg.start_line,
                        end_line=seg.end_line,
                        heading_path=seg.heading_path,
                        primary_type=seg.primary_type,
                        start_page=seg.start_page,
                        end_page=seg.end_page,
                    )
                current = [seg]
                current_len = len(seg.text)

        if current:
            current_len = sum(len(s.text) for s in current)
            if groups and current_len < config.min_chunk_size:
                groups[-1].extend(current)
            else:
                groups.append(current)

        # Merge chunks that remain too small
        merged: list[list[_Segment]] = []
        for group in groups:
            group_len = sum(len(s.text) for s in group)
            if merged and group_len < config.min_chunk_size:
                merged[-1].extend(group)
            else:
                merged.append(group)

        return merged

    @classmethod
    def _table_of_contents_segment_range(
        cls,
        segments: list[_Segment],
    ) -> tuple[int, int] | None:
        """Find an early navigation list that substantially mirrors later headings."""
        headings = {
            cls._normalize_toc_label(segment.text)
            for segment in segments
            if segment.primary_type == "heading"
        }
        headings.discard("")
        if len(headings) < 3:
            return None

        run_start: int | None = None
        run_end_line = 0
        for index, segment in enumerate(segments):
            if segment.start_line > _TOC_SCAN_MAX_LINE:
                break
            is_list_candidate = segment.primary_type in {"list_item", "raw_text"}
            has_line_gap = run_start is not None and segment.start_line > run_end_line + 2
            if not is_list_candidate or has_line_gap:
                if run_start is not None and cls._is_table_of_contents_run(
                    segments[run_start:index], headings
                ):
                    return run_start, index
                run_start = index if is_list_candidate else None
                run_end_line = segment.end_line if is_list_candidate else 0
                continue
            if run_start is None:
                run_start = index
            run_end_line = max(run_end_line, segment.end_line)

        if run_start is not None and cls._is_table_of_contents_run(
            segments[run_start : index + 1], headings
        ):
            return run_start, index + 1
        return None

    @classmethod
    def _is_table_of_contents_run(cls, run: list[_Segment], headings: set[str]) -> bool:
        labels = tuple(
            dict.fromkeys(
                label
                for segment in run
                for line in segment.text.splitlines()
                if (label := cls._normalize_toc_label(line))
            )
        )
        if len(labels) < 3:
            return False
        matches = sum(
            any(cls._toc_labels_match(label, heading) for heading in headings) for label in labels
        )
        return matches >= 3 and matches / len(labels) >= 0.6

    @staticmethod
    def _toc_labels_match(label: str, heading: str) -> bool:
        if label == heading:
            return True
        separators = " :：-—（("
        return (
            len(label) > len(heading)
            and label.startswith(heading)
            and label[len(heading)] in separators
        ) or (
            len(heading) > len(label)
            and heading.startswith(label)
            and heading[len(label)] in separators
        )

    @staticmethod
    def _merge_leading_small_group_forward(
        groups: list[list[_Segment]], config: ChunkerConfig
    ) -> list[list[_Segment]]:
        """Keep a small first content heading without merging it into the TOC."""
        if len(groups) < 2:
            return groups
        first_length = sum(len(segment.text) for segment in groups[0])
        second_length = sum(len(segment.text) for segment in groups[1])
        if first_length >= config.min_chunk_size:
            return groups
        if first_length + second_length > config.chunk_size:
            return groups
        return [[*groups[0], *groups[1]], *groups[2:]]

    @staticmethod
    def _normalize_toc_label(text: str) -> str:
        normalized = " ".join(text.casefold().split())
        normalized = _TOC_NUMBER_PREFIX.sub("", normalized)
        normalized = _TOC_TRAILING_PAGE.sub("", normalized)
        return normalized.strip(" .:-")

    @staticmethod
    def _split_oversize_line(line: str, max_size: int) -> list[str]:
        """Split a single over-size line at word boundaries, never mid-word.

        Each piece is cut at the last space inside the window so tokens stay
        intact.  Falls back to a hard character split only when a single token
        spans more than *max_size* characters (pathological).
        """
        pieces: list[str] = []
        remaining = line
        while len(remaining) > max_size:
            cut = remaining.rfind(" ", 0, max_size)
            if cut <= 0:
                # No space inside the window: hard character split
                cut = max_size
            pieces.append(remaining[:cut])
            remaining = remaining[cut:].lstrip(" ")
        if remaining:
            pieces.append(remaining)
        return pieces

    @staticmethod
    def _split_oversize_segment(seg: _Segment, max_size: int) -> list[_Segment]:
        """Split a single large segment into smaller ones.

        First attempts line-level splitting; if a single line still exceeds
        *max_size*, splits it at word boundaries instead of mid-word.
        """
        lines = seg.text.splitlines(keepends=False)
        if not lines:
            return [seg]

        source_line_count = seg.end_line - seg.start_line + 1
        preserve_source_span = (
            seg.start_line > 0
            and seg.end_line >= seg.start_line
            and len(lines) != source_line_count
        )

        result: list[_Segment] = []
        current_lines: list[str] = []
        current_len = 0
        line_offset = seg.start_line

        for line in lines:
            line_len = len(line)
            # If a single line exceeds max_size, flush any accumulated lines
            # first and split the oversized line on word boundaries.
            if line_len > max_size:
                if current_lines:
                    result.append(
                        _Segment(
                            text="\n".join(current_lines),
                            start_line=seg.start_line if preserve_source_span else line_offset,
                            end_line=(
                                seg.end_line
                                if preserve_source_span
                                else line_offset + len(current_lines) - 1
                            ),
                            heading_path=seg.heading_path,
                            primary_type=seg.primary_type,
                            start_page=seg.start_page,
                            end_page=seg.end_page,
                        )
                    )
                    line_offset += len(current_lines)
                    current_lines = []
                    current_len = 0
                for chunk_text in StructureChunker._split_oversize_line(line, max_size):
                    result.append(
                        _Segment(
                            text=chunk_text,
                            start_line=seg.start_line if preserve_source_span else line_offset,
                            end_line=seg.end_line if preserve_source_span else line_offset,
                            heading_path=seg.heading_path,
                            primary_type=seg.primary_type,
                            start_page=seg.start_page,
                            end_page=seg.end_page,
                        )
                    )
                line_offset += 1
                continue

            # +1 for the newline that separates lines in the joined text
            adjusted_len = line_len + 1
            if current_len + adjusted_len > max_size and current_lines:
                result.append(
                    _Segment(
                        text="\n".join(current_lines),
                        start_line=seg.start_line if preserve_source_span else line_offset,
                        end_line=(
                            seg.end_line
                            if preserve_source_span
                            else line_offset + len(current_lines) - 1
                        ),
                        heading_path=seg.heading_path,
                        primary_type=seg.primary_type,
                        start_page=seg.start_page,
                        end_page=seg.end_page,
                    )
                )
                line_offset += len(current_lines)
                current_lines = []
                current_len = 0
            current_lines.append(line)
            current_len += adjusted_len

        if current_lines:
            result.append(
                _Segment(
                    text="\n".join(current_lines),
                    start_line=seg.start_line if preserve_source_span else line_offset,
                    end_line=(
                        seg.end_line
                        if preserve_source_span
                        else line_offset + len(current_lines) - 1
                    ),
                    heading_path=seg.heading_path,
                    primary_type=seg.primary_type,
                    start_page=seg.start_page,
                    end_page=seg.end_page,
                )
            )

        return result

    @staticmethod
    def _is_heading_boundary(prev: _Segment, curr: _Segment) -> bool:
        """Return ``True`` if a new chunk should start at *curr*."""
        if curr.heading_path and curr.heading_path != prev.heading_path:
            return True
        return curr.primary_type == "thematic_break"

    @staticmethod
    def _extract_overlap_text(chunk_text: str, overlap_chars: int) -> str:
        """Return the trailing *overlap_chars* characters of *chunk_text*."""
        if not chunk_text or overlap_chars <= 0 or len(chunk_text) <= overlap_chars:
            return ""
        return chunk_text[-overlap_chars:]

    @staticmethod
    def _infer_type(group: list[_Segment]) -> str:
        """Infer the primary content type of a chunk group."""
        types = [s.primary_type for s in group if s.primary_type != "continuation"]
        if not types:
            return "continuation"
        # Prefer the first non-paragraph type (headings already absorbed into path)
        for t in types:
            if t != "paragraph":
                return t
        return "paragraph"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _join_segments(segments: list[_Segment]) -> str:
    """Join segment texts with double-newline separation."""
    parts = [s.text for s in segments if s.text.strip()]
    if not parts:
        return ""
    # Use single newline between segments that are continuations of each other
    result = "\n\n".join(parts)
    return result.strip()
