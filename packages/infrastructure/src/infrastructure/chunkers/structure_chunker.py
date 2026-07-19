"""Structure-aware chunker implementation.

Splits a parsed document into chunks by walking its structural node tree.
Chunk boundaries are placed at semantic breakpoints (headings, code blocks,
paragraphs) before respecting character-size limits.
"""

from __future__ import annotations

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
            if node.children:
                segments.extend(_extract_segments(node.children, heading_path))

        elif node.node_type == StructNodeType.DOCUMENT:
            # Top-level container — recurse directly
            if node.children:
                segments.extend(_extract_segments(node.children, heading_path))

        elif node.node_type in _CONTENT_NODE_TYPES:
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

    CHUNKER_VERSION = "1.0"

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

        # Group segments into chunks
        raw_chunks: list[list[_Segment]] = self._group_segments(segments, cfg)

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

            primary_type = self._infer_type(group)

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

    @staticmethod
    def _split_oversize_segment(seg: _Segment, max_size: int) -> list[_Segment]:
        """Split a single large segment into smaller ones.

        First attempts line-level splitting; if a single line still exceeds
        *max_size*, splits by character count.
        """
        lines = seg.text.splitlines(keepends=False)
        if not lines:
            return [seg]

        result: list[_Segment] = []
        current_lines: list[str] = []
        current_len = 0
        line_offset = seg.start_line

        for line in lines:
            line_len = len(line)
            # If a single line exceeds max_size, split it by characters
            if line_len > max_size and not current_lines:
                for i in range(0, line_len, max_size):
                    chunk_text = line[i : i + max_size]
                    result.append(
                        _Segment(
                            text=chunk_text,
                            start_line=line_offset,
                            end_line=line_offset,
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
                        start_line=line_offset,
                        end_line=line_offset + len(current_lines) - 1,
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
                    start_line=line_offset,
                    end_line=line_offset + len(current_lines) - 1,
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
