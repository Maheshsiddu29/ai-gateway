# Local quickstart

This runs the gateway with a mock provider, Redis, and PostgreSQL in Docker
Compose. No provider account is needed and nothing is charged. The mock answers
every request with a fixed sentence and reports word counts as token usage.

Requirements: Docker with Compose v2, `curl`, and `openssl`. Allow about two
minutes for the first build.

## 1. Clone and configure

```sh
git clone https://github.com/Maheshsiddu29/ai-gateway.git
cd ai-gateway
export GATEWAY_API_KEYS=$(openssl rand -hex 32)          # client key for the API
export GATEWAY_METRICS_API_KEYS=$(openssl rand -hex 32)  # separate key for /metrics
```

Compose reads both variables from the shell or from a `.env` file in the
repository root. `.env` is ignored by Git. Keep the keys out of shell history and
committed files if the machine is shared.

The stack uses `config/gateway.compose.toml`. It runs the gateway in development
mode, which allows short keys and plain HTTP to the mock. PostgreSQL uses a fixed
local-only password, Redis keeps no data on disk, and neither is published to the
host. Do not reuse this configuration outside a local machine.

## 2. Start the services

```sh
docker compose up --build --wait
```

This starts five containers:

| Service | Purpose |
| --- | --- |
| `postgres` | Cost ledger, budgets, and evaluation results (data in the `postgres-data` volume). |
| `migrate` | Runs `alembic upgrade head` once, then exits. The gateway starts only after it succeeds. |
| `redis` | Request, token, and concurrency limits. |
| `mock-upstream` | Mock OpenAI-compatible and Anthropic API on `127.0.0.1:9000`. |
| `gateway` | The gateway on `127.0.0.1:8000`. |

`--wait` returns when the gateway's readiness check passes. To check every step
below at once:

```sh
tools/smoke_compose.sh
```

## 3. Send a request

```sh
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEYS" \
  -H "Content-Type: application/json" \
  -d '{"model": "fast", "messages": [{"role": "user", "content": "one two three"}]}'
```

```json
{"id": "chatcmpl-...", "object": "chat.completion", "created": 1793497648, "model": "fast",
 "choices": [{"index": 0, "message": {"role": "assistant",
   "content": "Mock reply from mock-small: received 3 words."}, "finish_reason": "stop"}],
 "usage": {"prompt_tokens": 3, "completion_tokens": 7, "total_tokens": 10}}
```

## 4. Stream a response

```sh
curl -sN http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEYS" \
  -H "Content-Type: application/json" \
  -d '{"model": "balanced", "stream": true, "stream_options": {"include_usage": true},
       "messages": [{"role": "user", "content": "one two three"}]}'
```

Each `data:` line is a `chat.completion.chunk`. The stream ends with a chunk
carrying `finish_reason`, a usage chunk, and `data: [DONE]`.

## 5. Inspect usage and budgets

```sh
# Every upstream attempt made with your key: provider, model, tokens, charge.
curl -s "http://127.0.0.1:8000/v1/usage?limit=5" -H "Authorization: Bearer $GATEWAY_API_KEYS"
# Totals per provider and model for the last seven days.
curl -s http://127.0.0.1:8000/v1/usage/summary -H "Authorization: Bearer $GATEWAY_API_KEYS"
# Daily and monthly limits, spend, and what is left.
curl -s http://127.0.0.1:8000/v1/budgets -H "Authorization: Bearer $GATEWAY_API_KEYS"
```

Amounts are integer micro-USD at the mock prices in
`config/gateway.compose.toml` ($1.00 per day per key by default). They are
fictional. To see admission control refuse a request, lower the limits under
`[admission.default_limits]` or `[cost.default_key_limits]` and recreate the
gateway (next step).

## 6. Add a model alias

Append to `config/gateway.compose.toml`:

```toml
[models.summarize]
provider = "mock-openai"
model = "mock-summary"
max_output_tokens = 256

[[cost.pricing]]
provider = "mock-openai"
model = "mock-summary"
version = "mock-2026-10"
effective_at = 2020-01-01T00:00:00Z
input_per_million_usd = "0.20"
output_per_million_usd = "0.40"
```

Cost accounting is enabled, so every model needs a price, and the gateway
refuses to start without one. Recreate the gateway so that it rereads the file:

```sh
docker compose up -d --force-recreate --wait gateway
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEYS" -H "Content-Type: application/json" \
  -d '{"model": "summarize", "messages": [{"role": "user", "content": "a b c d"}]}'
```

If the gateway does not become healthy, `docker compose logs gateway` shows the
configuration error. [configuration.md](configuration.md) lists every setting.

## 7. Routing

The Compose configuration enables routing (`[routing] enabled = true`) and
defines two routed aliases:

- `routed` uses `lowest_cost` between `mock-anthropic/mock-large` and the
  cheaper `mock-openai/mock-routed-small`;
- `routed-quality` uses `quality_constrained` on the same two models. It needs
  recorded evaluation evidence, which a new stack does not have.

```sh
curl -s http://127.0.0.1:8000/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEYS" -H "Content-Type: application/json" \
  -d '{"model": "routed", "messages": [{"role": "user", "content": "route me"}]}'
# The latest attempt shows which model served it: mock-routed-small.
curl -s "http://127.0.0.1:8000/v1/usage?limit=1" -H "Authorization: Bearer $GATEWAY_API_KEYS"

# Which candidates of each quality-constrained alias qualify now:
docker compose exec gateway python -m gateway.evaluation status --config /etc/gateway/gateway.toml
```

```text
routed-quality: support-ticket-category v1, lower bound >= 0.9, at least 40 cases, at most 30 days old; when nothing qualifies: primary
  mock-anthropic/mock-large: no_evidence; no development run recorded
  mock-openai/mock-routed-small: no_evidence; no development run recorded
```

With no qualified candidate, `routed-quality` sends requests to its primary. The
log records this as not quality-verified:

```sh
docker compose logs gateway | grep routed-quality
```

```text
... "routing_reason": "no_qualified_candidate", "routing_quality_verified": false,
    "routing_primary_exclusion": "no_evidence", ...
```

The mock cannot answer evaluation cases, so it cannot qualify. Evidence comes
from evaluating real models; see [evaluation.md](evaluation.md) and
[routing.md](routing.md).

## 8. Metrics

```sh
curl -s http://127.0.0.1:8000/metrics -H "Authorization: Bearer $GATEWAY_METRICS_API_KEYS" | head
# A gateway API key is refused with 401.
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/metrics \
  -H "Authorization: Bearer $GATEWAY_API_KEYS"
```

To point a local Prometheus at the gateway, see
[configuration.md](configuration.md#metrics-access).

## 9. Shut down

```sh
docker compose down        # stop and remove containers; keep the database volume
docker compose down -v     # also delete the database volume
```

`docker compose --profile multi up --build --wait` starts a second gateway on
`127.0.0.1:8001` that shares Redis and PostgreSQL with the first, for testing
limits across instances.

## Without Docker

The tests and evaluation tools run without Docker:

```sh
uv sync --locked
uv run pytest tests/unit
# A simulated evaluation: no network calls, results marked simulated.
uv run python -m gateway.evaluation run --config evaluations/experiments/ticket-routing.toml \
  --dataset evaluations/datasets/support-ticket-category-v1.jsonl \
  --target sim/small --max-tokens 8 --output results/ --simulate sim/small=0.9
```

To run the gateway itself on the host, see the README's
[Running without Docker](../README.md#running-without-docker).
