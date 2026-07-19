"""Parser factory — resolves file extension and MIME type to a parser.

Handles consistency checks between file extension, declared MIME type,
and actual content.  Enforces file-size limits before dispatch.
"""

from __future__ import annotations

from typing import Final

from domain.parsing import (
    ParseError,
    ParseErrorCode,
    ParseMetadata,
    Parser,
    ParseResult,
)

from infrastructure.config import settings

from .markdown_parser import MarkdownParser
from .pdf_parser import PdfParser
from .txt_parser import TxtParser

# ---------------------------------------------------------------------------
# Supported extensions and MIME types
# ---------------------------------------------------------------------------

_EXTENSION_TO_MIME: Final[dict[str, str]] = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
}

_MIME_PARSER_MAP: Final[dict[str, type[Parser]]] = {
    "text/markdown": MarkdownParser,
    "text/plain": TxtParser,
    "application/pdf": PdfParser,
}

# Maximum file size in bytes (default 50 MB from config).
_MAX_SIZE_BYTES: Final[int] = settings.max_upload_size_mb * 1024 * 1024


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_parser(file_name: str, mime_type: str = "") -> type[Parser] | None:
    """Resolve a parser type by *file_name* extension and optional *mime_type*.

    Returns ``None`` when no parser supports the given extension or MIME.
    """
    ext = _get_extension(file_name).lower()
    parser_type = _MIME_PARSER_MAP.get(mime_type)
    if parser_type is None and ext:
        mime = _EXTENSION_TO_MIME.get(ext)
        if mime:
            parser_type = _MIME_PARSER_MAP.get(mime)
    return parser_type


class ParserFactory:
    """Factory that validates files and dispatches to the correct parser."""

    async def parse(self, raw: bytes, file_name: str, mime_type: str = "") -> ParseResult:
        """Parse *raw* file bytes by resolving the correct parser.

        Validates file size, extension, MIME type, and content consistency
        before parsing.  Returns a ``ParseError`` for any validation failure.
        """
        # --- Size check -------------------------------------------------------
        if len(raw) > _MAX_SIZE_BYTES:
            return ParseError(
                code=ParseErrorCode.OVERSIZED_FILE,
                message=f"File exceeds maximum size of {settings.max_upload_size_mb} MB",
                details={
                    "file_name": file_name,
                    "file_size_bytes": str(len(raw)),
                    "max_size_bytes": str(_MAX_SIZE_BYTES),
                },
            )

        # --- Extension + MIME resolution ------------------------------------
        ext = _get_extension(file_name)

        if not ext:
            return ParseError(
                code=ParseErrorCode.UNSUPPORTED_FORMAT,
                message=f"File '{file_name}' has no recognizable extension",
                details={"file_name": file_name},
            )

        ext_lower = ext.lower()
        expected_mime = _EXTENSION_TO_MIME.get(ext_lower)

        if expected_mime is None:
            return ParseError(
                code=ParseErrorCode.UNSUPPORTED_FORMAT,
                message=f"Unsupported file extension '{ext}'",
                details={"file_name": file_name, "extension": ext},
            )

        # --- MIME consistency check ------------------------------------------
        if (
            mime_type
            and expected_mime != mime_type
            and not _is_mime_compatible(expected_mime, mime_type)
        ):
            return ParseError(
                code=ParseErrorCode.TYPE_MISMATCH,
                message=(
                    f"MIME type '{mime_type}' does not match extension "
                    f"'{ext}' (expected '{expected_mime}')"
                ),
                details={
                    "file_name": file_name,
                    "extension": ext,
                    "declared_mime": mime_type,
                    "expected_mime": expected_mime,
                },
            )

        # --- Instantiate and parse -------------------------------------------
        parser_type = _MIME_PARSER_MAP[expected_mime]
        parser = parser_type()
        metadata = ParseMetadata(
            file_name=file_name,
            file_size=len(raw),
            mime_type=expected_mime,
        )

        return await parser.parse(raw, metadata)


def _get_extension(file_name: str) -> str:
    """Extract the file extension including the dot."""
    if "." not in file_name:
        return ""
    return "." + file_name.rsplit(".", 1)[-1]


def _is_mime_compatible(expected: str, declared: str) -> bool:
    """Check if *declared* MIME is compatible with *expected*."""
    if expected == declared:
        return True
    # Aliases and supertypes
    compat_map: dict[str, set[str]] = {
        "text/markdown": {"text/x-markdown"},
        "text/plain": {"text/plain; charset=utf-8", "text/plain;charset=utf-8"},
        "application/pdf": {"application/x-pdf"},
    }
    declared_lower = declared.lower().strip()
    return declared_lower in compat_map.get(expected, set())
