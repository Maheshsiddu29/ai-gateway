# AI Gateway

An HTTP service that exposes an OpenAI-compatible chat completion endpoint and
forwards requests to configured LLM providers. Clients call a model alias such as
`fast`. The gateway maps it to a provider and upstream model, enforces limits
and budgets, translates the request, and normalizes the response.

## Features

- `POST /v1/chat/completions`: a documented subset of the OpenAI Chat
  Completions request, non-streaming and Server-Sent Events streaming. Unsupported
  parameters are rejected, not ignored.
- Adapters for OpenAI-compatible APIs (OpenAI and self-hosted servers with the
  same API) and the Anthropic Messages API.
- Per-key request, token, and concurrency limits shared across instances through
  Redis, failing closed when Redis is unavailable.
- An end-to-end deadline per request, bounded retries only when a provider
  certainly did not run the request, per-provider circuit breakers, and fallback
  providers.
- A PostgreSQL ledger of every upstream attempt at versioned per-model prices,
  daily and monthly key and team budgets in exact micro-USD, and usage APIs.
- Per-alias routing (`fixed`, `lowest_cost`, `quality_constrained`) from a model
  capability registry, prices, circuit-breaker state, and recorded evaluation
  results.
- Offline evaluation: versioned development and holdout datasets, deterministic
  scorers, a CLI that calls real providers only after an approved cost-bounded
  plan, and a replay tool that compares routing policies on holdout data.
- JSON logs with request IDs, Prometheus metrics behind a separate credential,
  and liveness and readiness endpoints.
- A separate agent runtime (`services/agent_runtime`) that runs a bounded,
  read-only Linux incident investigation through the gateway: model-chosen
  diagnostics from a fixed allowlist, and a diagnosis that must cite the
  evidence collected. Investigations are accepted over HTTP, stored in their own
  PostgreSQL schema, and run by a durable worker on a LangGraph graph with
  checkpoints, leases, and a journal that never repeats a model call whose
  outcome is unknown. See [docs/agent-runtime.md](docs/agent-runtime.md) and
  [docs/agent-durable-execution.md](docs/agent-durable-execution.md).

Caching is not implemented.

## Requirements

