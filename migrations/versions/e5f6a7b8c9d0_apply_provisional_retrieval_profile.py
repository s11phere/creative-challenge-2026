"""Apply the provisional Qwen3 reranked retrieval defaults.

Only empty profiles and profiles exactly equal to the previous generated
defaults are changed. Explicitly customized Space profiles remain untouched.

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-07-25 23:30:00.000000
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | None = None
depends_on: str | None = None


_OLD_PROFILE = {
    "chunk_size": 512,
    "chunk_overlap": 64,
    "top_k": 10,
    "rerank_k": 5,
    "fusion_alpha": 0.5,
    "extra": {},
}

_NEW_PROFILE = {
    "chunk_size": 512,
    "chunk_overlap": 64,
    "top_k": 5,
    "rerank_k": 10,
    "fusion_alpha": 0.35,
    "extra": {
        "keyword_candidate_k": "30",
        "dense_candidate_k": "30",
        "fusion_candidate_k": "30",
        "rrf_k": "60",
        "reranker_enabled": "true",
        "adjacent_window": "1",
        "max_chunks_per_document": "3",
    },
}


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE spaces "
            "SET retrieval_profile = CAST(:new_profile AS jsonb), updated_at = now() "
            "WHERE retrieval_profile = '{}'::jsonb "
            "OR retrieval_profile = CAST(:old_profile AS jsonb)"
        ).bindparams(
            old_profile=json.dumps(_OLD_PROFILE, separators=(",", ":")),
            new_profile=json.dumps(_NEW_PROFILE, separators=(",", ":")),
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE spaces "
            "SET retrieval_profile = CAST(:old_profile AS jsonb), updated_at = now() "
            "WHERE retrieval_profile = CAST(:new_profile AS jsonb)"
        ).bindparams(
            old_profile=json.dumps(_OLD_PROFILE, separators=(",", ":")),
            new_profile=json.dumps(_NEW_PROFILE, separators=(",", ":")),
        )
    )
