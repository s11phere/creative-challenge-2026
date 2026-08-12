"""Trusted answer-shape identifiers shared by QA orchestration and generation."""

from enum import StrEnum


class GroundedAnswerMode(StrEnum):
    """Server-owned answer shapes; never accept free-form prompt instructions."""

    DEFAULT = "default"
    RESEARCH_DEEP_READ = "research_deep_read"
    RESEARCH_LITERATURE_REVIEW = "research_literature_review"


__all__ = ["GroundedAnswerMode"]
