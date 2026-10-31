# Architecture

## Components

| Module | Responsibility |
| --- | --- |
| `gateway.main` | Builds the FastAPI application; owns startup and shutdown. |
| `gateway.api.middleware` | Request ID, access log, conversion of unhandled exceptions to 500. |
| `gateway.api.dependencies` | Authentication, bounded body reading, request validation. |
| `gateway.api.routes` | `/v1/chat/completions` and the health endpoints. |
| `gateway.api.streaming`, `gateway.api.disconnect` | SSE encoding and response, disconnect detection. |
| `gateway.api.encoding`, `gateway.api.errors` | OpenAI-format response and error bodies. |
| `gateway.services.completion` | Alias resolution, provider dispatch, completion log event. |
| `gateway.providers.*` | Provider interface, adapters, HTTP client setup, upstream error mapping, SSE parsing. |
| `gateway.core.*` | Configuration, logging, authentication. |
| `gateway.schemas.completions` | The accepted request schema. |

Dependencies point inwards: `api` uses `services`, which uses `providers`; all of
them use `core`, `schemas`, and `errors`. Providers know nothing about HTTP
requests from clients, and the service knows nothing about wire formats.

## Request lifecycle

1. **Context.** `RequestContextMiddleware` accepts a well-formed client
   `X-Request-ID` or generates one, stores it in a context variable for logging,
   and adds it to the response headers.
2. **Authentication.** The Bearer token is checked before the body is read. Missing
   or invalid credentials return 401 without reading the body.
3. **Body.** The body must be `application/json`. It is read incrementally and
   rejected with 413 once it exceeds `GATEWAY_MAX_REQUEST_BYTES`. A declared
   `Content-Length` over the limit is rejected without reading.
4. **Validation.** The body is parsed strictly into `ChatCompletionRequest`. The
   first validation error is returned with its parameter path.
5. **Resolution.** `CompletionService` maps the alias to a provider and upstream
   model and builds a `ProviderRequest`, applying the alias's `default_max_tokens`
   if the client did not set `max_tokens`.
6. **Translation.** The adapter checks that it can represent the request and
   raises `unsupported_capability` or `invalid_request` before any network call if
   not. It then serializes the request in its provider's format.
7. **Upstream call.** The provider's shared `httpx.AsyncClient` sends the request.
   Only configured headers are sent; nothing from the client request is forwarded
   except the validated body content.
8. **Normalization.** The adapter validates the upstream response and converts it
   to a `Completion` or a sequence of `TextDelta` events ending in `StreamEnd`.
9. **Response.** The route encodes the result as an OpenAI-format JSON body or SSE
   stream.
10. **Logging.** The service logs one `completion finished` event (outcome, alias,
    provider, upstream model, duration, finish reason, token usage, and error
    category if any). The middleware logs one `request completed` event (method,
    path, status, duration, principal, error category).

## Provider adapter boundary

```python
class Provider(Protocol):
    @property
    def name(self) -> str: ...
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
`pool_timeout_seconds` for a free connection. Concurrency is otherwise unbounded:
the gateway does not queue or limit requests in Phase 1.

**Cancellation.** ASGI servers do not cancel a handler when its client
disconnects; they deliver an `http.disconnect` message through `receive()`. The
gateway races the upstream work against that message (`run_until_disconnect`) and
cancels the work if the client leaves first. Cancellation propagates through the
service and adapter as `asyncio.CancelledError`; `async with` blocks around the
upstream response close it, which closes the connection because the body was not
fully read. Disconnected non-streaming requests are logged with status 499.

**Shutdown.** On SIGTERM uvicorn stops accepting connections and waits up to
`--timeout-graceful-shutdown` seconds (30 in the container image) for in-flight
requests, including streams. Requests still running after that are cancelled, which
closes their upstream connections. The lifespan handler then closes the provider
clients.

## Streaming data flow

```text
upstream bytes ─► iter_sse ─► adapter _parse_stream ─► CompletionService.stream
   ─► encode_stream ─► EventStreamResponse ─► ASGI send
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
pipeline does not change.

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
| Unexpected exception in the gateway | `internal_error` | 500 |

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
`pydantic.SecretStr` and never appear in `repr` or validation errors. See
[configuration.md](configuration.md).

## Security controls

- Upstream URLs come only from configuration. Redirects are not followed, and
  proxy and `.netrc` settings from the environment are ignored, so no request can
  direct the gateway to another host.
- Upstream requests are built from scratch: client headers are never forwarded, so
  they cannot override upstream authorization or transport headers.
- Credentials are never logged. httpx and httpcore loggers are held at WARNING;
  request bodies, prompts, and completions are never logged.
- Requests are bounded by body size, message count (512), stop sequence count (4),
  and field ranges.
- Production mode requires 32-character gateway keys and HTTPS upstreams unless a
  provider explicitly opts out.
- The container runs as an unprivileged user; Compose adds a read-only root
  filesystem and drops all capabilities.

## Trade-offs

- **Manual body parsing** gives exact control over size limits and error mapping,
  at the cost of FastAPI's generated OpenAPI schema; the interactive docs are
  disabled rather than shown incomplete.
- **Delaying stream headers until the first event** gives accurate error statuses
  for most failures, at the cost of a later first byte.
- **Strict rejection of unsupported fields** breaks clients that send fields the
  gateway does not implement, instead of returning responses generated without
  them.
- **No retries.** Retrying belongs with failover and budgets in a later phase,
  where it can be bounded and observed.
- **One process, no shared state.** Nothing is shared between instances, so the
  gateway scales horizontally without coordination, but limits and budgets cannot
  be enforced across instances yet.
