# Provisional knowledge_qa contract

This declaration-only package is intentionally nested under `_provisional`, so trusted-root bulk reload
does not install or activate it. It exists only for deterministic Stage 5 contract tests against the single
provisional `GroundedQAApplicationPort`.

It is not a production Skill and is not exposed through HTTP, Worker, or Web entry points. Formal activation
requires Stage 3 exit and Stage 4 PostgreSQL, Worker, Citation API, and quality gates.
