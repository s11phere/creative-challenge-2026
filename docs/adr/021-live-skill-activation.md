# ADR-021: Live Skill Activation

- Status: Accepted
- Date: 2026-08-14

## Context

Installed Skills were loaded as active at process startup. The Web catalog did
not expose that state, and a user could not remove a Skill from the Assistant
catalog without restarting the API process. Personal Skills also required an
explicit activation step after creation.

## Decision

- `skill_activations` persists an `active` boolean alongside the immutable
  version pin. Existing rows migrate as active, and newly installed Skills are
  active by default.
- The activation API updates the durable record and both in-process registries
  before it returns. The Assistant catalog is rebuilt from the active registry
  for each new turn, so the following conversation sees the changed directory.
- Disabling a Skill removes only its active pointer. It neither deletes the
  package nor changes the fixed identity of an already-created Run.
- The fixed Skill catalog and personal Skill catalog both expose an active
  status and a single toggle control. Draft promotion remains subject to its
  existing validation and evaluation gate.

## Alternatives

- Restart the API after each change: rejected because it delays the effect and
  disrupts unrelated conversations.
- Delete the package when disabled: rejected because deactivation must be
  reversible and pinned historical Runs need immutable package identity.
- Keep activation state only in browser memory: rejected because it would not
  survive a new process or apply to server-side routing.

## Consequences

The API has an additional state transition to validate and observe. A running
conversation keeps its existing Skill pin, while only subsequent turns see a
changed catalog. The change does not alter retrieval or answer quality gates;
ADR-010 and ADR-011 remain in force.

## Reassessment

Reassess when activation becomes tenant- or Space-scoped, needs scheduled
changes, or is coordinated across multiple API processes.
