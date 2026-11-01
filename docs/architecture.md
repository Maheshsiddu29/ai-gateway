# Architecture

## Components

| Module | Responsibility |
| --- | --- |
| `gateway.main` | Builds the FastAPI application; owns startup and shutdown. |
| `gateway.api.middleware` | Request ID, access log, HTTP metrics, conversion of unhandled exceptions to 500. |
| `gateway.api.dependencies` | Authentication, bounded body reading, request validation. |
| `gateway.api.routes` | `/v1/chat/completions`, authenticated usage and budget reads, health, and `/metrics`. |
| `gateway.api.streaming`, `gateway.api.disconnect` | SSE encoding and response, disconnect detection. |
| `gateway.api.encoding`, `gateway.api.errors` | OpenAI-format response and error bodies. |
| `gateway.services.completion` | Request planning (alias, fallbacks or routing, output ceiling, validation), admission and settlement, completion log event. |
| `gateway.routing.*` | Capability registry and routing configuration, cost estimates, evaluation evidence snapshot, and the routing decision. |
| `gateway.evaluation.*` | Offline evaluation: partitioned datasets, scorers, runner, live-run plans and cost ceiling, progress journal, simulated provider, PostgreSQL recording, and the `python -m gateway.evaluation` CLI. The request path loads only datasets, scorers, and statistics (through `gateway.routing.evidence`); a test enforces that the runner, plan, and CLI stay unloaded. |
| `gateway.services.execution` | Provider attempts: retries, circuit breakers, fallback, per-attempt usage. |
| `gateway.admission.*` | Per-key limits, token estimation, the Redis scripts, failure policy, and usage accounting. |
| `gateway.cost.*` | Versioned pricing, PostgreSQL ledger, per-attempt reservations and spending budgets. |
| `gateway.resilience.*` | Request deadlines, retry policy, circuit breakers. |
| `gateway.providers.*` | Provider interface, adapters, HTTP client setup, upstream error mapping, SSE parsing. |
| `gateway.core.*` | Configuration, logging, metrics, Redis client, authentication. |
| `gateway.schemas.completions` | The accepted request schema. |

Dependencies point inwards: `api` uses `services`, which uses `admission`,
`resilience`, `routing`, and `providers`; all of them use `core`, `schemas`, and `errors`. Providers know nothing about HTTP
requests from clients, and the service knows nothing about wire formats.

## Request lifecycle

Authentication -> validation -> model resolution -> admission -> provider attempts
-> retry or fallback decision -> response -> reconciliation.

1. **Context.** `RequestContextMiddleware` accepts a well-formed client
   `X-Request-ID` or generates one, stores it in a context variable for logging,
   and adds it to the response headers.
2. **Deadline.** A `Deadline` for `request_deadline_seconds` starts before any
   other processing, measured on the monotonic clock.
3. **Authentication.** The Bearer token is checked before the body is read. Missing
   or invalid credentials return 401 without reading the body.
4. **Body.** The body must be `application/json`. It is read incrementally and
   rejected with 413 once it exceeds `GATEWAY_MAX_REQUEST_BYTES`. A declared
   `Content-Length` over the limit is rejected without reading.
5. **Validation.** The body is parsed strictly into `ChatCompletionRequest`. The
   first validation error is returned with its parameter path.
6. **Planning.** `CompletionService` maps the alias to its primary provider and
   fallbacks, applies `max_output_tokens` and `default_max_tokens`, and builds one
   `ProviderRequest` per candidate. Each adapter validates its request without I/O;
   the primary's rejection is returned to the client, and an incompatible fallback
   is dropped from the plan. For an alias with a dynamic routing policy, the
   router filters and ranks the alias's candidates instead and returns the
   ordered attempt chain, without I/O (see [routing.md](routing.md)).
7. **Admission.** If admission control is enabled, one Redis script checks the
   key's request, token, and concurrency limits and either commits all of them or
   none. The token reservation is the estimated input plus the output allowance.
   Rejection returns 429; a Redis failure returns 503.
8. **Provider attempts.** `ProviderExecutor` tries the candidates in order. Each
   attempt passes through the provider's circuit breaker. When cost accounting is
   enabled, it obtains a PostgreSQL budget reservation and commits a dispatch mark
   before calling the provider. The provider call runs within the
   remaining deadline. The adapter translates the request, the provider's
   `httpx.AsyncClient` sends it with only configured headers, and the adapter
   normalizes the response into a `Completion` or a stream of `TextDelta` events
   ending in `StreamEnd`.
