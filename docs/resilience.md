# Provider resilience

How the gateway behaves when providers are slow, failing, or unreachable: request
deadlines, retries, and circuit breakers.

## Deadlines

Every request has one end-to-end deadline, `request_deadline_seconds`, measured on
the gateway's monotonic clock from the start of request handling. It covers body
reading, admission, every provider attempt, every backoff delay, and for streams the
whole response. Retries never reset it.

Per-operation provider timeouts (`connect_timeout_seconds`, `read_timeout_seconds`,
and so on) still apply inside the deadline; whichever expires first ends the
operation.

On expiry:

- Before a response has started, the client receives 504 `deadline_exceeded`.
- During a stream, the gateway sends a final `data: {"error": ...}` event with type
  `deadline_exceeded` and does not send `[DONE]`.
- If the client has stopped reading, so the gateway is blocked writing, the
  connection is closed without an error event.

In each case the upstream request is cancelled, its connection closed, and the
admission reservation settled.

## Retries

A failed attempt is retried only if all of these hold:

1. The provider certainly did not run the request: the connection was never
   established (connection refused, connect or pool timeout), or the provider
   answered with an error status.
2. The failure is transient: HTTP 429, a status in `retry_on_status` (default 429,
   500, 502, 503, 529), or a connection failure.
3. Attempts remain (`max_attempts_per_provider`, default 2, at most 5).
4. The backoff delay ends before the deadline.
5. If the provider sent `Retry-After`, it is at most `max_retry_after_seconds`.

The delay after attempt *n* is
`min(retry_max_backoff_seconds, retry_initial_backoff_seconds * 2^(n-1))`, reduced
by a random fraction of up to `retry_jitter_ratio`, and never shorter than the
provider's `Retry-After`.

Not retried:

| Failure | Reason |
| --- | --- |
| Read timeout, connection dropped after sending, 504, 408 | The provider may be running the request. |
| Malformed response, incomplete stream | The provider ran the request. |
| 400, 401, 403, 404, 422, unsupported capabilities | Retrying cannot succeed. |
| Any failure after a streamed event was sent to the client | The client already has output. |
| Deadline expiry, client disconnect | The request is over. |

For streams, the first upstream event is part of the attempt: a failure before it
(such as a 503 status or a connection failure) can be retried, and the client sees
only the successful attempt's output.

## Duplicate execution and charges

Provider inference APIs are not idempotent and offer no way to cancel or
deduplicate a request. Retries are therefore limited to failures where the request
did not run, but this rests on two assumptions:

- a provider that returns an error status has not generated, or charged for, a
  completion;
- a request whose connection was never established was not received.

If a provider charges for requests that it then answers with 5xx, a retry can be
charged twice. The gateway never retries a request that may still be running.
Retries are not exactly-once delivery: they reduce failures without guaranteeing a
single upstream execution.

Each attempt is charged against the key's token limit separately (see
[admission-control.md](admission-control.md#settlement)). Attempts that certainly
did not run are charged nothing; attempts with unknown usage are charged a full
reservation.

## Circuit breakers

Each provider has a circuit breaker in each gateway process:

- **Closed.** Requests pass. Provider failures (5xx, connection failures, timeouts,
  malformed or interrupted responses) are counted. `failure_threshold` failures
  within `failure_window_seconds` open the breaker. Rate limiting (429), client
  errors (4xx), provider configuration errors, cancellations, and deadline expiry
  are not counted.
- **Open.** Requests to the provider fail immediately with 502
  `provider_unavailable` / `provider_circuit_open`, without contacting it.
- **Half-open.** After `recovery_seconds`, up to `half_open_max_probes` requests at
  a time are let through. A successful probe closes the breaker and clears its
  failure history; a failed probe reopens it for another recovery period. A probe
  that ends for an unrelated reason (client cancellation) frees its slot without
  deciding.

A stream counts as a successful attempt once its first event arrives; a failure
later in the stream is still counted as a provider failure.

Breaker state is not shared between gateway instances. Each process opens and
closes its breakers based on the traffic it sees, so with several replicas some may
still be sending requests to a provider that others have stopped calling.

`gateway_circuit_breaker_transitions_total{provider,state}` and the
`circuit breaker state changed` log event record transitions;
`gateway_provider_attempts_total{provider,outcome="circuit_open"}` counts refused
attempts.

## Metrics

| Metric | Labels |
| --- | --- |
| `gateway_provider_attempts_total` | `provider`, `outcome` (`success`, error type, `circuit_open`) |
| `gateway_provider_attempt_duration_seconds` | `provider` |
| `gateway_provider_retries_total` | `provider` |
| `gateway_circuit_breaker_transitions_total` | `provider`, `state` |
| `gateway_deadline_exceeded_total` | `stream` |
