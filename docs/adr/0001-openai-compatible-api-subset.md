# 0001: Expose a strict subset of the OpenAI Chat Completions API

## Status

Accepted

## Context

The gateway needs one client-facing API across providers whose request and response
formats differ. Most LLM client libraries and tools already speak the OpenAI Chat
Completions format, and OpenAI-compatible inference servers use it too.

The full OpenAI API is large and changes often, and many of its options have no
equivalent at other providers. A gateway that accepts the full API but forwards
only part of it returns responses generated without options the client asked for,
with no indication that anything was dropped.

## Decision

The client-facing API is the OpenAI Chat Completions endpoint with an explicitly
listed subset of fields: text messages in the `system`, `user`, and `assistant`
roles, `max_tokens`, `temperature`, `top_p`, `stop`, `stream`, and
`stream_options.include_usage`.

Requests are parsed strictly. Any other field is rejected: known OpenAI fields the
gateway does not implement are reported as `unsupported_capability`, and unknown
fields as `invalid_request`. Adapters reject requests they cannot translate
faithfully for their provider, before calling it.

Responses use the OpenAI format with the alias as `model`. Usage is included only
when the provider reported it. Provider-specific output the format cannot carry,
such as thinking blocks, is dropped; output that would change the meaning of the
response, such as tool calls, is treated as an error.

## Alternatives considered

- **A gateway-specific API.** Full control over semantics, but every client would
  need a new integration.
- **Pass unknown fields through to the provider.** Works for OpenAI upstreams, but
  behaves differently per provider and can forward options an adapter has not been
  written to handle.
- **Accept and ignore unsupported fields.** Compatible with more clients, but hides
  incorrect behaviour.

## Consequences

- Existing OpenAI clients work for supported features without changes.
- Clients that send unsupported fields get an explicit error and must drop them or
  wait for support.
- Each new capability (tools, structured output, images) needs a schema change and
  a translation in every adapter, or an explicit rejection in adapters that cannot
  support it.