9. **Retry or fallback.** A failure where the provider certainly did not run the
   request is retried with backoff, then passed to the next candidate. Any other
   failure ends the request. See [resilience.md](resilience.md).
10. **Response.** The route encodes the result as an OpenAI-format JSON body or SSE
    stream.
11. **Reconciliation.** Whatever the outcome, including cancellation and deadline
    expiry, each PostgreSQL cost attempt is settled with observed usage or a
    conservative liability. The Redis lease is released and its token reservation
    is replaced by usage charged for every attempt. See
    [admission-control.md](admission-control.md) and
    [cost-accounting.md](cost-accounting.md).
12. **Logging.** The service logs one `completion finished` event (outcome, alias,
    serving provider, attempts, duration, finish reason, token usage and
    reservation, error category). The middleware logs one `request completed` event
    (method, path, status, duration, principal, error category).

### Non-streaming path

Planning, admission, the provider attempts, and settlement all run inside the route
handler, raced against the client's disconnect (see Cancellation below). The
response is sent after settlement has finished.

### Streaming path

The route runs planning, admission, and the provider attempts up to and including
the first upstream event before it returns the response, so failures before any
output still produce an HTTP error status. The remaining events are read while the
response is being sent. The admission reservation is settled when the stream ends:
after `StreamEnd`, after a mid-stream error, when the client disconnects, or when
the deadline expires.

## Provider adapter boundary

```python
class Provider(Protocol):
    @property
    def name(self) -> str: ...
    def validate(self, request: ProviderRequest) -> None: ...
    async def complete(self, request: ProviderRequest) -> Completion: ...
    def stream(self, request: ProviderRequest) -> AsyncGenerator[StreamEvent]: ...
    async def aclose(self) -> None: ...
```

Everything provider-specific stays inside the adapter: payload construction,
headers, endpoint paths, response models, stream event parsing, stop-reason
mapping, and capability checks. Adapters raise only `GatewayError` subclasses.
Shared HTTP concerns (client construction, status and transport error mapping)
live in `gateway.providers.http`.

Adapters receive a `ProviderRequest` rather than the API schema, so they see the
resolved upstream model and effective `max_tokens`, and never the alias.

**OpenAI-compatible.** Sends only supported fields that are set, so servers
implementing a subset of the API are not sent options they may reject.
`stream_options` is sent only when the client asked for usage. A stream ends at
`data: [DONE]`; a connection closed after a `finish_reason` is also accepted,
because some compatible servers omit `[DONE]`.

**Anthropic.** Moves leading system messages into the top-level `system` field,
renames `stop` to `stop_sequences`, and requires `max_tokens`. Sampling
parameters are validated against the configured API version. Responses are
normalized by joining text blocks and skipping `thinking` blocks; other block types
are treated as malformed because the gateway cannot represent them. Usage combines
`input_tokens` with cache creation and cache read tokens. A stream ends at
`message_stop`.

## Async execution model

The gateway runs on a single asyncio event loop under uvicorn. All upstream I/O is
asynchronous; there are no threads or blocking calls in the request path.

Each provider has one `httpx.AsyncClient`, created at startup and closed at
shutdown, with a connection pool bounded by `max_connections`. Requests wait up to
`pool_timeout_seconds` for a free connection. The gateway does not queue requests:
with admission control enabled, concurrency per key is limited by leases in Redis;
without it, concurrency is bounded only by the connection pools. Redis connections
come from one bounded pool per process.

Background work is limited to settlement tasks, capped per process at
`redis_max_connections`. Each is bounded by the Redis operation timeout and
awaited at shutdown; overloaded cleanup retains its reservation until expiry. There are no queues or periodic tasks.

**Cancellation.** ASGI servers do not cancel a handler when its client
disconnects; they deliver an `http.disconnect` message through `receive()`. The
gateway races the upstream work against that message (`run_until_disconnect`) and
cancels the work if the client leaves first. Cancellation propagates through the
service and adapter as `asyncio.CancelledError`; `async with` blocks around the
upstream response close it, which closes the connection because the body was not
fully read. The admission reservation is then settled in a task shielded from the
cancellation, charging the full reservation because the provider may have been
running the request. Disconnected non-streaming requests are logged with status 499.

