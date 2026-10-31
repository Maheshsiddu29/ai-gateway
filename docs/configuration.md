# Configuration

The gateway reads two sources at startup and refuses to start if either is invalid:

- **Environment variables** prefixed `GATEWAY_`, for deployment settings and the
  gateway's own API keys.
- **A TOML file** named by `GATEWAY_CONFIG_FILE`, describing providers and model
  aliases. It must not contain secrets; each provider names the environment
  variable that holds its API key.

Configuration is read once. Changes require a restart.

## Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `GATEWAY_API_KEYS` | required | Comma-separated keys accepted in `Authorization: Bearer`. Whitespace around keys is ignored. At least one key is required. |
| `GATEWAY_ENVIRONMENT` | `production` | `production` or `development`. See [Production checks](#production-checks). |
| `GATEWAY_CONFIG_FILE` | `config/gateway.toml` | Path to the provider configuration file, relative to the working directory or absolute. The container image sets `/etc/gateway/gateway.toml`. |
| `GATEWAY_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, or `ERROR`. The httpx and httpcore loggers stay at `WARNING` regardless. |
| `GATEWAY_MAX_REQUEST_BYTES` | `1048576` | Maximum request body size in bytes, from 1024 to 67108864. Larger bodies receive 413. |
| `GATEWAY_REDIS_CA_FILE` | system trust store | Optional PEM CA file for Redis TLS. Certificates and hostnames are always verified. |
| `GATEWAY_REDIS_URL` | none | Redis URL. Production requires `rediss://` for TCP; local `unix://` sockets are allowed. Plain `redis://` is restricted to development. Query parameters and fragments are rejected to prevent transport-policy overrides. |

Provider API keys are read from whichever variables the configuration file names
in `api_key_env`, for example `OPENAI_API_KEY`. A referenced variable that is unset
or empty is a startup error.

To generate a gateway key:

```sh
openssl rand -hex 32
```

To rotate keys without rejecting clients, list both the old and new key, restart,
move clients to the new key, then remove the old one and restart again.

## Configuration file

```toml
[providers.<name>]
type = "openai_compatible"   # or "anthropic"
base_url = "https://..."
# provider settings...

[models.<alias>]
provider = "<name>"
model = "<upstream model id>"
```

Unknown keys are rejected anywhere in the file. `config/gateway.example.toml` is a
starting point for real providers, and `config/gateway.mock.toml` is used with the
mock provider.

### Providers

Provider names must match `[a-z][a-z0-9_-]*` (up to 64 characters).

Settings for both provider types:

| Key | Default | Description |
| --- | --- | --- |
| `type` | required | `openai_compatible` or `anthropic`. |
| `base_url` | required | HTTP(S) URL. For `openai_compatible` it includes the API prefix, e.g. `https://api.openai.com/v1`; requests go to `{base_url}/chat/completions`. For `anthropic` it is the host, e.g. `https://api.anthropic.com`; requests go to `{base_url}/v1/messages`. Credentials, query strings, and fragments are rejected. |
| `api_key_env` | none | Environment variable holding the provider API key. Required for `anthropic`. Optional for `openai_compatible`, for servers without authentication. |
| `allow_insecure_http` | `false` | Permit a non-HTTPS `base_url` in production, for example a self-hosted server on a private network. |
| `connect_timeout_seconds` | `5` | Time allowed to establish a connection. |
| `read_timeout_seconds` | `120` | Maximum time between bytes received from the provider. For streams this is the longest allowed pause between chunks. For non-streaming requests it usually bounds the whole generation, because providers send nothing until they finish. |
| `write_timeout_seconds` | `30` | Maximum time to send the request body. |
| `pool_timeout_seconds` | `5` | Maximum wait for a free connection when `max_connections` are in use. |
| `max_connections` | `100` | Size of the connection pool to this provider. |

All timeouts must be positive. A timeout returns `provider_timeout` (504) to the
client.

The OpenAI-compatible adapter sends `Authorization: Bearer <key>` when
`api_key_env` is set.

The Anthropic adapter sends `x-api-key` and `anthropic-version`, and has one extra
setting:

| Key | Default | Description |
| --- | --- | --- |
| `api_version` | `2026-10-21` | Messages API version sent in `anthropic-version`: `2026-10-21` or `2023-06-01`. On `2026-10-21` the API rejects `temperature` and `top_p`, so the gateway rejects them with `unsupported_capability`. On `2023-06-01` they are forwarded, with `temperature` limited to 1. Some models accept only default sampling values regardless of version; requests with other values are rejected by the provider. |

### Models

Aliases are the values clients send as `model`. They must match
`[A-Za-z0-9][A-Za-z0-9._:-]*` (up to 64 characters). At least one alias is
required.

| Key | Default | Description |
| --- | --- | --- |
| `provider` | required | Name of a provider defined in the same file. |
| `model` | required | Model identifier sent to the provider. |
| `default_max_tokens` | none | `max_tokens` to send when the client omits it. Anthropic requires `max_tokens`; without a default, requests to an Anthropic alias must include it. |
| `max_output_tokens` | none | Largest `max_tokens` a client may request; larger values are rejected with `max_tokens_exceeds_limit`. When the client omits `max_tokens` and there is no `default_max_tokens`, this value is sent. Must be at least `default_max_tokens`. |
| `fallbacks` | `[]` | Up to three `{ provider, model }` entries tried in order when the primary fails without running the request. See [resilience.md](resilience.md#fallback). |

Several aliases can point at the same provider and model.

### Resilience

```toml
[resilience]
request_deadline_seconds = 300
max_attempts_per_provider = 2

[resilience.circuit_breaker]
failure_threshold = 5
recovery_seconds = 30
```

| Key | Default | Description |
| --- | --- | --- |
| `request_deadline_seconds` | `300` | End-to-end limit for one request, from the start of request handling until the last response byte, including every provider attempt and backoff delay and, for streams, the whole stream. At most 3600. Provider timeouts still apply to individual operations. |
| `max_attempts_per_provider` | `2` | Attempts per provider for retryable failures, including the first. `1` disables retries. At most 5. |
| `retry_initial_backoff_seconds` | `0.2` | Delay after the first failed attempt. |
| `retry_max_backoff_seconds` | `2` | Cap on the exponentially growing delay. |
| `retry_jitter_ratio` | `0.5` | Each delay is reduced by a random fraction of up to this ratio (0 to 1). |
| `max_retry_after_seconds` | `10` | A provider `Retry-After` longer than this is not waited for; the error is returned instead. |
| `retry_on_status` | `[429]` | HTTP statuses eligible for retry. Only 429 and 5xx are allowed. All 5xx statuses are uncertain and never retried, even if listed in an older configuration. |

`[resilience.circuit_breaker]`:

| Key | Default | Description |
| --- | --- | --- |
| `enabled` | `true` | Use per-provider circuit breakers. |
| `failure_threshold` | `5` | Provider failures within the window that open the breaker. |
| `failure_window_seconds` | `30` | Window for counting failures. |
| `recovery_seconds` | `30` | Time open before probe requests are allowed. |
| `half_open_max_probes` | `1` | Concurrent probe requests while half-open. |

See [resilience.md](resilience.md) for retry eligibility and breaker behaviour.

### Admission control

```toml
[admission]
enabled = true

[admission.default_limits]
requests_per_minute = 60
request_burst = 10
tokens_per_minute = 100000
max_concurrent_requests = 8

# Replaces default_limits for one key, identified by its principal ID.
[admission.key_limits.key-0123456789ab]
requests_per_minute = 600
```

| Key | Default | Description |
| --- | --- | --- |
| `enabled` | `false` | Enforce limits through Redis. When false, no limits are enforced and the gateway logs a warning at startup; `/health/ready` reports `"admission": "disabled"`. |
| `redis_connect_timeout_seconds` | `1` | Time allowed to connect to Redis. |
| `redis_operation_timeout_seconds` | `1` | Time allowed for one admission or settlement call. |
| `redis_pool_timeout_seconds` | `1` | Maximum wait for a free pooled Redis connection. |
| `redis_max_connections` | `50` | Size of the Redis connection pool. |
| `lease_grace_seconds` | `30` | Added to `request_deadline_seconds` to form the concurrency lease lifetime. |
| `reservation_retention_seconds` | `300` | How long a settled reservation is remembered after its lease, to detect duplicate settlement. |

Limits, in `[admission.default_limits]` or `[admission.key_limits.<principal id>]`:

| Key | Description |
| --- | --- |
| `requests_per_minute` | Request bucket refill rate. |
| `request_burst` | Request bucket capacity. Defaults to `requests_per_minute`. |
| `tokens_per_minute` | Token bucket refill rate. Requires every request to have an output bound (see `max_output_tokens`). |
| `token_burst` | Token bucket capacity. Defaults to `tokens_per_minute`. A single reservation larger than this is rejected. |
| `max_concurrent_requests` | Active requests allowed at once. |

An omitted limit is not enforced. Rates and bursts range from 1 to 10^9. A key's
entry in `key_limits` replaces the defaults entirely; unspecified limits in it are
not enforced for that key. A principal ID is `key-` followed by the first 12 hex
characters of the SHA-256 digest of the gateway key:

```sh
printf %s "$GATEWAY_KEY" | sha256sum | cut -c1-12
```

See [admission-control.md](admission-control.md) for the algorithms and failure
behaviour.

## Production checks

With `GATEWAY_ENVIRONMENT=production`, which is the default:

- Every gateway API key must be at least 32 characters.
- Every provider `base_url` must use HTTPS unless the provider sets
  `allow_insecure_http = true`.

`development` relaxes both checks. Authentication is required in both modes.

## Example

```toml
[providers.openai]
type = "openai_compatible"
base_url = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"

[providers.anthropic]
type = "anthropic"
base_url = "https://api.anthropic.com"
api_key_env = "ANTHROPIC_API_KEY"
read_timeout_seconds = 300

[providers.local]
type = "openai_compatible"
base_url = "http://inference.internal:8000/v1"
allow_insecure_http = true

[models.fast]
provider = "openai"
model = "your-openai-model-id"

[models.balanced]
provider = "anthropic"
model = "your-anthropic-model-id"
default_max_tokens = 1024

[models.local]
provider = "local"
model = "served-model-name"
```

## Changes from Phase 1 configurations

Phase 1 configuration files load unchanged. Behaviour differences:

- Every request now has an end-to-end deadline (`request_deadline_seconds`,
  default 300).
- Failures where the provider certainly did not run the request are retried once
  by default (`max_attempts_per_provider = 2`); set it to 1 for the Phase 1
  behaviour.
- Each provider has a circuit breaker, enabled by default.
- `/health/ready` includes an `admission` field in its response body.
- If a model sets `max_output_tokens`, requests without `max_tokens` are sent that
  value instead of omitting the field.

### Audit hardening migration

Production Redis TCP URLs must now use `rediss://`; use `GATEWAY_REDIS_CA_FILE`
for a private certificate authority. The local Compose stack uses development mode
on an internal network. Redis URL query options are intentionally unsupported.

`GET /metrics` now requires a gateway Bearer API key. Configure the scraper's
authorization header; health endpoints remain public. Every configured gateway
key can read aggregate metrics; metrics have no per-key labels.
