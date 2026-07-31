"""Markdown document parser using markdown-it-py.

Extracts heading hierarchy, paragraphs, code blocks, and lists with
1-based line numbers for every structural node.
"""

from __future__ import annotations

from typing import Final

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
from markdown_it import MarkdownIt
from markdown_it.token import Token

# Maximum nesting depth for list items before flattening.
_MAX_LIST_DEPTH: Final[int] = 6


def _heading_level(token: Token) -> int:
    """Extract heading level from a heading ``Token``."""
    tag = token.tag  # h1-h6
    if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
        return int(tag[1])
    return 1


def _build_structure(tokens: list[Token]) -> tuple[StructNode, ...]:
    """Convert a flat ``markdown-it`` token stream into a tree of ``StructNode``."""

    nodes: list[StructNode] = []
    i = 0

    while i < len(tokens):
        token = tokens[i]

        # --- Heading -----------------------------------------------------------
        if token.type == "heading_open":
            level = _heading_level(token)
            end_token = tokens[i + 2] if i + 2 < len(tokens) else token  # inline after open
            inline_token = tokens[i + 1]
            text = _inline_text(inline_token)
            nodes.append(
                StructNode(
                    node_type=StructNodeType.HEADING,
                    text=text,
                    level=level,
                    start_line=token.map[0] + 1 if token.map else 0,
                    end_line=end_token.map[1]
                    if end_token.map and end_token.map[1]
                    else (token.map[0] + 1 if token.map else 0),
                )
            )
            i += 3  # heading_open + inline + heading_close
            continue

        # --- Fenced code block ------------------------------------------------
        if token.type == "fence":
            language: str | None = token.info.strip() if token.info else None
            if language == "":
                language = None
            start_line = token.map[0] + 1 if token.map else 0
            end_line = token.map[1] if token.map else start_line
            nodes.append(
                StructNode(
                    node_type=StructNodeType.CODE_BLOCK,
                    text=token.content,
                    language=language,
                    start_line=start_line,
                    end_line=end_line,
                )
            )
            i += 1
            continue

        # --- Indented code block (code_block) --------------------------------
        if token.type == "code_block":
            start_line = token.map[0] + 1 if token.map else 0
            end_line = token.map[1] if token.map else start_line
            nodes.append(
                StructNode(
                    node_type=StructNodeType.CODE_BLOCK,
                    text=token.content,
                    start_line=start_line,
                    end_line=end_line,
                )
            )
            i += 1
            continue

        # --- Paragraph --------------------------------------------------------
        if token.type == "paragraph_open":
            inline_tokens: list[Token] = []
            text_parts: list[str] = []
            para_start = token.map[0] + 1 if token.map else 0
            while i + 1 < len(tokens):
                i += 1
                if tokens[i].type == "paragraph_close":
                    break
                if tokens[i].type == "inline":
                    inline_tokens.append(tokens[i])
                    text_parts.append(_inline_text(tokens[i]))
            para_end = token.map[1] if token.map else para_start
            text = " ".join(text_parts)
            if text.strip() or para_start:  # preserve empty-ish paragraphs if positioned
                nodes.append(
                    StructNode(
                        node_type=StructNodeType.PARAGRAPH,
                        text=text,
                        start_line=para_start,
                        end_line=para_end,
                    )
                )
            i += 1
            continue

        # --- Unordered / ordered list ----------------------------------------
        if token.type in ("bullet_list_open", "ordered_list_open"):
            depth = 0
            list_nodes, consumed = _consume_list(tokens, i, depth + 1)
            nodes.extend(list_nodes)
            i += consumed
            continue

        # --- Blockquote -------------------------------------------------------
        if token.type == "blockquote_open":
            quote_start = token.map[0] + 1 if token.map else 0
            quote_text: list[str] = []
            depth = 1
            while i + 1 < len(tokens):
                i += 1
                current = tokens[i]
                if current.type == "blockquote_open":
                    depth += 1
                elif current.type == "blockquote_close":
                    depth -= 1
                    if depth == 0:
                        break
                elif current.type == "inline":
                    text = " ".join(_inline_text(current).split())
                    if text:
                        quote_text.append(text)
                elif current.type in ("fence", "code_block") and current.content:
                    quote_text.append(current.content)
            quote_end = token.map[1] if token.map else quote_start
            nodes.append(
                StructNode(
                    node_type=StructNodeType.QUOTE_BLOCK,
                    text="\n".join(quote_text),
                    start_line=quote_start,
                    end_line=quote_end,
                )
            )
            i += 1
            continue

        # --- Thematic break ---------------------------------------------------
        if token.type == "hr":
            line_num = token.map[0] + 1 if token.map else 0
            nodes.append(
                StructNode(
                    node_type=StructNodeType.THEMATIC_BREAK,
                    text="",
                    start_line=line_num,
                    end_line=line_num,
                )
            )
            i += 1
            continue

        # --- Inline text outside paragraph (fallback) ------------------------
        if token.type == "inline" and token.content.strip():
            line_num = token.map[0] + 1 if token.map else 0
            nodes.append(
                StructNode(
                    node_type=StructNodeType.RAW_TEXT,
                    text=token.content,
                    start_line=line_num,
                    end_line=line_num,
                )
            )
            i += 1
            continue

        i += 1

    return tuple(nodes)