**Shutdown.** On SIGTERM uvicorn stops accepting connections and waits up to
`--timeout-graceful-shutdown` seconds (30 in the container image) for in-flight
requests, including streams. Requests still running after that are cancelled, which
closes their upstream connections and settles their reservations. The lifespan
handler then waits for pending settlements and closes the Redis and provider
clients.

## Streaming data flow

```text
upstream bytes ─► iter_sse ─► adapter _parse_stream ─► ProviderExecutor.stream
   ─► CompletionService.stream ─► encode_stream ─► EventStreamResponse ─► ASGI send
```

Every stage is a pull-based async generator with no buffer between stages. The
next upstream chunk is read only after the previous chunk has been handed to the
server's `send`, and uvicorn's `send` waits while the socket's write buffer is
full. A slow client therefore slows reads from the provider instead of growing
memory; TCP flow control carries the backpressure to the provider.

**Headers are delayed until the first event.** The route reads the first event
from the service before returning the response. Errors that happen before any
output (authentication, rate limiting, timeouts, an Anthropic `error` event before
the first text) therefore return a normal JSON error with the right status code.
The trade-off is that response headers are sent at the time of the first token,
not when the upstream connection opens.

**Errors after output has started.** The status code has been sent and cannot
change. The encoder emits a final `data: {"error": {...}}` event with the same
error object used in JSON responses and does not send `[DONE]`. The upstream
response is closed. The gateway does not retry, resume, or switch providers after
partial output, because the client has already received text that a different
generation would contradict. ADR 0002 describes this in more detail.

**SSE parsing.** Upstream streams are parsed by `gateway.providers.sse`, which
splits lines only on CR, LF, and CRLF as the SSE format specifies. httpx's
`aiter_lines` also splits on characters such as U+2028, which can appear unescaped
in JSON strings and would corrupt events. A property-based test checks parsing
across arbitrary chunk boundaries.

**Response class.** `EventStreamResponse` watches for client disconnects on every
ASGI server version and closes the body generator before returning. Starlette's
`StreamingResponse` only watches on servers reporting ASGI spec versions before
2.4, and leaves an abandoned generator to be finalized by the garbage collector.
Closing the generator closes the service and adapter generators in turn, which
releases the upstream connection immediately.

## Authentication boundary

`require_principal` turns the `Authorization` header into a `Principal`, which is
passed to the service and recorded in logs. Nothing downstream of that dependency
reads credentials. `StaticKeyAuthenticator` implements the `Authenticator`
protocol by comparing SHA-256 digests with `hmac.compare_digest` against every
configured key, so timing does not depend on which key matched. A principal's ID is
a truncated digest of its key and is safe to log.

Replacing the static list with a tenant-aware store means providing another
`Authenticator` that returns a `Principal` carrying tenant information; the request
pipeline does not change. Phase 3 uses static configuration to map existing key
fingerprints to budget teams and explicit team analytics readers.

## Failure handling

| Condition | Error type | Status |
| --- | --- | --- |
| Missing or invalid gateway key | `authentication_error` | 401 |
| Invalid JSON, schema violation, unknown field | `invalid_request` | 400 |
| Unknown alias | `invalid_request` (`model_not_found`) | 404 |
| Body over the size limit | `invalid_request` (`request_too_large`) | 413 |
| Field or value the gateway or adapter cannot support | `unsupported_capability` | 400 |
| Upstream 400, 413, 422 | `invalid_request` (`provider_rejected_request`) | 400 |
| Upstream 401, 402, 403, 404 | `provider_configuration_error` | 502 |
| Upstream 429 | `provider_rate_limited` | 429 |
| Upstream 408, 504; connect, read, write, or pool timeout | `provider_timeout` | 504 |
| Upstream 5xx (including 529) or other unexpected status, connection failure, malformed response, incomplete stream | `provider_unavailable` | 502 |
| Circuit breaker open for every remaining candidate | `provider_unavailable` (`provider_circuit_open`) | 502 |
| Request, token, or concurrency limit reached | `rate_limited` | 429 |
| Token reservation larger than the key's token bucket | `invalid_request` (`token_reservation_exceeds_limit`) | 400 |
| Redis unavailable while admission control is enabled | `admission_unavailable` | 503 |
| Request deadline expired | `deadline_exceeded` | 504 |
| Unexpected exception in the gateway | `internal_error` | 500 |

