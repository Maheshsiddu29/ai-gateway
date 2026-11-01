# AI Gateway

An HTTP service that exposes one OpenAI-compatible chat completion endpoint and
forwards requests to configured LLM providers. Clients call a model alias such as
`fast`; the gateway maps the alias to a provider and an upstream model, translates
the request, and normalizes the response.

The gateway enforces per-key request, token, and concurrency limits shared across
instances through Redis, bounds every request with an end-to-end deadline, and
retries or falls back between providers only when a failed provider certainly did
not run the request. Optional PostgreSQL spending budgets account for each upstream
attempt. An alias can route each request to the cheapest model that supports it
and, optionally, has passed an offline evaluation on the alias's task. Caching is
not implemented.

## Current capabilities

- `POST /v1/chat/completions` accepting a documented subset of the OpenAI Chat
  Completions request, with non-streaming responses and Server-Sent Events
  streaming.
- Provider adapters for OpenAI-compatible HTTP APIs (OpenAI and self-hosted servers
  with the same API) and the Anthropic Messages API.
- Model aliases defined in a configuration file, validated at startup.
- Gateway API-key authentication with Bearer tokens.
- Strict request validation: unsupported parameters are rejected, not ignored.
- Stable error categories in an OpenAI-style error envelope.
- Request IDs on every response and log line, JSON logs, and a per-completion log
  event with alias, provider, outcome, duration, and token usage when the provider
  reports it.
- Client disconnects cancel the upstream request, for streaming and non-streaming
  calls.
- Distributed admission control (optional): requests per minute, tokens per minute
  (by reservation and settlement), and concurrent requests per gateway key,
  enforced atomically in Redis for all instances, failing closed when Redis is
  unavailable.
- PostgreSQL usage ledger with versioned per-model prices, exact micro-USD charges,
  daily and monthly key/team budgets, and authenticated usage APIs when cost accounting
  is enabled.
- An end-to-end deadline per request, covering admission, all provider attempts,
  and the whole stream.
- Bounded retries with exponential backoff, per-provider circuit breakers, and
  configured fallback providers per alias.
- Policy-driven routing per alias (optional): `fixed`, `lowest_cost`, or
  `quality_constrained`. It uses a model capability registry, cost estimates
  from the versioned prices, circuit-breaker state, and recorded evaluation
  evidence. Each routed attempt keeps its own budget reservation, retries, and
  fallback.
- Offline evaluation tooling: versioned JSONL datasets, deterministic scorers, a
  CLI that calls real providers only when explicitly asked to, PostgreSQL storage
  of results, and an offline comparison of routing policies.
- Prometheus metrics at `GET /metrics` (gateway Bearer key required); `GET /health/live`
  and `GET /health/ready` (which checks required Redis and PostgreSQL dependencies).

## Architecture

```text
client ─► RequestContextMiddleware   request ID, access log, metrics, last-resort 500
           └─► /v1/chat/completions   deadline starts
                 auth ─► bounded body read ─► schema validation
                 └─► CompletionService
                       plan: alias ─► primary + fallbacks, or Router ─► ranked candidates
                             (capabilities, price, breaker state, evaluation evidence)
                       admit ─► AdmissionController ─► Redis script (all limits at once)
                       └─► ProviderExecutor   retries, circuit breakers, fallback
                             ├─► PostgreSQL reserve + dispatch per attempt
                             └─► Provider adapter ─► httpx.AsyncClient (per provider)
                       settle ─► PostgreSQL cost ledger + Redis token lease
```

The API layer speaks the OpenAI wire format. The service layer is independent of
HTTP. Each adapter owns its provider's serialization and returns provider-neutral
results (`Completion`, or a stream of `TextDelta` events ending in `StreamEnd`),
which the API layer encodes back into OpenAI-format responses.

See [docs/architecture.md](docs/architecture.md) for the request lifecycle,
[docs/cost-accounting.md](docs/cost-accounting.md) for charge semantics,
[docs/budget-enforcement.md](docs/budget-enforcement.md) for spending admission,
[docs/routing.md](docs/routing.md) and [docs/evaluation.md](docs/evaluation.md) for
routing and offline evaluation,
[docs/admission-control.md](docs/admission-control.md) and
[docs/resilience.md](docs/resilience.md) for limits and failure handling, and
[docs/adr/](docs/adr/) for the main design decisions.

## Requirements

