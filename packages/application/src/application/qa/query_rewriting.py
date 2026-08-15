"""LLM-backed query rewriting for the QA planning stage.

Rewrites the user's question into search sub-queries through the ModelGateway
``fast_chat`` capability. The rewriter is question-only: it never sees the
answer claims or retrieved evidence, so multi-query expansion stays a legal
retrieval-side lever (R4-04).
"""

from __future__ import annotations

import json

from domain.grounded_qa import QAContractError, QuestionInput
from model_gateway import (
    CapabilityAlias,
    ChatMessage,
    ChatRequest,
    ChatRole,
    ModelGateway,
)

_REWRITE_FORMAT_INSTRUCTION = (
    'Return exactly one JSON object with no Markdown: {"queries": ["query one", "query two"]}'
)


class LlmQueryRewriter:
    """Rewrite a question into search sub-queries via the ``fast_chat`` capability."""

    def __init__(self, gateway: ModelGateway, *, max_tokens: int = 1024) -> None:
        self._gateway = gateway
        self._max_tokens = max_tokens

    async def rewrite(self, question: QuestionInput, *, max_queries: int) -> tuple[str, ...]:
        if max_queries < 1:
            raise QAContractError("Rewrite max_queries must be positive")
        request = ChatRequest(
            messages=(
                ChatMessage(role=ChatRole.SYSTEM, content=_system_prompt(max_queries)),
                ChatMessage(role=ChatRole.USER, content=question.question),
            ),
            temperature=0.0,
            max_tokens=self._max_tokens,
        )
        response = await self._gateway.chat(request, capability=CapabilityAlias.FAST_CHAT)
        return _parse_rewrite_queries(response.text, max_queries=max_queries)


def _system_prompt(max_queries: int) -> str:
    return (
        "You rewrite a user's question into search queries for a local knowledge base. "
        f"Produce up to {max_queries} distinct search queries that would retrieve the source "
        "passages needed to answer the question. Rewrite for retrievability: extract key "
        "entities and technical terms, translate between Chinese and English where the source "
        "may use either, and split multi-part questions into focused sub-queries. Base every "
        "query only on the question text; do not invent facts, names, or answer content. "
        "Interpret the request semantically: presentation format, saving or writing a file, a "
        "workspace path, and other delivery operations are execution constraints rather than "
        "knowledge subquestions, so do not create search queries for them. Keep the subject "
        "matter needed for the requested artifact. " + _REWRITE_FORMAT_INSTRUCTION
    )


def _parse_rewrite_queries(text: str, *, max_queries: int) -> tuple[str, ...]:
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise QAContractError("Query rewriter output is not exact JSON") from exc
    if not isinstance(value, dict):
        raise QAContractError("Query rewriter output must be a JSON object")
    raw_queries = value.get("queries")
    if not isinstance(raw_queries, list):
        raise QAContractError("Query rewriter output must contain a queries list")
    queries = tuple(
        str(item).strip() for item in raw_queries if isinstance(item, str) and item.strip()
    )
    return queries[:max_queries]


__all__ = ["LlmQueryRewriter"]
