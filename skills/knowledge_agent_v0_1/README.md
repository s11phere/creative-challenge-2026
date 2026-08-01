# knowledge_agent 0.1.0

This provisional Skill runs a bounded LLM decision loop over one server-registered read-only Tool,
`grounded_qa 1.0.0`. Grounded QA remains the answer, Citation, persistence, and SSE authority.

The Agent cannot select arbitrary Tools, change Space or permissions, or invoke write Tools. Tool
output returned to the Agent contains only status, result type, and Citation count. The default fake
provider chooses the Tool deterministically; an external OpenAI-compatible provider requires explicit
endpoint, model, key, and external-policy configuration.
