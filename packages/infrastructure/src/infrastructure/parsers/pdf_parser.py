"""Copyable-text PDF parser using PyMuPDF.

Extracts text with 1-based page numbers.  Scanned / image-only PDFs return a
``ParseError`` with code ``scanned_pdf`` rather than silently producing empty
text.

Structure is reconstructed from geometry rather than raw ``get_text("text")``
output: spans are assembled into visual lines (fixing glyph reading order for
inlined math), then lines are grouped into heading / paragraph / list / table
nodes based on font size, line spacing, and indentation.  This gives the PDF
the same paragraph structure the Markdown parser provides, so the downstream
``StructureChunker`` cuts at semantic boundaries instead of flooding a page
into hundreds of character-sliced fragments.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import fitz
from domain.parsing import (
    ParsedDocument,
    ParseError,
    ParseErrorCode,
    ParseMetadata,
    ParseResult,
    ParseSuccess,
    StructNode,
    StructNodeType,
)

# Tolerances (in points) and thresholds for geometry heuristics.
_Y_EPS = 2.5  # baselines within this are the same visual line
_BODY_FALLBACK = 11.0
_HEADING_RATIO = 1.15  # a line this much larger than body is a heading
_GAP_PARAGRAPH = 8.0  # vertical gap above this starts a new paragraph
_INDENT_LIST_MATCH = 6.0  # list continuation lines share indentation

_BULLET_PREFIX = ("•", "-", "–", "—", "·", "▪", "◦")
_LIST_DIGIT_RE = __import__("re").compile(r"^\s*\d+(?:\.\d+)?[.)]\s+")


@dataclass(frozen=True)
class _VisualLine:
    """A reconstructed single visual line of text on a page."""

    text: str
    y0: float
    y1: float
    x0: float
    x1: float
    size: float
    start_page: int


# A "span" as returned by ``page.get_text("dict")`` is a dict; keep this
# alias so the helpers don't depend on ``object`` subscripting.
_Span = dict[str, Any]


def _iter_spans(page: fitz.Page) -> list[_Span]:
    """Collect every text span on *page* via ``get_text("dict")``."""
    spans: list[_Span] = []
    try:
        data = page.get_text("dict")
    except Exception:
        return spans
    for block in data.get("blocks", []):
        if block.get("type") != 0:  # 0 = text block
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if span.get("text"):
                    spans.append(span)
    return spans


def _line_y(span: _Span) -> float:
    bbox = span["bbox"]
    # Use the baseline (bbox bottom) for banding: subscripts/superscripts share
    # the parent line's baseline, so they merge into their line instead of being
    # emitted as stray fragments.
    return float(bbox[3])


def _accumulate_sizes(counts: dict[float, int], spans: list[_Span]) -> None:
    """Add character-weighted font-size counts from *spans* into *counts*."""
    for span in spans:
        size = round(float(span["size"]), 1)
        counts[size] = counts.get(size, 0) + len(span["text"])


def _body_size_from_counts(counts: dict[float, int]) -> float:
    """Dominant body font size from character-weighted counts.

    Body is the size carrying the most characters, not the largest size
    (a title/heading on the page is short but big).
    """
    if not counts:
        return _BODY_FALLBACK
    return max(counts, key=lambda k: counts[k])


def _assemble_line(spans: list[_Span], y0: float, y1: float, page_no: int) -> _VisualLine:
    """Order spans by x and join with spaces across horizontal gaps."""
    spans.sort(key=lambda s: s["bbox"][0])
    parts: list[str] = []
    prev_x1: float | None = None
    xs = [s["bbox"][0] for s in spans]
    xe = [s["bbox"][2] for s in spans]
    size = max(round(float(s["size"]), 1) for s in spans)

    for span in spans:
        text = span["text"]
        if not text:
            continue
        sx0 = float(span["bbox"][0])
        sx1 = float(span["bbox"][2])
        if prev_x1 is not None and sx0 - prev_x1 > 1.0:
            parts.append(" ")
        parts.append(text)
        prev_x1 = sx1

    return _VisualLine(
        text="".join(parts).strip(),
        y0=y0,
        y1=y1,
        x0=min(xs) if xs else 0.0,
        x1=max(xe) if xe else 0.0,
        size=size,
        start_page=page_no,
    )


def _page_visual_lines(page: fitz.Page, page_no: int) -> list[_VisualLine]:
    """Reconstruct all visual lines on *page* in reading order."""
    spans = _iter_spans(page)
    if not spans:
        return []

    # Group spans into y-bands, then assemble each band as a visual line.
    spans.sort(key=_line_y)
    bands: list[list[_Span]] = [[spans[0]]]
    for span in spans[1:]:
        if abs(_line_y(span) - _line_y(bands[-1][0])) <= _Y_EPS:
            bands[-1].append(span)
        else:
            bands.append([span])

    lines: list[_VisualLine] = []
    for band in bands:
        y0 = min(s["bbox"][1] for s in band)
        y1 = max(s["bbox"][3] for s in band)
        line = _assemble_line(band, y0, y1, page_no)
        if line.text:
            lines.append(line)
    return lines


def _is_heading(line: _VisualLine, body_size: float) -> bool:
    """A line is a heading if notably larger than body."""
    return line.size >= body_size * _HEADING_RATIO


def _heading_level(line: _VisualLine, body_size: float) -> int:
    """Map a heading line's size ratio to a level (1-4)."""
    ratio = line.size / body_size
    if ratio >= 2.0:
        return 1
    if ratio >= 1.6:
        return 2
    if ratio >= 1.3:
        return 3
    return 4


