# knowledge_agent 0.2.0

This provisional Skill runs a bounded LLM decision loop over the server-registered read-only Tools
`inspect_retrieval 1.0.0` and `grounded_qa 1.0.0`. Grounded QA remains the answer, Citation,
persistence, and SSE authority. The previous `0.1.0` package remains installed separately for
fixed-run recovery and rollback.

The Agent cannot select arbitrary Tools, change Space or permissions, or invoke write Tools. Tool
output returned to the Agent contains only status, result type, and Citation count. The default fake
provider chooses the Tool deterministically; an external OpenAI-compatible provider requires explicit
endpoint, model, key, and external-policy configuration.

Assistant operational metrics are content-safe: routing, command, clarification, token, latency, and
termination observations carry only aggregate values and safe labels. They never contain user text,
prompts, document content, Tool results, Provider responses, or internal resource IDs. The synthetic
Assistant routing evaluator is development/provisional only and cannot be used as a formal Skill
quality gate.
