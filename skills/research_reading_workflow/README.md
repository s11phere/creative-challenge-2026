# research_reading_workflow 1.1.0

Active provisional manifest-v2 RAG workflow. `deep_read` pins one current published document and
combines paper-structure analysis with undergraduate teaching explanations. `literature_review`
pins two to eight current published documents and produces per-paper briefs, a citation-backed
evidence matrix, and a thematic review.

The runtime validates Research answers against `research-grounded-answer-v2`. A literature review
must contain distinct per-paper briefs, at least three matrix dimensions, at least two thematic
sections, consensus, condition-dependent differences, conflicts, evidence gaps, and limitations.
Cross-paper synthesis must cite at least two pinned documents. Missing sections or invalid
cross-document grounding use the single bounded repair attempt; an unrepaired structure is not
published as a review. A truncated or structurally invalid Research response is regenerated from
the pinned evidence under compact section limits; the incomplete candidate is not copied into the
repair prompt. Failed generation still records safe token, model-call, and repair counts.

Topic-only requests first return at most eight safe labels and pause for confirmation. The Skill
does not execute code, plan reproductions, validate hypotheses, or publish unsupported research
claims. Grounded QA remains the sole owner of retrieval evidence, citations, refusal, and the final
Assistant answer. Optional Markdown saving uses the existing workspace approval path.
