# Agent runtime

A separately deployable service that runs bounded LLM workflows. It reaches
models only through the AI Gateway's `/v1/chat/completions` API, so routing, rate
limits, budgets, retries, and provider credentials stay in the gateway.

The runtime has its own credentials (`AGENT_API_KEYS`, at least 32 characters in
production) and reads all configuration from `AGENT_*` environment variables.
It never imports the gateway's Python package.

Documentation: [docs/agent-runtime.md](../../docs/agent-runtime.md).
