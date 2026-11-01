# API reference

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

## Supported request fields

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

## Rate limits

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

## Spending APIs

With cost accounting enabled, authenticated keys can call `GET /v1/usage`,
`GET /v1/usage/summary`, and `GET /v1/budgets`. Usage queries accept `start`,
`end` (timezone-aware, at most 31 days), `provider`, and `model`; `/v1/usage`
also accepts bounded `limit` and `offset`. A `team_id` query is allowed only for
keys explicitly listed as readers of that configured team. All amounts are integer
micro-USD.

## Errors

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

## Failure behaviour

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

Details: [resilience.md](resilience.md) and
[admission-control.md](admission-control.md).

## Operational endpoints

| Endpoint | Authentication | Purpose |
| --- | --- | --- |
| `GET /health/live` | none | The process is serving requests. |
| `GET /health/ready` | none | Startup finished, and Redis and PostgreSQL answer when they are required. Providers are not contacted. |
| `GET /metrics` | metrics key (`GATEWAY_METRICS_API_KEYS`) | Prometheus text format. Gateway API keys are refused. See [configuration.md](configuration.md#metrics-access). |
