# ADR-003: Owned Agent Runtime Port With A Deferred LangGraph Adapter

- Status: Accepted
- Date: 2026-07-15

## Context

Knowledge QA first needs a bounded, deterministic workflow with versioned state, evidence, budgets,
and tool permissions. Binding domain or application code directly to one orchestration framework
would make Skill contracts and recovery semantics provider-specific.

## Decision

Define an owned Agent Runtime port and structured run state. The first `knowledge_qa` workflow is a
single-agent, finite state machine with a tool allowlist, step/token/time budgets, checkpoints, and
audited structured I/O. A LangGraph adapter may implement the port after the grounded QA path is
validated; no second agent framework is introduced.

## Alternatives

- Direct LangGraph use in application code was rejected because it leaks framework types across boundaries.
- A custom general-purpose autonomous planner was rejected because it adds risk without a measured P0 benefit.
- Multi-agent orchestration was deferred because no current workflow requires separate permissions or contexts.

## Consequences

The project owns stable run and Skill semantics and must maintain adapter contract tests. Some
framework-specific features require explicit mapping. Initial development stays focused on a small,
deterministic workflow.

## Reassessment Triggers

Reassess when checkpoints, human approval, parallel execution, or context isolation cannot be
implemented through the port without material duplication or loss of reliability.