def _consume_list(tokens: list[Token], start: int, depth: int) -> tuple[list[StructNode], int]:
    """Consume tokens for a list (bullet or ordered) and return its StructNodes."""
    items: list[StructNode] = []
    i = start + 1  # skip the *_list_open
    max_depth = _MAX_LIST_DEPTH
    if depth >= max_depth:
        # Too deep — skip until end token
        while i < len(tokens):
            if tokens[i].type in ("bullet_list_close", "ordered_list_close"):
                i += 1
                break
            i += 1
        return items, i - start

    while i < len(tokens):
        token = tokens[i]

        # End of list
        if token.type in ("bullet_list_close", "ordered_list_close"):
            i += 1
            break

        # List item
        if token.type == "list_item_open":
            item_start = token.map[0] + 1 if token.map else 0
            item_end = token.map[1] if token.map else item_start
            item_text: list[str] = []
            child_nodes: list[StructNode] = []
            i += 1  # move past list_item_open

            while i < len(tokens):
                t = tokens[i]
                if t.type == "list_item_close":
                    item_end = t.map[1] if t.map and t.map[1] else item_end
                    i += 1
                    break
                # Nested list
                if t.type in ("bullet_list_open", "ordered_list_open"):
                    nested, consumed = _consume_list(tokens, i, depth + 1)
                    child_nodes.extend(nested)
                    i += consumed
                    continue
                # Paragraph inside list item
                if t.type == "paragraph_open":
                    i += 1
                    while i < len(tokens) and tokens[i].type != "paragraph_close":
                        if tokens[i].type == "inline":
                            item_text.append(_inline_text(tokens[i]))
                        i += 1
                    i += 1  # paragraph_close
                    continue
                # Inline text directly
                if t.type == "inline" and t.content.strip():
                    item_text.append(t.content)
                    i += 1
                    continue
                i += 1

            text = " ".join(item_text)
            items.append(
                StructNode(
                    node_type=StructNodeType.LIST_ITEM,
                    text=text,
                    level=depth,
                    start_line=item_start,
                    end_line=item_end,
                    children=tuple(child_nodes),
                )
            )
            continue

        i += 1

    return items, i - start


def _inline_text(token: Token) -> str:
    """Extract plain text from an inline token, stripping formatting."""
    if not token.children:
        return token.content
    parts: list[str] = []
    for child in token.children:
        if child.type in ("text", "softbreak", "hardbreak") or child.type == "code_inline":
            parts.append(child.content)
        elif child.type in ("image",):
            # alt text for images
            alt = child.attrs.get("alt", "") if child.attrs else ""
            parts.append(str(alt))
    return " ".join(parts).strip()


# ---------------------------------------------------------------------------
# Parser Implementation
# ---------------------------------------------------------------------------


class MarkdownParser:
    """Parser for Markdown documents using ``markdown-it-py``.

    Produces a ``ParsedDocument`` with heading, paragraph, code-block,
    and list-item structural nodes annotated with 1-based line numbers.
    """

    async def parse(self, raw: bytes, metadata: ParseMetadata) -> ParseResult:
        """Parse Markdown content from raw bytes."""
        try:
            text = raw.decode(encoding=metadata.encoding or "utf-8")
        except (UnicodeDecodeError, LookupError) as exc:
            return ParseError(
                code=ParseErrorCode.ENCODING_FAILURE,
                message=f"Failed to decode file as {metadata.encoding or 'utf-8'}: {exc}",
                details={"file_name": metadata.file_name, "encoding": metadata.encoding or "utf-8"},
            )

        if not text.strip():
            return ParseError(
                code=ParseErrorCode.EMPTY_DOCUMENT,
                message="File contains no text content",
                details={"file_name": metadata.file_name},
            )

        try:
            md = MarkdownIt("commonmark")
            tokens = md.parse(text)
        except Exception as exc:
            return ParseError(
                code=ParseErrorCode.CONTENT_CORRUPT,
                message=f"Markdown parse failed: {exc}",
                details={"file_name": metadata.file_name},
            )

        structure = _build_structure(tokens)
        total_lines = text.count("\n") + 1

        # Also extract full text representation
        full_text = _extract_full_text(structure)

        return ParseSuccess(
            document=ParsedDocument(
                metadata=metadata,
                text=full_text,
                structure=structure,
                total_lines=total_lines,
            )
        )


def _extract_full_text(nodes: tuple[StructNode, ...]) -> str:
    """Recursively extract all text from a structure tree."""
    parts: list[str] = []
    for node in nodes:
        if node.text:
            parts.append(node.text)
        if node.children:
            child_text = _extract_full_text(node.children)
            if child_text:
                parts.append(child_text)
    return "\n".join(parts)
