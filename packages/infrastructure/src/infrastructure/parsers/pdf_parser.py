"""Copyable-text PDF parser using pypdf.

Extracts text with 1-based page numbers.  Scanned / image-only PDFs
return a ``ParseError`` with code ``scanned_pdf`` rather than silently
producing empty text.
"""

from __future__ import annotations

from io import BytesIO

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
from pypdf import PdfReader


class PdfParser:
    """Parser for copyable-text PDF documents.

    Each page becomes a ``RAW_TEXT`` node annotated with 1-based page
    number and line numbers.
    """

    async def parse(self, raw: bytes, metadata: ParseMetadata) -> ParseResult:
        """Parse copyable-text PDF from raw bytes."""
        try:
            reader = PdfReader(BytesIO(raw))
        except Exception as exc:
            # pypdf raises a variety of exceptions for corrupt streams.
            return ParseError(
                code=ParseErrorCode.CONTENT_CORRUPT,
                message=f"Failed to read PDF: {exc}",
                details={"file_name": metadata.file_name},
            )

        num_pages = len(reader.pages)
        if num_pages == 0:
            return ParseError(
                code=ParseErrorCode.EMPTY_DOCUMENT,
                message="PDF has zero pages",
                details={"file_name": metadata.file_name},
            )

        page_nodes: list[StructNode] = []
        total_text_lines = 0
        text_lines: list[str] = []

        for page_num, page in enumerate(reader.pages, start=1):
            try:
                page_text = page.extract_text() or ""
            except Exception:
                page_text = ""

            stripped = page_text.strip()

            # Detect scanned / image-only PDF — no extractable text.
            if not stripped:
                # If *every* page is empty, the whole document is scanned.
                # If only *this* page is empty, we still record a node.
                page_text = ""

            # Sanity: a page with very little text is probably irrelevant
            # but still counts as a node for line-tracking purposes.
            page_line_count = page_text.count("\n") + 1 if page_text.strip() else 0
            text_lines.append(page_text)

            page_nodes.append(
                StructNode(
                    node_type=StructNodeType.RAW_TEXT,
                    text=stripped,
                    start_page=page_num,
                    end_page=page_num,
                    start_line=total_text_lines + 1,
                    end_line=total_text_lines + page_line_count,
                )
            )
            total_text_lines += page_line_count

        # If absolutely no text was found across all pages, it's scanned.
        full_text = "\n".join(text_lines).rstrip("\n")
        if not full_text.strip():
            return ParseError(
                code=ParseErrorCode.SCANNED_PDF,
                message=(
                    "No extractable text found — PDF appears to be a scanned / image-only document"
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
