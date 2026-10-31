# Provider resilience

How the gateway behaves when providers are slow, failing, or unreachable: request
deadlines, retries, fallback, and circuit breakers.

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

## Retries and fallback

Connection establishment failures, connect timeouts, and pool timeouts can be
retried because dispatch did not occur. HTTP 429 is retryable under the assumption
that the provider rejected the request before generation. Explicit client/account
rejections (400, 401, 402, 403, 404, 413, 422) do not incur token charges; only
account/configuration failures can trigger a configured fallback.

**All HTTP 5xx statuses are uncertain execution**, including 500, 502, 503, 504,
and 529. A server or proxy can fail after inference executed. They are not retried
or redirected, and retain the full token reservation when usage is unknown. The
same applies to read/write failures, malformed responses, and stream errors.
There is no exactly-once execution guarantee. The 429 and client-rejection policy
still assumes the provider implements those rejection semantics honestly.

`max_attempts_per_provider` includes the first attempt (default 2, at most 5).
Backoff doubles from `retry_initial_backoff_seconds` to
`retry_max_backoff_seconds`, reduced by a random fraction of up to
`retry_jitter_ratio`. Delay-seconds `Retry-After` is respected up to
`max_retry_after_seconds`; longer delays stop retries. Every delay and attempt
shares the original deadline. No new attempt begins after it expires.

`retry_on_status` defaults to `[429]`. Existing configurations listing 5xx remain
loadable, but cannot override the execution-uncertainty safety check. There is no
setting that enables automatic replay of uncertain inference calls.

### Configured fallback

```toml
[models.fast]
provider = "primary"
model = "primary-model-id"
fallbacks = [{ provider = "secondary", model = "secondary-model-id" }]
```

Candidates reference configured providers, not aliases; chains cannot form cycles.
Up to three fallbacks are allowed, with no duplicate provider/model pairs. They
receive the same request with only the upstream model changed. Adapters validate
candidates before admission; an incompatible fallback is skipped rather than
receiving modified generation parameters. Each provider uses its own credential.

A connection establishment failure, explicit rate rejection, provider configuration
error, or an already-open breaker can move to the next candidate. The request
that first opens a circuit with an uncertain failure ends with that error; later
requests can skip the open provider. Usage is accounted for each attempt.

For streams, fallback and retry stop at the first upstream event. Partial output
is never joined to another generation. A later failure emits an error event and
withholds `[DONE]`.

## Circuit breakers

A streamed probe holds its permit until the stream ends. Its first event does not
declare recovery. Permits belong to one breaker generation; stale completions
cannot close or consume probe capacity from a later recovery cycle. Releases are
idempotent.


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

A stream counts as successful only after full completion. Attempt outcomes and
duration metrics are recorded at completion, failure, or cancellation; streamed
durations include client backpressure. The first event only closes the retry and
fallback window.

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
| `gateway_provider_fallbacks_total` | `from_provider`, `to_provider` |
| `gateway_deadline_exceeded_total` | `stream` |

### Deadline termination

Each SSE read and write uses the remaining request deadline. There is no competing
whole-response timer. When the deadline expires, the gateway allows at most 100 ms
to deliver a terminal error event to a reading client; a blocked connection is
closed. Cleanup may additionally wait up to the configured Redis operation timeout.
The success sentinel is sent only after provider exhaustion and settlement. Stalled
request-body uploads are also interrupted by the same request deadline.

If the deadline expires after prefetch but before response headers, the response
returns a JSON 504 and explicitly closes the prefetched provider stream. Closing
an encoder that has never started is insufficient: Python does not execute its
`finally` block in that case.
