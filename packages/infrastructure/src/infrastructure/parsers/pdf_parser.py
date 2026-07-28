"""Copyable-text PDF parser using PyMuPDF.

Extracts text with 1-based page numbers. Scanned / image-only PDFs return a
``ParseError`` with code ``scanned_pdf`` rather than silently producing empty
text.
"""

from __future__ import annotations

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


class PdfParser:
    """Parse copyable-text PDF documents one physical page at a time."""

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
            total_text_lines = 0
            text_lines: list[str] = []

            for page_num, page in enumerate(reader, start=1):
                try:
                    page_text = page.get_text("text") or ""
                except Exception:
                    page_text = ""

                stripped = page_text.strip()
                page_line_count = page_text.count("\n") + 1 if stripped else 0
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

            full_text = "\n".join(text_lines).rstrip("\n")
            if not full_text.strip():
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
