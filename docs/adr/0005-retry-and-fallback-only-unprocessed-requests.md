# 0005: Retry and fall back only when the provider did not run the request

## Status

Accepted

## Context

Provider inference APIs are not idempotent and offer no request deduplication. A
request that times out after it was sent may still run to completion, and be
charged, on the provider's side. Retrying or redirecting such a request can produce
two executions and two charges, and for streams, output from two generations in one
response.

## Decision

- Each provider error records whether the provider may have processed the request.
  It may not have only when the connection was never established or the provider
  explicitly rejected it with a client/account status. All 5xx statuses remain
  uncertain because inference may already have executed.
- Only those failures are retried, and only if transient (429, connection establishment failures), with bounded exponential backoff and jitter,
  within the request deadline, honouring `Retry-After` up to a cap.
- After retries, the same class of failures (plus provider configuration errors and
  open circuit breakers) moves the request to the alias's next configured fallback.
  Fallbacks receive the same request with only the model changed; a fallback that
  cannot run it as given is skipped.
- For streams, the first upstream event belongs to the attempt; after it, failures
  end the stream with an error event.
- Each attempt is charged against token limits separately: reported usage, a full
  reservation when unknown, nothing when it certainly did not run.
- Circuit breakers are per process and per provider.

## Alternatives considered

- **Retry timeouts as well.** Recovers from more transient failures, but risks
  duplicate execution and charges on every slow response.
- **Fall back after partial stream output.** Completes more streams, but the client
  would receive text from two different generations.
- **Shared circuit breaker state in Redis.** Replicas would agree on provider
  health, at the cost of Redis traffic on every attempt and a dependency on Redis
  for routing even without admission control.

## Consequences

- Read timeouts and dropped connections are returned to the client rather than
  retried.
- The policy depends on providers not charging for explicit client rejections; it does not provide exactly-once execution.
- Replicas may disagree about whether a provider's circuit is open.
