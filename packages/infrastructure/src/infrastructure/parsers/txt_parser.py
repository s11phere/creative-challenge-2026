"""Plain-text (TXT) document parser.

Splits text by blank-line-separated paragraphs and preserves 1-based
line numbers.  Detects and reports encoding failures explicitly.
"""

from __future__ import annotations

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

# Tries in order when the declared encoding fails.
_FALLBACK_ENCODINGS = ["utf-8", "utf-16", "latin-1", "cp1252"]


class TxtParser:
    """Parser for plain-text (``.txt``) files.

    Each blank-line-separated block becomes a ``PARAGRAPH`` node.  Lines
    that are not separated by blank lines are grouped into the same
    paragraph.
    """

    async def parse(self, raw: bytes, metadata: ParseMetadata) -> ParseResult:
        """Parse plain text from raw bytes."""
        text, encoding, err = _decode(raw, metadata.encoding)
        if err:
            return err

        if not text.strip():
            return ParseError(
                code=ParseErrorCode.EMPTY_DOCUMENT,
                message="File contains no text content",
                details={"file_name": metadata.file_name},
            )

        lines = text.splitlines(keepends=False)
        total_lines = len(lines)

        # Build paragraph nodes from blank-line-separated blocks.
        structure = _build_paragraphs(lines)
        # Full text is the original decoded text.
        full_text = text.rstrip("\n")

        # Update metadata with detected encoding.
        parsed_metadata = ParseMetadata(
            file_name=metadata.file_name,
            file_size=metadata.file_size,
            mime_type=metadata.mime_type,
            encoding=encoding,
        )

        return ParseSuccess(
            document=ParsedDocument(
                metadata=parsed_metadata,
                text=full_text,
                structure=structure,
                total_lines=total_lines,
            )
        )


def _decode(raw: bytes, declared_encoding: str) -> tuple[str, str, ParseError | None]:
    """Decode *raw* bytes trying the declared encoding then fallbacks."""
    if declared_encoding and declared_encoding.lower() != "utf-8":
        # Try the declared / metadata encoding first.
        try:
            return raw.decode(declared_encoding), declared_encoding, None
        except (UnicodeDecodeError, LookupError):
            pass  # fall through to fallback list

    encodings = _FALLBACK_ENCODINGS
    if declared_encoding and declared_encoding.lower() not in (e.lower() for e in encodings):
        encodings.insert(0, declared_encoding)

    for enc in encodings:
        try:
            return raw.decode(enc), enc, None
        except (UnicodeDecodeError, LookupError):
            continue

    return (
        "",
        "",
        ParseError(
            code=ParseErrorCode.ENCODING_FAILURE,
            message=f"Could not decode file with any of {_FALLBACK_ENCODINGS}",
            details={"declared_encoding": declared_encoding},
        ),
    )


def _build_paragraphs(lines: list[str]) -> tuple[StructNode, ...]:
    """Group *lines* into paragraphs separated by blank lines."""
    paragraphs: list[StructNode] = []
    para_start = 1  # 1-based
    para_lines: list[str] = []
    para_line_nums: list[int] = []

    for idx, line in enumerate(lines):
        line_num = idx + 1

        if line.strip() == "":
            # Blank line — flush pending paragraph.
            if para_lines:
                text = "\n".join(para_lines)
                paragraphs.append(
                    StructNode(
                        node_type=StructNodeType.PARAGRAPH,
                        text=text,
                        start_line=para_start,
                        end_line=para_line_nums[-1] if para_line_nums else para_start,
                    )
                )
                para_lines = []
                para_line_nums = []
            para_start = line_num + 1
        else:
            if not para_lines:
                para_start = line_num
            para_lines.append(line)
            para_line_nums.append(line_num)

    # Flush last paragraph.
    if para_lines:
        text = "\n".join(para_lines)
        paragraphs.append(
            StructNode(
                node_type=StructNodeType.PARAGRAPH,
                text=text,
                start_line=para_start,
                end_line=para_line_nums[-1] if para_line_nums else para_start,
            )
        )

    return tuple(paragraphs)
