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

### Amendment: product-level Assistant Agent (2026-08-06)

ADR-013 introduces `ConversationRun` as the shared durable parent identity for an Assistant turn,
grounded-QA projection, Runtime execution, checkpoint, approval, cancellation, and retry. The
product-level `AssistantAgentService` is an Application orchestrator, not a Skill or a second Runtime
framework. It dispatches persisted `run_kind` work through the existing Worker boundary; API request
handlers do not execute long model work and no second queue is added.

The existing `AgentRun`/Runtime contract remains the bounded execution projection until migration.
During migration, historical fixed-Skill runs and checkpoints retain their exact identity and pin.
Router decisions are application intents, not direct Runtime Tool calls. `RunBudget`, provider
limits, cancellation, loop detection, and emergency ceilings remain enforceable server-side safety
controls even though the product interface no longer presents them as a user quota.

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
