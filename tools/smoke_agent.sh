#!/usr/bin/env bash
# End-to-end check of the agent runtime in a running Compose stack
# (docker compose --profile agent up --build --wait): readiness, authentication,
# and one investigation through the gateway and the mock provider, with the
# diagnostics reading the runtime's container. It costs nothing.
#
#   AGENT_API_KEYS=... AGENT_GATEWAY_API_KEY=... tools/smoke_agent.sh [base-url]
set -euo pipefail

base="${1:-http://127.0.0.1:8100}"
key="${AGENT_API_KEYS:?set AGENT_API_KEYS}"
key="${key%%,*}"
gateway_key="${AGENT_GATEWAY_API_KEY:?set AGENT_GATEWAY_API_KEY}"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

# Prints the HTTP status, then the body, of a request.
call() {
    curl -sS --max-time 60 -w '\n%{http_code}' "$@"
}

status_of() {
    tail -n 1 <<<"$1"
}

body_of() {
    sed '$d' <<<"$1"
}

investigate() {
    call "$base/v1/investigations" -H "Content-Type: application/json" "$@" \
        -d '{"incident": "The payments container restarted with exit code 137."}'
}

response=$(call "$base/health/ready")
[[ $(status_of "$response") == 200 ]] || fail "readiness: $(body_of "$response")"
echo "ok: ready"

response=$(investigate)
[[ $(status_of "$response") == 401 ]] || fail "an investigation ran without a key"
response=$(investigate -H "Authorization: Bearer $gateway_key")
[[ $(status_of "$response") == 401 ]] || fail "the gateway key was accepted by the runtime"
echo "ok: agent key required, gateway key refused"

response=$(investigate -H "Authorization: Bearer $key")
[[ $(status_of "$response") == 200 ]] || fail "investigation: $(body_of "$response")"
# Field checks on the result; prints what failed.
python3 -c '
import json, sys
result = json.load(sys.stdin)
status = {item["tool"]: item["status"] for item in result["evidence"]}
problems = []
if result["status"] != "completed":
    problems.append("status %s: %s" % (result["status"], result["failure"]))
if result["usage"]["llm_calls"] != 2:
    problems.append("llm_calls %s" % result["usage"]["llm_calls"])
# A Linux container always has procfs and statvfs. Its cgroup may lack memory
# accounting, but the read must not fail.
for tool in ("filesystem_capacity", "memory_info"):
    if status.get(tool) != "ok":
        problems.append("%s %s" % (tool, status.get(tool)))
if status.get("cgroup_memory") not in {"ok", "unavailable"}:
    problems.append("cgroup_memory %s" % status.get("cgroup_memory"))
if problems:
    sys.exit("; ".join(problems))
print("evidence: " + ", ".join("%s=%s" % item for item in status.items()))
' <<<"$(body_of "$response")" || fail "investigation result"
echo "ok: investigation completed through the gateway"
