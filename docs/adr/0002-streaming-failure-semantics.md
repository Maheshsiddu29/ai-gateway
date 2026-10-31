# 0002: Stream normalization and failure semantics

## Status

Accepted

## Context

Providers stream in different formats: OpenAI-compatible servers send
`chat.completion.chunk` objects followed by `data: [DONE]`; Anthropic sends typed
events ending in `message_stop`, and can report errors in an `error` event after a
200 response. Clients expect the OpenAI chunk format.

With SSE the HTTP status is sent before the body. Once any output has been sent,
a later failure can no longer change the status, and a client that has displayed
partial text cannot transparently receive a different generation.

## Decision

- Adapters convert provider streams into provider-neutral events: `TextDelta`, then
  exactly one `StreamEnd` carrying the finish reason and usage. A stream that ends
  without the provider's completion signal raises an error instead of producing
  `StreamEnd`.
- The gateway waits for the first event before sending response headers. Failures
  before any output return a regular JSON error with the matching status.
- Failures after output has started are reported as a final
  `data: {"error": {...}}` event using the same error object as JSON responses.
  `[DONE]` is sent only after a successful `StreamEnd`.
- The gateway never retries or switches providers once output has been delivered.
- Client disconnects cancel the upstream request.

## Alternatives considered

- **Send headers as soon as the upstream responds.** The first byte arrives
  earlier, but errors reported in the first events (such as Anthropic's overloaded
  error) would become in-stream errors with a 200 status.
- **Close the connection without an error event.** Simpler, but clients cannot
  distinguish a gateway failure from a network problem, and some treat a closed
  stream as complete.
- **Retry or fail over mid-stream.** The client would receive text from two
  different generations joined together.

## Consequences

- The official OpenAI Python SDK (checked at version 3.42.0) raises `APIError`
  when a `data:` event contains an `error` object, so its users see the failure as
  an exception. Clients that wait for `[DONE]` can also detect the interruption.
- Time to first byte equals time to first token.
- Failover, when added, can only apply before the first event has been sent.
