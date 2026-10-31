# AI Gateway

An HTTP service that exposes one OpenAI-compatible chat completion endpoint and
forwards requests to configured LLM providers. Clients call a model alias such as
`fast`; the gateway maps the alias to a provider and an upstream model, translates
the request, and normalizes the response.

This is Phase 1: the request pipeline, two provider adapters, streaming, and
authentication. Rate limiting, budgets, routing, caching, and failover are planned
but not implemented (see [Roadmap](#roadmap)).

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
- `GET /health/live` and `GET /health/ready`.

## Architecture

```text
client ─► RequestContextMiddleware   request ID, access log, last-resort 500
           └─► /v1/chat/completions
                 auth ─► bounded body read ─► schema validation
                 └─► CompletionService   alias ─► provider + upstream model
                       └─► Provider adapter   translate, call upstream, normalize
                             └─► httpx.AsyncClient (one per provider)
```

The API layer speaks the OpenAI wire format. The service layer is independent of
HTTP. Each adapter owns its provider's serialization and returns provider-neutral
results (`Completion`, or a stream of `TextDelta` events ending in `StreamEnd`),
which the API layer encodes back into OpenAI-format responses.

See [docs/architecture.md](docs/architecture.md) for the request lifecycle,
streaming data flow, and failure handling, and [docs/adr/](docs/adr/) for the main
design decisions.

## Requirements

- Python 3.13. The code uses syntax from Python 3.12 (PEP 695 type aliases and
  generic functions); 3.13 is the version tested and used in the container image.
- [uv](https://docs.astral.sh/uv/) 0.13 or later, for the locked dependency set in
  `uv.lock`.
- Docker with Compose, optionally, for the container setup.

## Local setup

```sh
uv sync                     # creates .venv from uv.lock, including dev tools
cp .env.example .env        # then set GATEWAY_API_KEYS
```

### Run with the mock provider

`tools/mock_upstream.py` implements enough of the OpenAI and Anthropic APIs to
exercise both adapters without vendor accounts. With Docker:

```sh
export GATEWAY_API_KEYS=$(openssl rand -hex 32)
docker compose up --build
```

Without Docker, run the mock and the gateway in two terminals. The mock
configuration addresses the mock by its Compose service name, so point it at
localhost first:

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

`GATEWAY_ENVIRONMENT` defaults to `production`, which requires gateway keys of at
least 32 characters and HTTPS provider URLs. Every setting is described in
[docs/configuration.md](docs/configuration.md).

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
| `provider_rate_limited` | 429, with `Retry-After` when the provider sent a delay in seconds |
| `provider_timeout` | 504 |
| `provider_unavailable` | 502 |
| `internal_error` | 500 |

Every response carries an `X-Request-ID` header. A client may supply its own
(8 to 128 characters from `A-Z a-z 0-9 . _ -`); other values are replaced.

## Testing

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest tests/unit
uv run pytest tests/integration
```

The tests need no network access or provider accounts. Provider behaviour is
simulated with httpx transports; `tests/integration/test_socket_server.py` runs the
gateway and a fake upstream in uvicorn on localhost to test incremental delivery
and client disconnects over real sockets.

`tests/live/` contains a smoke test against a real provider. It is skipped unless
`GATEWAY_LIVE_TESTS=1` and spends tokens when enabled:

```sh
GATEWAY_LIVE_TESTS=1 GATEWAY_LIVE_CONFIG_FILE=config/gateway.toml GATEWAY_LIVE_MODEL=fast \
  uv run --env-file .env pytest tests/live
```

The GitHub Actions workflow in `.github/workflows/ci.yml` runs the checks above,
builds and installs the package, builds the container image, audits dependencies
with pip-audit, and scans history with gitleaks.

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
- No retries, failover, or rate limiting. Upstream errors are returned to the
  client.
- The read timeout limits the time between reads, not the total request time. A
  stream that keeps producing data is never cut off, and there is no overall
  deadline. A non-streaming upstream that sends nothing until it has finished must
  respond within the read timeout.
- No keep-alive comments are sent on idle streams; proxies with short idle timeouts
  may close long pauses.
- `GET /v1/models` and other OpenAI endpoints are not implemented.
- Gateway keys are a static list from the environment. There are no per-key
  permissions or quotas, and changing keys requires a restart.
- Readiness reflects local state only; it does not check provider reachability.
- Request bodies are limited in size, but there is no limit on concurrent requests
  beyond each provider's connection pool.

## Roadmap

Planned for later phases, in no fixed order: distributed rate limiting, per-tenant
budget enforcement, cost-aware model routing, provider failover before output has
started, response caching, and cost analytics. None of these exist yet.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE).