These statuses describe the last attempt. Before an error is returned, transient
failures may have been retried and later candidates tried; see
[resilience.md](resilience.md).

Client messages are fixed strings written by the gateway. Upstream response bodies
are never forwarded, because they can echo request content; only the upstream error
`type` is kept, in logs. `Retry-After` is forwarded on 429 when the upstream sent a
number of seconds. Unhandled exceptions are logged with a traceback and returned as
a generic 500.

## Configuration model

Configuration is loaded and validated once, before the application is created:
environment variables into `Settings`, then the TOML file into `GatewayConfig`,
then each provider's `api_key_env` is resolved from the environment. Any problem
stops startup with a message naming the setting. Secrets are held as
`pydantic.SecretStr` and never appear in `repr` or validation errors. Admission
limits and resilience settings live in the same file; the Redis URL, which may hold
a password, comes from `GATEWAY_REDIS_URL`. See [configuration.md](configuration.md).

## Security controls

- Upstream URLs come only from configuration. Redirects are not followed, and
  proxy and `.netrc` settings from the environment are ignored, so no request can
  direct the gateway to another host.
- Upstream requests are built from scratch: client headers are never forwarded, so
  they cannot override upstream authorization or transport headers.
- Production Redis TCP uses verified TLS. `/metrics` requires a separate metrics
  credential (`GATEWAY_METRICS_API_KEYS`); gateway API keys cannot read it.
- Credentials are never logged. httpx and httpcore loggers are held at WARNING;
  request bodies, prompts, and completions are never logged.
- Requests are bounded by body size, message count (512), stop sequence count (4),
  and field ranges.
- Production mode requires 32-character gateway keys and HTTPS upstreams unless a
  provider explicitly opts out.
- The container runs as an unprivileged user; Compose adds a read-only root
  filesystem and drops all capabilities, and keeps Redis on an internal network.
- Admission state is keyed by a digest-derived principal ID from the authenticated
  key; callers cannot choose it, unauthenticated requests create no Redis state,
  and reservation IDs are generated by the gateway and never accepted as input.
- Admission fails closed: without a decision from Redis, requests are refused.
- Each provider has its own client and credential; fallback never sends one
  provider's credential to another.
- Retry amplification is bounded: at most `max_attempts_per_provider` (5 or fewer)
  attempts for each of at most four candidates, all within one deadline.
- Readiness results are reused for one second, so unauthenticated probes cannot
  generate unbounded Redis traffic.

## Trade-offs

- **Manual body parsing** gives exact control over size limits and error mapping,
  at the cost of FastAPI's generated OpenAPI schema; the interactive docs are
  disabled rather than shown incomplete.
- **Delaying stream headers until the first event** gives accurate error statuses
  for most failures, at the cost of a later first byte.
- **Strict rejection of unsupported fields** breaks clients that send fields the
  gateway does not implement, instead of returning responses generated without
  them.
- **Retry only what certainly did not run.** This gives up recovery from read
  timeouts and dropped connections in exchange for not running a request twice;
  see [ADR 0005](adr/0005-retry-and-fallback-only-unprocessed-requests.md).
- **One Redis script per admission decision** keeps admission atomic and fast, at
  the cost of making Redis a required dependency for limits; see
  [ADR 0004](adr/0004-atomic-admission-in-redis.md).
- **Leases sized to the deadline** need no renewal, but a crashed gateway's
  concurrency slots stay held until the deadline plus grace period has passed.
- **Process-local circuit breakers** need no coordination, but replicas detect
  provider outages independently.

## Phase 3 financial accounting

When cost accounting is enabled, `gateway.cost` adds a PostgreSQL reservation and ledger transaction around each provider attempt. Redis admission still occurs first. The executor reserves and marks each attempt dispatched before provider I/O, then settles its observed charge or unresolved liability after completion, failure, or cancellation. A retry or fallback performs this sequence again with a new attempt UUID. Readiness probes PostgreSQL; a background recovery task resolves expired reservations. `gateway.api.routes.usage` reads authenticated, bounded spending views. See [cost accounting](cost-accounting.md) and [budget enforcement](budget-enforcement.md) for transaction and period semantics.
