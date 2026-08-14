# exam_preparation_workflow 1.2.1

Active, provisional Harness v2 Skill for interactive exam preparation. It provides a persistent
course-map, adaptive diagnostic, review-material, card-preview, mock-exam, and post-submission review
capabilities inside one conversational flow.

## Runtime integration

The Worker registers a native adapter for the exact `exam_preparation_workflow 1.2.1` pin and these
three logical tools:

- `exam_prepare 1.2.1`: pin the selected published sources and create or recover one conversational
  Session, recover locked diagnostic/mock papers, and return UI-safe question data without hidden material.
- `exam_submit 1.2.1`: parse numbered chat answers, validate paper identity and ownership, and
  save answers; advance diagnostics; and expose review material only after a valid submission or
  explicit reveal. A broad-diagnostic submission persists its 3–5 question adaptive check before
  returning `submitted_follow_up_ready`, so the assistant cannot announce a missing follow-up.
- `exam_review_cards 1.2.1`: create citation-backed previews and delegate persistence to the existing
  durable approval and Derived Knowledge path. Before approval it must return
  `SKILL_WRITE_REQUIRES_APPROVAL` with `side_effects=0`.

The adapter preserves stable IDs and a capability history in `ExamSession`; legacy phases are a
compatibility projection, not a mandatory workflow. It must
reuse SearchService and the Grounded QA/Citation ports, enforce Space and DocumentVersion scope, and
never treat model output as permission or persisted state. It must not register process-execution
capability for submitted programming answers.

Do not route this Skill through the generic knowledge adapter. If the configured real chat model
lacks native Tool use, generation rejects with `EXAM_MODEL_TOOL_USE_REQUIRED`.

## Message interaction renderer

Anchor `interaction_model` to the assistant message produced by its Run and key submissions by
`interaction_id + paper_id + paper_version + submission_id`:

- `quiz`: compact single/multiple-choice controls within the message.
- `mock_exam`: sectioned paper, points, suggested time, objective and subjective inputs, and one
  submit action.
- `review_plan`, `study_guide`, `review_cards`, `exam_review`: structured sections instead of a chat
  essay.

The v3 action endpoint remains for compatibility. Conversational submissions use `exam_submit`, which revalidates local ownership, Space, locked paper version,
interaction identity and an idempotency key. Each action is a short request against the same
Session; refresh and retry return the same visible paper. Concurrent or stale submissions fail
without changing state.

Answers, rubrics, private scoring material, and source excerpts do not enter the redacted
`agent-run-sse-v4` timeline. The Web uses the separately authorized versioned interaction/action
projection. The Web may render public questions next to their assistant message; clicking an answer
creates a normal user Turn using the same numbered-answer contract as typed input.

## Current fallback and limits

If the interaction renderer is unavailable, present the schema as compact Markdown and accept objective answers
as `Q1:A, Q2:BD`; accept subjective answers as one labelled section per question. This is a display
fallback and the canonical conversational submission format.

Question papers never contain answer keys, explanations, reference answers, rubrics, or scores.
Past papers inform only clearly labelled historical structure and difficulty. Subjective grading is
advisory and exposes confidence plus human-review requirements. Programming review is static and
never executes submitted code. All eval evidence is synthetic, development-only, and provisional.