- Python 3.13. The code uses syntax from Python 3.12 (PEP 695 type aliases and
  generic functions); 3.13 is the version tested and used in the container image.
- [uv](https://docs.astral.sh/uv/) 0.13 or later, for the locked dependency set in
  `uv.lock`.
- Redis 7 or later when admission control is enabled. The tests start their own
  `redis-server` processes, so the binary must be installed to run them.
- PostgreSQL 16 for cost accounting. Apply Alembic migrations before gateway startup.
- Docker with Compose, optionally, for the container setup.

## Local setup

```sh
uv sync                     # creates .venv from uv.lock, including dev tools
cp .env.example .env        # then set GATEWAY_API_KEYS
```

### Run with the mock provider

`tools/mock_upstream.py` implements enough of the OpenAI and Anthropic APIs to
exercise both adapters without vendor accounts. With Docker, the stack runs the
gateway, the mock, Redis, and PostgreSQL, with admission and cost accounting enabled
(`config/gateway.compose.toml`):

```sh
export GATEWAY_API_KEYS=$(openssl rand -hex 32)
docker compose up --build                    # gateway on 127.0.0.1:8000
docker compose --profile multi up --build    # and a second gateway on 127.0.0.1:8001
```

Without Docker, run the mock and the gateway in two terminals.
`config/gateway.mock.toml` leaves admission control disabled, so Redis is not
needed (the gateway logs a warning that no limits are enforced). It addresses the
mock by its Compose service name, so point it at localhost first:

```sh
sed 's#mock-upstream:9000#127.0.0.1:9000#' config/gateway.mock.toml > config/gateway.toml

uv run uvicorn mock_upstream:app --app-dir tools --port 9000
uv run --env-file .env uvicorn gateway.main:create_app --factory --port 8000 --no-access-log
```

### Run against real providers

Copy `config/gateway.example.toml` to `config/gateway.toml`, replace the placeholder
model identifiers, and set the provider keys it references (`OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`) in `.env`. The container image expects the file at
`/etc/gateway/gateway.toml`:

If you enable `[cost]`, configure prices, set `GATEWAY_DATABASE_URL`, and apply
`GATEWAY_DATABASE_URL=... uv run alembic upgrade head` before starting the gateway.

```sh
docker build -t ai-gateway .
docker run --rm -p 8000:8000 --env-file .env \
  -v "$PWD/config/gateway.toml:/etc/gateway/gateway.toml:ro" ai-gateway
```

## Configuration

Deployment settings and credentials come from `GATEWAY_*` environment variables.
Providers and model aliases come from a TOML file named by `GATEWAY_CONFIG_FILE`.
The file contains no secrets: each provider names the environment variable that
holds its key.

```toml
[providers.anthropic]
type = "anthropic"
base_url = "https://api.anthropic.com"
api_key_env = "ANTHROPIC_API_KEY"

[models.balanced]
provider = "anthropic"
model = "your-anthropic-model-id"
default_max_tokens = 1024
```

Limits, deadlines, retries, and fallbacks are configured in the same file:

```toml
[models.balanced]
provider = "anthropic"
model = "your-anthropic-model-id"
max_output_tokens = 4096
fallbacks = [{ provider = "openai", model = "your-openai-model-id" }]

[resilience]
request_deadline_seconds = 300
max_attempts_per_provider = 2

[admission]
enabled = true            # requires GATEWAY_REDIS_URL

[admission.default_limits]
requests_per_minute = 60
tokens_per_minute = 100000
max_concurrent_requests = 8
```

When `[cost] enabled = true`, set `GATEWAY_DATABASE_URL` and configure a price for
every primary and fallback provider/model. For example:

```toml
[cost]
enabled = true

[cost.default_key_limits]
daily_micro_usd = 1000000   # $1.00
monthly_micro_usd = 10000000 # $10.00

[[cost.pricing]]
provider = "anthropic"
model = "your-anthropic-model-id"
version = "internal-2026-10"
effective_at = 2026-10-01T00:00:00Z
input_per_million_usd = "0.50"
output_per_million_usd = "2.00"
```

These are illustrative rates, not vendor prices. See
[docs/configuration.md](docs/configuration.md) for team mapping and reader grants.

## Routing

With `[routing] enabled = true` and cost accounting on, an alias can choose among
candidate models per request. Every routed model needs a registry entry with its
limits and supported request features, and a price:

```toml
[models.classify]
provider = "anthropic"
model = "large-model-id"
max_output_tokens = 16

[models.classify.routing]
policy = "quality_constrained"    # or "lowest_cost"; "fixed" is the default
candidates = [{ provider = "openai", model = "small-model-id" }]
dataset = "support-ticket-category"
dataset_version = "1"
min_success_rate = 0.9            # against the 95% lower confidence bound
min_samples = 40
max_evidence_age_days = 30

[routing]
enabled = true

[[routing.models]]
provider = "openai"
model = "small-model-id"
context_window_tokens = 128000
max_output_tokens = 16384
capabilities = ["streaming", "stream_usage", "temperature", "top_p", "stop"]
# ... one entry per routed model, and a [[cost.pricing]] entry for each.
```

The router drops candidates for these reasons:

- they lack a feature the request uses;
- the request would not fit their context or output limits;
- their adapter rejects the request;
- they are unpriced;
- their circuit is open;
- (for `quality_constrained`) they have no fresh, large enough evaluation run
  that passes the threshold.

It then picks the lowest maximum cost, with configuration order breaking ties.
The remaining eligible candidates become the fallbacks.

If nothing qualifies, `quality_constrained` uses the alias's primary model by
default, or rejects the request with `on_insufficient_evidence = "reject"`. Each
decision is logged with policy and evidence versions, exclusions, and estimates.
See [docs/routing.md](docs/routing.md).

### Offline evaluation

```sh
# No provider calls; results are marked simulated and never used for routing.
uv run python -m gateway.evaluation run --config config/gateway.toml \
  --dataset evaluations/datasets/support-ticket-category-v1.jsonl \
  --target openai/small-model-id --max-tokens 8 --output results/ \
  --simulate openai/small-model-id=0.9

# Real provider calls (costs money): --live prints a preflight plan and sends nothing
# until the command is repeated with --approve <plan sha256>.
uv run --env-file .env python -m gateway.evaluation run --config config/gateway.toml \
  --dataset evaluations/datasets/support-ticket-category-v1.jsonl \
  --target openai/small-model-id --max-tokens 8 --output results/ \
  --live --max-cost-micro-usd 200000 --max-requests 100

# Record results as routing evidence.
GATEWAY_DATABASE_URL=... uv run python -m gateway.evaluation record results/*.json

# Compare fixed, lowest_cost, and quality_constrained on recorded results.
uv run python tools/routing_experiment.py --config config/gateway.toml --alias classify \
  --dataset evaluations/datasets/support-ticket-category-v1.jsonl --results results/*.json
```

The experiment report shows cost, success rate with a confidence interval,
latency, and routing distribution for each policy. Savings against the
fixed-model baseline are reported only when both policies served the same cases
with recorded usage. Results from simulated runs, or from runs that also served as
evidence (in-sample), are labelled as such. The cost figures are gateway
accounting at configured prices, not invoices. See
[docs/evaluation.md](docs/evaluation.md).

`GATEWAY_ENVIRONMENT` defaults to `production`, which requires gateway keys of at
least 32 characters and HTTPS provider URLs. Every setting is described in
[docs/configuration.md](docs/configuration.md), including changes from Phase 1
configurations.

## API usage

Authenticate with `Authorization: Bearer <gateway key>`. The examples assume
`GATEWAY_API_KEYS` holds a single key.

```sh
curl -s http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEYS" \
  -H "Content-Type: application/json" \
  -d '{"model": "fast", "messages": [{"role": "user", "content": "Explain database indexing."}]}'
```

```json
{
  "id": "chatcmpl-7d25d2d1f7b64c48a2bc2e3ddc9cd430",
  "object": "chat.completion",
  "created": 1793422274,
  "model": "fast",
  "choices": [
    {
      "index": 0,
      "message": {"role": "assistant", "content": "..."},
      "finish_reason": "stop"
    }
  ],
  "usage": {"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10}
}
```

`model` in the response is the alias that was requested. `usage` is present only
when the provider reported token counts.

Streaming:

```sh
curl -sN http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEYS" \
  -H "Content-Type: application/json" \
  -d '{"model": "balanced", "stream": true, "stream_options": {"include_usage": true},
       "messages": [{"role": "user", "content": "Explain database indexing."}]}'
```

Each event is a `data:` line containing a `chat.completion.chunk`. A complete
stream ends with a chunk carrying `finish_reason`, a usage chunk if requested and
reported, and `data: [DONE]`. If the stream fails after output has started, the last
event is `data: {"error": {...}}` and `[DONE]` is not sent.

### Supported request fields

| Field | Notes |
| --- | --- |
| `model` | A configured alias. Unknown aliases return 404 `model_not_found`. |
| `messages` | 1 to 512 messages with `role` `system`, `user`, or `assistant` and string `content`. |
| `stream` | Boolean. |
| `stream_options.include_usage` | Only with `stream: true`. |
| `max_tokens` | Integer from 1 to 1,000,000. Falls back to the alias's `default_max_tokens`. |
| `temperature` | 0 to 2. Provider-specific limits apply (see below). |
| `top_p` | 0 to 1. Provider-specific limits apply. |
| `stop` | A string or a list of 1 to 4 strings. |

Any other field is rejected. Fields that exist in the OpenAI API but are not
implemented (`tools`, `response_format`, `n`, `seed`, `logprobs`, and others) return
`unsupported_capability`; unknown fields return `invalid_request`. Content-part
arrays, images, audio, and the `tool`, `function`, and `developer` roles are not
accepted.

The Anthropic adapter additionally rejects requests it cannot translate faithfully:
system messages after the first user or assistant message, a missing `max_tokens`
with no configured default, and `temperature` or `top_p` on API version
`2026-10-21` (the default), which does not accept them.

### Rate limits

A request over a key's limits is refused before any provider is called:

```text
HTTP/1.1 429 Too Many Requests
retry-after: 4

{"error": {"type": "rate_limited", "code": "tokens_per_minute_exceeded",
           "message": "Token rate limit exceeded.", "param": null,
           "request_id": "9f0c2a6d1b8e4f3a8c7d6e5f4a3b2c1d"}}
```

`code` is `requests_per_minute_exceeded`, `tokens_per_minute_exceeded`, or
`concurrency_limit_exceeded`. `Retry-After` is the time until the bucket has
refilled enough, rounded up to whole seconds; it is omitted for concurrency
rejections, whose wait depends on other requests finishing. A request whose token
reservation can never fit the key's limit gets 400 `token_reservation_exceeds_limit`.

### Spending APIs

With cost accounting enabled, authenticated keys can call `GET /v1/usage`,
`GET /v1/usage/summary`, and `GET /v1/budgets`. Usage queries accept `start`,
`end` (timezone-aware, at most 31 days), `provider`, and `model`; `/v1/usage`
also accepts bounded `limit` and `offset`. A `team_id` query is allowed only for
keys explicitly listed as readers of that configured team. All amounts are integer
micro-USD.

### Errors

```json
{"error": {"type": "provider_rate_limited", "code": "provider_rate_limited",
           "message": "The provider's rate limit was exceeded.", "param": null,
           "request_id": "92cf20ec634041868a49079798c79940"}}
```

| `type` | HTTP status |
| --- | --- |
| `authentication_error` | 401 |
| `invalid_request` | 400, 404 (unknown alias or path), 405, 413, 415 |
| `unsupported_capability` | 400 |
| `provider_configuration_error` | 502 |
| `rate_limited` | 429, with `Retry-After` when it can be calculated |
| `provider_rate_limited` | 429, with `Retry-After` when the provider sent a delay in seconds |
| `provider_timeout` | 504 |
| `provider_unavailable` | 502, including `provider_circuit_open` |
| `admission_unavailable` | 503, when Redis or required PostgreSQL accounting is unavailable |
| `deadline_exceeded` | 504 |
| `internal_error` | 500 |

Every response carries an `X-Request-ID` header. A client may supply its own
(8 to 128 characters from `A-Z a-z 0-9 . _ -`); other values are replaced.

### Failure behaviour

- Provider failures where the provider certainly did not run the request
  (connection establishment failures or explicit 429 rejections) are retried with backoff and then
  passed to the alias's next fallback provider. Read timeouts, dropped connections,
  and malformed responses are returned to the client, because the provider may
  already have run, and charged for, the request.
- A stream is retried or redirected only before its first event. Once output has
  been sent, a failure ends the stream with a final `data: {"error": ...}` event
  and no `[DONE]`.
- When the deadline passes, the request ends with 504 `deadline_exceeded`, or with
  an error event if a stream has started.
- With admission control enabled, a Redis failure refuses requests with 503
  instead of admitting them without limits.

Details: [docs/resilience.md](docs/resilience.md) and
[docs/admission-control.md](docs/admission-control.md).

## Testing

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest tests/unit
uv run pytest tests/integration
```

The tests need no network access or provider accounts. Provider behaviour is
simulated with httpx transports and fake upstream servers. Admission tests start
real `redis-server` processes on Unix sockets and are skipped if the binary is
missing; set `GATEWAY_REQUIRE_REDIS_TESTS=1` (as CI does) to fail instead. Tests in
`tests/integration/test_socket_server.py` and
`tests/integration/test_multi_instance_load.py` run gateways and fake providers in
uvicorn on localhost to test streaming, disconnects, and limits across two
instances over real sockets.

To run the suite in the Linux build environment with Redis 7.0:

```sh
docker build --target validation -t ai-gateway:validation .
docker run --rm ai-gateway:validation
```

CI pins Ubuntu 24.04 and verifies the installed Redis is from the 7.0 series.

`tools/loadtest.py` measures latency and throughput against running gateways, and
`tools/bench_routing.py` measures routing overhead in process. See
[docs/benchmarks.md](docs/benchmarks.md) for commands and recorded results.

Evaluation tests use simulated or fake providers only. The PostgreSQL integration
tests (cost ledger, evaluation storage, routing) need a migrated database in
`GATEWAY_TEST_DATABASE_URL` and are skipped without one:

```sh
GATEWAY_DATABASE_URL=$GATEWAY_TEST_DATABASE_URL uv run alembic upgrade head
GATEWAY_TEST_DATABASE_URL=postgresql+asyncpg://... uv run pytest tests/integration
```

`tests/live/` contains a smoke test against a real provider. It is skipped unless
`GATEWAY_LIVE_TESTS=1` and spends tokens when enabled:

```sh
GATEWAY_LIVE_TESTS=1 GATEWAY_LIVE_CONFIG_FILE=config/gateway.toml GATEWAY_LIVE_MODEL=fast \
  uv run --env-file .env pytest tests/live
```

The GitHub Actions workflow in `.github/workflows/ci.yml` runs the checks above
with Redis installed, builds and installs the package, builds the container
images, validates the Compose files, audits dependencies with pip-audit, and scans
history with gitleaks.

## Limitations

- Text only. No tool calling, structured output, images, audio, or content-part
  arrays.
- One choice per request (`n` is not supported).
- `max_completion_tokens` is not accepted. Some OpenAI models only accept that
  field, so they cannot be used through the OpenAI-compatible adapter yet.
- Thinking output from Anthropic models is discarded. Thinking tokens still count
  towards `max_tokens` and `completion_tokens`.
- Anthropic stop reasons without an OpenAI equivalent (for example `pause_turn`)
  are returned as `finish_reason: null`.
- Routing quality rests on offline evaluation datasets. The two shipped datasets
  are small and synthetic. No evaluation against a real provider has been run for
  this repository, so no measured savings or quality figures exist for any
  vendor model.
- Routing ranks by maximum cost (full output allowance), not by predicted output
  length. Latency constraints use evaluation-run latency, not live traffic.
  Circuit-breaker state, one routing input, is per instance.
- Token limits are enforced on estimates: input tokens are estimated from UTF-8
  size, and requests whose provider reports no usage are charged their full
  reservation. Actual provider token consumption can differ from the configured
  limit.
- Limits are only as durable as Redis: if Redis loses data (restart without
  persistence, failover to a lagging replica), buckets reset and active leases are
  forgotten.
- Circuit breaker state is per process, not shared between instances.
- All 5xx responses are treated as uncertain execution: no retry or fallback,
  and unknown usage retains its reservation. Explicit 429/client rejections still
  depend on provider rejection semantics; exactly-once execution is not guaranteed.
- No keep-alive comments are sent on idle streams; proxies with short idle timeouts
  may close long pauses.
- `GET /v1/models` and other OpenAI endpoints are not implemented.
- Gateway keys are a static list from the environment. Team mapping and budgets are
  configuration based; changing them requires a restart.
- Without Redis admission, there is no request, token, or concurrency limit beyond
  each provider's connection pool. PostgreSQL budgets can still be enabled.
- Readiness does not check provider reachability. `/metrics` requires a gateway API key; health
  endpoints remain unauthenticated on the API port.

## Roadmap

Planned for later phases: response caching and richer tenant administration. The current cost APIs report ledger amounts; they
do not generate vendor invoices.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
