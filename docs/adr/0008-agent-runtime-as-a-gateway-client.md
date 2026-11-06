# 0008: The agent runtime is a separate service and an ordinary gateway client

## Status

Accepted

## Context

Agent workflows need model calls with the controls the gateway already provides:
routing, provider credentials, rate limits, budgets, retries, and metrics. They
also need workflow state, tools, and validation of model output that has nothing
to do with proxying completions. The gateway's API is a strict text-only subset of
Chat Completions, without tool calling or structured output.

## Decision

- The runtime is its own package (`services/agent_runtime`) and process, with its
  own credentials and configuration. Neither package imports the other.
- It calls `POST /v1/chat/completions` with one gateway key, sending only `model`,
  `messages`, and `max_tokens`. Every gateway limit and budget for that key applies
  to all investigations.
- Tool selection is a JSON plan in the model's text reply, parsed and validated by
  the runtime, rather than native provider tool calling. The gateway's API and
  adapters are unchanged.
- The runtime does not retry gateway calls. Its errors record whether the gateway
  may have executed the call, for a later idempotent retry policy.

## Alternatives considered

- **Workflows inside the gateway process.** Removes one network hop, but mixes
  tool execution and long-running work into the request path that enforces
  limits, and ties their deployment and failure domains together.
- **Native tool calling through the gateway.** Lets providers enforce the tool
  schema, but requires extending the request schema, every adapter, admission
  token estimates, and cost accounting. The runtime would still have to validate
  the calls, since a schema-conforming call can still be unauthorised.
- **Calling providers directly from the runtime.** Duplicates credentials, limits,
  and accounting, and bypasses budgets.

## Consequences

- Investigations are limited by the gateway's per-key limits as a whole, not per
  caller of the runtime. Per-caller budgets would need one gateway key per caller
  or workflow-level accounting in the runtime.
- JSON plans depend on the model following a format; malformed replies end the
  investigation instead of being repaired.