def _is_list_start(line: _VisualLine) -> bool:
    text = line.text.lstrip(" ")
    if not text:
        return False
    if text[0] in _BULLET_PREFIX:
        return True
    return bool(_LIST_DIGIT_RE.match(line.text))


def _page_nodes(
    lines: list[_VisualLine],
    *,
    body_size: float,
    global_line: int,
    page_no: int,
) -> list[StructNode]:
    """Group visual lines into paragraph/heading/list nodes with local line
    numbers (anchored per page) so downstream chunking doesn't spread the
    whole page's line span onto every fragment."""
    if not lines:
        return []

    nodes: list[StructNode] = []

    def emit(node_type: StructNodeType, text: str, level: int, start: int, end: int) -> None:
        nodes.append(
            StructNode(
                node_type=node_type,
                text=text,
                level=level,
                start_line=global_line + start - 1,
                end_line=global_line + end - 1,
                start_page=page_no,
                end_page=page_no,
            )
        )

    i = 0
    while i < len(lines):
        line = lines[i]
        start = i + 1
        if _is_heading(line, body_size):
            # Gather consecutive heading lines of the same size (a wrapped
            # heading).  A title block spans multiple sizes (title, author,
            # section) — those stay separate headings.
            level = _heading_level(line, body_size)
            texts = [line.text]
            size = line.size
            j = i + 1
            while (
                j < len(lines)
                and _is_heading(lines[j], body_size)
                and abs(lines[j].size - size) < 0.5
            ):
                texts.append(lines[j].text)
                j += 1
            emit(StructNodeType.HEADING, " ".join(texts).strip(), level, start, j)
            i = j
            continue

        if _is_list_start(line):
            texts = [line.text]
            indent = line.x0
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if _is_list_start(nxt):
                    break
                # continuation of the current list item if indented
                if _indent_matches(nxt, indent) and _gap_ok(lines[j - 1], nxt):
                    texts.append(nxt.text)
                    j += 1
                    continue
                break
            emit(StructNodeType.LIST_ITEM, " ".join(texts).strip(), 0, start, j)
            i = j
            continue

        # paragraph: gather consecutive lines with small inter-line gaps
        texts = [line.text]
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if _is_heading(nxt, body_size) or _is_list_start(nxt):
                break
            if nxt.y0 - lines[j - 1].y1 > _GAP_PARAGRAPH:
                break
            texts.append(nxt.text)
            j += 1
        emit(StructNodeType.PARAGRAPH, " ".join(texts).strip(), 0, start, j)
        i = j

    return nodes


def _indent_matches(line: _VisualLine, indent: float) -> bool:
    return abs(line.x0 - indent) <= _INDENT_LIST_MATCH


def _gap_ok(prev: _VisualLine, curr: _VisualLine) -> bool:
    return curr.y0 - prev.y1 <= _GAP_PARAGRAPH


# ---------------------------------------------------------------------------
# Parser Implementation
# ---------------------------------------------------------------------------


class PdfParser:
    """Parse copyable-text PDF documents into structured nodes."""

    async def parse(self, raw: bytes, metadata: ParseMetadata) -> ParseResult:
        """Parse copyable-text PDF from raw bytes."""
        try:
            reader = fitz.open(stream=raw, filetype="pdf")
        except Exception as exc:
            return ParseError(
                code=ParseErrorCode.CONTENT_CORRUPT,
                message=f"Failed to read PDF: {exc}",
                details={"file_name": metadata.file_name},
            )

        try:
            num_pages = reader.page_count
            if num_pages == 0:
                return ParseError(
                    code=ParseErrorCode.EMPTY_DOCUMENT,
                    message="PDF has zero pages",
                    details={"file_name": metadata.file_name},
                )

            page_nodes: list[StructNode] = []
            text_lines: list[str] = []
            total_text_lines = 0
            global_line = 1
            size_counts: dict[float, int] = {}

            for page in reader:
                page_no = page.number + 1
                try:
                    spans = _iter_spans(page)
                    lines = _page_visual_lines(page, page_no)
                except Exception:
                    spans, lines = [], []
                    page_text = page.get_text("text") or ""
                    if page_text.strip():
                        text_lines.append(page_text)
                        for _ in page_text.splitlines():
                            total_text_lines += 1
                    continue

                if not lines:
                    continue  # empty / image-only / error page

                # Running body estimate across the whole document: heading-only
                # pages (TOC, cover) still classify correctly because earlier
                # body pages anchor the body size.
                _accumulate_sizes(size_counts, spans)
                body = _body_size_from_counts(size_counts)
                nodes = _page_nodes(
                    lines,
                    body_size=body,
                    global_line=global_line,
                    page_no=page_no,
                )
                page_nodes.extend(nodes)
                for n in nodes:
                    text_lines.append(n.text)
                    total_text_lines += n.end_line - n.start_line + 1
                # Advance the per-page anchor for the next page.
                global_line += sum(n.end_line - n.start_line + 1 for n in nodes)

            full_text = "\n".join(text_lines).rstrip("\n")
            if not page_nodes and not full_text.strip():
                return ParseError(
                    code=ParseErrorCode.SCANNED_PDF,
                    message=(
                        "No extractable text found - PDF appears to be a scanned / "
                        "image-only document"
                    ),
                    details={
                        "file_name": metadata.file_name,
                        "pages": str(num_pages),
                    },
                )

            return ParseSuccess(
                document=ParsedDocument(
                    metadata=metadata,
                    text=full_text,
                    structure=tuple(page_nodes),
                    total_lines=total_text_lines,
                )
            )
        finally:
            reader.close()
