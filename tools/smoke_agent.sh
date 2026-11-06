#!/usr/bin/env bash
# End-to-end check of the agent runtime in a running Compose stack
# (docker compose --profile agent up --build --wait): readiness, authentication,
# an investigation accepted by the API and finished by the durable worker through
# the gateway and the mock provider, and an idempotent resubmission. The
# diagnostics read the worker's container. It costs nothing.
#
#   AGENT_API_KEYS=... AGENT_GATEWAY_API_KEY=... tools/smoke_agent.sh [base-url]
set -euo pipefail

base="${1:-http://127.0.0.1:8100}"
key="${AGENT_API_KEYS:?set AGENT_API_KEYS}"
key="${key%%,*}"
gateway_key="${AGENT_GATEWAY_API_KEY:?set AGENT_GATEWAY_API_KEY}"
# A different key per run, so a rerun against the same stack submits again.
idempotency_key="smoke-$(date +%s)-$$"

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

field() {
    python3 -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"
}

submit() {
    call "$base/v1/investigations" -H "Content-Type: application/json" "$@" \
        -d '{"incident": "The payments container restarted with exit code 137."}'
}

response=$(call "$base/health/ready")
[[ $(status_of "$response") == 200 ]] || fail "readiness: $(body_of "$response")"
echo "ok: ready"

response=$(submit)
[[ $(status_of "$response") == 401 ]] || fail "an investigation was accepted without a key"
response=$(submit -H "Authorization: Bearer $gateway_key")
[[ $(status_of "$response") == 401 ]] || fail "the gateway key was accepted by the runtime"
echo "ok: agent key required, gateway key refused"

response=$(submit -H "Authorization: Bearer $key" -H "Idempotency-Key: $idempotency_key")
[[ $(status_of "$response") == 202 ]] || fail "submission: $(body_of "$response")"
investigation_id=$(body_of "$response" | field investigation_id)
echo "ok: accepted $investigation_id"

response=$(submit -H "Authorization: Bearer $key" -H "Idempotency-Key: $idempotency_key")
[[ $(status_of "$response") == 202 ]] || fail "resubmission: $(body_of "$response")"
[[ $(body_of "$response" | field investigation_id) == "$investigation_id" ]] ||
    fail "the idempotent resubmission created another investigation"
echo "ok: idempotent resubmission returned the same investigation"

for _ in $(seq 1 60); do
    response=$(call "$base/v1/investigations/$investigation_id" -H "Authorization: Bearer $key")
    [[ $(status_of "$response") == 200 ]] || fail "status: $(body_of "$response")"
    status=$(body_of "$response" | field status)
    [[ $status == accepted || $status == running ]] || break
    sleep 1
done

# Field checks on the result; prints what failed.
python3 -c '
import json, sys
body = json.load(sys.stdin)
result = body["result"] or {}
evidence = {item["tool"]: item["status"] for item in result.get("evidence", [])}
problems = []
if body["status"] != "completed":
    problems.append("status %s: %s" % (body["status"], result.get("failure")))
elif result["usage"]["llm_calls"] != 2:
    problems.append("llm_calls %s" % result["usage"]["llm_calls"])
# A Linux container always has procfs and statvfs. Its cgroup may lack memory
# accounting, but the read must not fail.
for tool in ("filesystem_capacity", "memory_info"):
    if evidence.get(tool) != "ok":
        problems.append("%s %s" % (tool, evidence.get(tool)))
if evidence.get("cgroup_memory") not in {"ok", "unavailable"}:
    problems.append("cgroup_memory %s" % evidence.get("cgroup_memory"))
if problems:
    sys.exit("; ".join(problems))
print("evidence: " + ", ".join("%s=%s" % item for item in evidence.items()))
' <<<"$(body_of "$response")" || fail "investigation result"
echo "ok: the worker completed the investigation through the gateway"
