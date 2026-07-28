# Demo And Evaluation Data Policy

## Scope

This policy applies to corpus files, evaluation cases, excerpts, screenshots, generated reports,
logs, embeddings, and demo packages. Source documents are untrusted data and never instructions.

## Classification

| Value | Meaning | Repository and demo rule |
| --- | --- | --- |
| `public_demo` | Redistribution terms are known and satisfied | May be packaged only with required attribution and license files |
| `private_local` | Personal, restricted, or license-undetermined material | Must remain local; never committed, uploaded, cached externally, or shown in a public demo |
| `restricted` | Explicit prohibition or sensitive content | Excluded unless written authorization and a documented exception exist |

`allowed_uses` narrows the classification. `repository_fixture` must be explicitly present before a
file is copied into a repository. `redistribution` records the remaining obligation or prohibition.

## Corpus v0 Decisions

- CS229 files are `private_local` until provenance and redistribution rights are documented.
- AI lab files are `private_local` and redistribution is prohibited by the supplied course notice.
- Personal notes are `private_local` until the author confirms they contain no restricted excerpts.
- OmniStudio sources may be used under GPLv3 with attribution and license preservation.
- Purpose-built validation fixtures are CC0-1.0 and may be committed or demonstrated.

For corpus v0, the project has selected an internal-only freeze exception:
`distribution_scope: internal_team_only` permits authorized team members to use
`private_local` and license-undetermined sources for local development and
evaluation. It does not permit Git upload, public demos, redistribution outside
the team, or external model-provider calls. The original license values remain
in the manifest as facts and are not rewritten to imply public rights.

## Redaction And Review

Before approving a source, record its owner, origin, license, allowed uses, sensitivity, SHA-256,
and reviewer. Remove names, student IDs, email addresses, credentials, private URLs, account data,
and identifying screenshots. Redaction produces a new source version and hash; do not overwrite the
original while retaining its old hash.

Automated scans are only a first pass. A human reviewer must open representative pages and code,
verify attribution, and check that excerpts do not reconstruct a restricted work.

## External Models

Private or restricted content stays on the local machine by default. Sending any document excerpt
to an external model provider requires an explicit provider configuration, visible user consent,
and a policy decision that records what leaves the device. Test and CI runs use fakes and must not
depend on paid external models.

## Logs And Derived Data

Logs store IDs, hashes, counts, timing, error classes, and redacted summaries, not full documents or
prompts. Embeddings, parser caches, screenshots, and model responses inherit the source's sensitivity.
Debug content capture is opt-in, time-limited, and excluded from version control.

## Approval Checklist

- Source path and SHA-256 match the manifest.
- License and redistribution values are supported by evidence.
- PII and secrets scans plus human review are complete.
- Public-demo attribution and license files are present.
- External-provider behavior is known and approved.
- Evaluation quotes are the minimum text needed to verify a claim.

Public release still requires every selected source to pass this checklist. The
internal v0 freeze is recorded separately in `docs/stage-0-acceptance.md`.
