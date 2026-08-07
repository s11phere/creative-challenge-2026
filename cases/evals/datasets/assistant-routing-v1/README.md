# Dataset: assistant-routing-v1

## Purpose

`assistant-routing-v1` is a synthetic development-only routing set for the generic Assistant
Agent. It verifies routing and safety contracts without reading the controlled knowledge-QA corpus,
calling an external Provider, or claiming a quality gate.

The set covers ordinary conversation, knowledge requests, explicit commands, ambiguous resources,
prompt injection, cross-Space requests, write approval, and counterexamples that must not invoke a
Skill. Its examples are purpose-built synthetic strings; they are not user conversations, corpus
queries, model responses, source excerpts, or formal evaluation cases.

## Boundary

- `status` is `provisional`; this dataset is not a formal holdout and cannot enable one.
- Every row declares `content_policy: synthetic_only` and may be committed as a repository fixture.
- No row contains document text, identifiers from the corpus, personal content, credentials, or an
  external Provider response.
- The catalog names express expected policy behavior only. A later implementation must validate the
  active catalog, caller policy, current Space, fixed Skill identity, resource scope, approval, and
  deployment policy independently.

## Integrity

`manifest.yaml` pins the SHA-256 of the schema and development JSONL. Any semantic change requires
a new dataset version and a new manifest hash; do not add a holdout split to this version.

