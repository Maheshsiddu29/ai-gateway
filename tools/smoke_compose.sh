#!/usr/bin/env bash
# End-to-end check of a running Compose stack (docker compose up --build --wait):
# readiness, a completion, a stream, usage and budgets, routing, and metrics access.
# It uses only the mock provider and costs nothing.
#
#   GATEWAY_API_KEYS=... GATEWAY_METRICS_API_KEYS=... tools/smoke_compose.sh [base-url]
set -euo pipefail

base="${1:-http://127.0.0.1:8000}"
key="${GATEWAY_API_KEYS:?set GATEWAY_API_KEYS}"
key="${key%%,*}"
metrics_key="${GATEWAY_METRICS_API_KEYS:-}"
metrics_key="${metrics_key%%,*}"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

# Prints the HTTP status, then the body, of a request.
call() {
    curl -sS --max-time 30 -w '\n%{http_code}' "$@"
}

status_of() {
    tail -n 1 <<<"$1"
}

body_of() {
    sed '$d' <<<"$1"
}

complete() {
    call "$base/v1/chat/completions" -H "Authorization: Bearer $key" \
        -H "Content-Type: application/json" -d "$1"
}

response=$(call "$base/health/ready")
[[ $(status_of "$response") == 200 ]] || fail "readiness: $(body_of "$response")"
echo "ok: ready"

response=$(complete '{"model": "fast", "messages": [{"role": "user", "content": "one two three"}]}')
[[ $(status_of "$response") == 200 ]] || fail "completion: $(body_of "$response")"
grep -q '"object":"chat.completion"' <<<"$(body_of "$response")" || fail "completion body"
echo "ok: non-streaming completion"

response=$(complete '{"model": "balanced", "stream": true, "stream_options": {"include_usage": true},
    "messages": [{"role": "user", "content": "one two three"}]}')
[[ $(status_of "$response") == 200 ]] || fail "stream: $(body_of "$response")"
[[ $(body_of "$response" | grep '^data: ' | tail -n 1) == "data: [DONE]" ]] || fail "stream end"
grep -q '"usage"' <<<"$(body_of "$response")" || fail "stream usage chunk"
echo "ok: streaming completion"

response=$(complete '{"model": "routed", "messages": [{"role": "user", "content": "route me"}]}')
[[ $(status_of "$response") == 200 ]] || fail "routed completion: $(body_of "$response")"
response=$(call "$base/v1/usage?limit=1" -H "Authorization: Bearer $key")
[[ $(status_of "$response") == 200 ]] || fail "usage: $(body_of "$response")"
grep -q '"model":"mock-routed-small"' <<<"$(body_of "$response")" ||
    fail "the routed alias did not use the cheaper model: $(body_of "$response")"
echo "ok: lowest_cost routing chose mock-routed-small"

for path in /v1/usage/summary /v1/budgets; do
    response=$(call "$base$path" -H "Authorization: Bearer $key")
    [[ $(status_of "$response") == 200 ]] || fail "$path: $(body_of "$response")"
done
echo "ok: usage summary and budgets"

response=$(call "$base/metrics" -H "Authorization: Bearer $key")
[[ $(status_of "$response") == 401 ]] || fail "a gateway key could read /metrics"
if [[ -n $metrics_key ]]; then
    response=$(call "$base/metrics" -H "Authorization: Bearer $metrics_key")
    [[ $(status_of "$response") == 200 ]] || fail "metrics key refused"
    grep -q '^gateway_http_requests_total' <<<"$(body_of "$response")" || fail "metrics body"
    echo "ok: metrics readable only with the metrics key"
else
    echo "ok: gateway keys cannot read /metrics (set GATEWAY_METRICS_API_KEYS to test access)"
fi