- Docker with Compose v2, for the local stack.
- For development on the host: Python 3.13 and [uv](https://docs.astral.sh/uv/)
  0.13 or later. The tests also need `redis-server` 7 or later, and the PostgreSQL
  integration tests a PostgreSQL 16 database.

## Quickstart

The Compose stack runs the gateway, a mock provider, Redis, and PostgreSQL, and
applies migrations before the gateway starts. No provider account is needed.

```sh
export GATEWAY_API_KEYS=$(openssl rand -hex 32)
export GATEWAY_METRICS_API_KEYS=$(openssl rand -hex 32)
docker compose up --build --wait            # gateway on 127.0.0.1:8000

curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEYS" -H "Content-Type: application/json" \
  -d '{"model": "fast", "messages": [{"role": "user", "content": "one two three"}]}'

tools/smoke_compose.sh                      # completion, stream, usage, routing, metrics
docker compose down -v
```

[docs/quickstart.md](docs/quickstart.md) walks through streaming, usage and
budgets, adding an alias, routing, and metrics.
`docker compose --profile agent up` also starts the agent runtime, its own
PostgreSQL, and a durable worker; see
[docs/agent-runtime.md](docs/agent-runtime.md#running-it-locally).

### Running without Docker

`config/gateway.mock.toml` uses the mock provider with admission control and cost
accounting disabled, so neither Redis nor PostgreSQL is needed:

```sh
uv sync --locked
sed 's#mock-upstream:9000#127.0.0.1:9000#' config/gateway.mock.toml > config/gateway.toml
uv run uvicorn mock_upstream:app --app-dir tools --port 9000 &
GATEWAY_ENVIRONMENT=development GATEWAY_API_KEYS=dev-key MOCK_UPSTREAM_API_KEY=mock-upstream-key \
  uv run uvicorn gateway.main:create_app --factory --port 8000 --no-access-log
```

## Configuration

Deployment settings and credentials come from `GATEWAY_*` environment variables.
Providers, model aliases, limits, prices, and routing come from a TOML file named
by `GATEWAY_CONFIG_FILE`. The file holds no secrets: each provider names the
environment variable that holds its key.

```toml
[providers.anthropic]
type = "anthropic"
base_url = "https://api.anthropic.com"
api_key_env = "ANTHROPIC_API_KEY"

[models.balanced]
provider = "anthropic"
model = "your-anthropic-model-id"
max_output_tokens = 1024
fallbacks = [{ provider = "openai", model = "your-openai-model-id" }]
```

`GATEWAY_ENVIRONMENT` defaults to `production`, which requires keys of at least
32 characters, HTTPS provider URLs, and TLS to Redis. `config/gateway.example.toml`
is a starting point for real providers. With `[cost] enabled = true`, apply
migrations (`GATEWAY_DATABASE_URL=... uv run alembic upgrade head`) before
starting the gateway. [docs/configuration.md](docs/configuration.md) describes
every setting.

## Example request

```sh
curl -sN http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEYS" -H "Content-Type: application/json" \
  -d '{"model": "balanced", "stream": true, "stream_options": {"include_usage": true},
       "messages": [{"role": "user", "content": "Explain database indexing."}]}'
```

Responses use the OpenAI format with the requested alias as `model`. Errors use
an OpenAI-style envelope with a stable `type` and `code`. A stream that fails after
output has started ends with a `data: {"error": ...}` event and no `[DONE]`.
[docs/api.md](docs/api.md) lists the accepted fields, errors, rate-limit
responses, and usage APIs.

## Routing and evaluation

An alias with `[models.<alias>.routing]` chooses among candidate models per
request. `lowest_cost` picks the cheapest model that supports the request.
`quality_constrained` also requires a recorded evaluation on the alias's task
whose 95% Wilson lower bound meets `min_success_rate`. When nothing qualifies, it
either uses the primary, logged as not quality-verified, or rejects the request.
Every routed attempt keeps its own budget reservation, retries, and fallback.

Evaluation results come from `python -m gateway.evaluation`:

```sh
# Simulated: no network calls; never accepted as routing evidence.
uv run python -m gateway.evaluation run --config config/gateway.toml \
  --dataset evaluations/datasets/support-ticket-category-v1.jsonl \
  --target openai/small-model-id --max-tokens 8 --output results/ \
  --simulate openai/small-model-id=0.9

# Real providers: prints a preflight plan with its worst-case cost and sends
# nothing until the same command is repeated with --approve <plan sha256>.
uv run --env-file .env python -m gateway.evaluation run --config config/gateway.toml \
  --dataset evaluations/datasets/support-ticket-category-v1.jsonl \
  --target openai/small-model-id --max-tokens 8 --output results/ \
  --live --max-cost-micro-usd 200000 --max-requests 100

GATEWAY_DATABASE_URL=... uv run python -m gateway.evaluation record results/*.json
GATEWAY_DATABASE_URL=... uv run python -m gateway.evaluation status --config config/gateway.toml
```

Development-partition runs qualify models. Holdout-partition runs never do.
`tools/routing_experiment.py` replays a holdout dataset to compare the fixed
primary, `lowest_cost`, and `quality_constrained` on cost and quality, with
paired confidence intervals and unknown costs reported separately. See
[docs/routing.md](docs/routing.md) and [docs/evaluation.md](docs/evaluation.md).

No evaluation against a real provider has been run for this repository, so it
contains no measured cost or quality figures for any vendor model.

## Observability

- One `completion finished` JSON log event per request: alias, provider, model,
  outcome, duration, token usage, routing decision, and ledger request ID. Prompts
  and outputs are never logged.
- `GET /metrics` in Prometheus format, readable only with a key from
  `GATEWAY_METRICS_API_KEYS`. Gateway API keys are refused.
- `GET /health/live` and `GET /health/ready`, unauthenticated. Readiness checks
  Redis and PostgreSQL when they are required.

## Testing

```sh
uv sync --locked
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest tests/unit
GATEWAY_DATABASE_URL=$GATEWAY_TEST_DATABASE_URL uv run alembic upgrade head
GATEWAY_TEST_DATABASE_URL=postgresql+asyncpg://... uv run pytest tests/integration
```

The agent runtime has its own checks; see
[docs/agent-runtime.md](docs/agent-runtime.md#testing).

The tests need no network access or provider credentials. Providers are
simulated with httpx transports, fake servers, and the mock provider. Redis
tests start their own `redis-server` and are skipped without it, unless
`GATEWAY_REQUIRE_REDIS_TESTS=1` (as in CI). PostgreSQL tests are skipped without
`GATEWAY_TEST_DATABASE_URL`. `tests/live/` calls a real provider only with
`GATEWAY_LIVE_TESTS=1`. CI (`.github/workflows/ci.yml`) also builds the package
and images, runs the Compose smoke test, audits dependencies, and scans for
secrets. [docs/benchmarks.md](docs/benchmarks.md) has the benchmark tools and
recorded results.

## Documentation

| Document | Contents |
| --- | --- |
| [docs/quickstart.md](docs/quickstart.md) | Local stack, step by step. |
| [docs/api.md](docs/api.md) | Request fields, errors, rate limits, usage APIs. |
| [docs/configuration.md](docs/configuration.md) | Every setting, metrics access, migrations between phases. |
| [docs/architecture.md](docs/architecture.md) | Request lifecycle, modules, security controls. |
| [docs/admission-control.md](docs/admission-control.md), [docs/resilience.md](docs/resilience.md) | Limits, deadlines, retries, circuit breakers, fallback. |
| [docs/cost-accounting.md](docs/cost-accounting.md), [docs/budget-enforcement.md](docs/budget-enforcement.md) | Ledger semantics and spending admission. |
| [docs/routing.md](docs/routing.md), [docs/evaluation.md](docs/evaluation.md) | Routing policies, evaluation datasets, live-run safeguards, comparisons. |
| [docs/security.md](docs/security.md) | Trust boundaries, controls, database roles, and open risks. |
| [docs/agent-runtime.md](docs/agent-runtime.md) | The agent runtime: investigation workflow, diagnostics, API, configuration, operations, limits. |
| [docs/agent-durable-execution.md](docs/agent-durable-execution.md) | Durable execution: states, leases, the model-call journal, budgets, recovery. |
| [docs/benchmarks.md](docs/benchmarks.md) | Measurements and how to reproduce them. |
| [docs/adr/](docs/adr/) | Design decisions. |

## Limitations

- Text only: no tool calling, structured output, images, audio, or content-part
  arrays. One choice per request. `max_completion_tokens` is not accepted.
- Anthropic thinking output is discarded but counts toward output tokens. Stop
  reasons without an OpenAI equivalent become `finish_reason: null`.
- Token limits use an input estimate (UTF-8 bytes / 3). Requests whose provider
  reports no usage are charged their full reservation. Costs are gateway
  accounting at configured prices, not invoices.
- Any 5xx, read timeout, or broken stream is treated as possibly executed: no
  retry or fallback, and its reservation is kept. Exactly-once execution is not
  guaranteed.
- Routing ranks by maximum cost (full output allowance). Quality evidence comes
  from offline datasets: the shipped ones are small, synthetic, and written by
  one author. Circuit-breaker state is per instance.
- Limits last only as long as Redis keeps its data. Keys, teams, and budgets are
  static configuration and change only on restart.
- `GET /v1/models` and other OpenAI endpoints are not implemented. Idle streams
  get no keep-alive comments.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
