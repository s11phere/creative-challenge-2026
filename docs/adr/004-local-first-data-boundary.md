# ADR-004: Local-First Data Boundary And Explicit External Model Use

- Status: Accepted
- Date: 2026-07-15

## Context

The corpus includes personal notes, license-undetermined course material, and explicitly restricted
teaching files. Model providers may receive prompts and excerpts, creating a data-boundary change that
must not happen implicitly.

## Decision

Store source files, parsed text, indexes, evaluation artifacts, and logs locally by default. Route all
model access through `ModelGateway` capability aliases. External providers are opt-in per deployment,
must visibly disclose that content leaves the device, and may receive only sources whose policy allows
that use. CI uses deterministic fakes. Logs exclude full private text, prompts, secrets, and credentials.

## Alternatives

- Cloud-first storage was rejected because it conflicts with the product promise and current data rights.
- Direct provider SDK calls were rejected because they scatter policy and make provider replacement difficult.
- A blanket ban on external models was rejected because approved public sources and explicit user consent may permit them.

## Consequences

Local operation remains possible without a paid model. Provider configuration needs policy checks,
consent visibility, redaction, and auditing. Some model quality or throughput may be lower when only
local providers are permitted.

## Reassessment Triggers

Reassess when a new deployment model, data agreement, team-sharing feature, or local-model limitation
changes the approved data boundary. Any relaxation requires an ADR update and privacy regression tests.
