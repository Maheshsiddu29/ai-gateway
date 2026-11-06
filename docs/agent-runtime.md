# Agent runtime

`services/agent_runtime` is a separate service that runs bounded LLM workflows.
Its first workflow is a read-only investigation of a Linux incident: it asks a
model which diagnostics to run, runs the permitted ones, and asks a model for a
conclusion that must cite the evidence collected.

```text
client ──► agent runtime ──HTTP──► gateway ──► LLM providers
              │
              └── read-only diagnostics (procfs, cgroup, statvfs)
```

The runtime reaches models only through the gateway's `POST /v1/chat/completions`.
Routing, provider credentials, rate limits, budgets, retries, fallback, and LLM
metrics stay in the gateway and apply to the runtime's gateway key like any other
client. The runtime owns the workflow, the tools, validation of model output,
and its own limits and credentials. Neither package imports the other
(`tests/unit/test_import_boundaries.py`).

## Investigation workflow

1. **Plan.** The planner alias receives the tool catalog, the allowed filesystem
   paths, and the incident as a JSON string. It must reply with
   `{"steps": [{"tool", "arguments", "reason"}]}`.
2. **Validate the plan.** The reply is parsed strictly: one surrounding code fence
   is removed, nothing else is repaired, and unknown fields fail. Every step must
   name a catalog tool with arguments matching its schema, and paths must exactly
   match `AGENT_FILESYSTEM_PATHS`. Duplicate steps and plans longer than
   `AGENT_MAX_TOOL_CALLS` are rejected. Any rejection happens before any
   diagnostic runs.
3. **Collect evidence.** Permitted diagnostics run one at a time, each with a
   timeout and an output size limit, and are numbered `E1`, `E2`, ... A diagnostic
   that is unavailable or fails becomes evidence with that status. If none
   produced data, the investigation ends without a diagnosis call.
4. **Diagnose.** The diagnosis alias receives the incident and the evidence. It
   must reply with `{"conclusion", "summary", "findings", "next_steps"}`.
5. **Validate the diagnosis.** Every finding must cite at least one evidence ID,
   and each must exist and have status `ok`. A `supported` conclusion needs at
   least one finding. `next_steps` are text for an operator; the runtime has no
   way to act on them.

The checks ensure citations exist. They do not verify that a claim follows from
the cited values.

### Diagnostics

| Tool | Arguments | Reads |
| --- | --- | --- |
| `memory_info` | none | `/proc/meminfo`: total, free, available, buffers, cached, shmem, swap (bytes) |
| `cgroup_memory` | none | The runtime's own cgroup (v1 or v2): usage, limit, peak, `max`/OOM/OOM-kill counts |
| `filesystem_capacity` | `path` | `statvfs`: total, free, available, used bytes, used percent, read-only flag |
| `filesystem_inodes` | `path` | `statvfs`: total, free, available, used inodes, used percent |

Results contain only numbers and booleans under fixed keys. Status `unavailable`
means the environment does not provide the information (no procfs, no memory
controller, a filesystem that reports no inode counts). Status `failed` means it
should exist but could not be read or parsed (`permission_denied`,
`unrecognized_format`, `timeout`, `output_too_large`, `diagnostic_workers_busy`,
...).

Reads run on a dedicated pool of `AGENT_DIAGNOSTIC_WORKERS` threads, never on the
event loop's default executor, which also resolves the gateway's host name. A read
that hangs in the kernel keeps its thread after it times out; once every worker
is held, further diagnostics fail at once as `diagnostic_workers_busy` until a
hung read returns.

The tools describe the host or container the runtime runs in. In a container,
`cgroup_memory` and the filesystems are the container's, and `memory_info` shows
the kernel's view of the whole machine. Inspecting other hosts needs a remote
backend behind the `DiagnosticBackend` protocol, which is not implemented.

## API

`POST /v1/investigations` with `Authorization: Bearer <agent key>`:

```json
{"incident": "payments-api restarted three times; dmesg mentions an OOM kill."}
```

`incident` is 1 to 4000 characters; no other fields are accepted. The request
runs the investigation to completion and returns 200 with the result, whether the
investigation completed or failed:

```json
{
  "investigation_id": "inv-5f0c...",
  "status": "completed",
  "prompt_version": "linux-investigation-1",
  "plan": [{"tool": "cgroup_memory", "arguments": {}, "reason": "..."}],
  "evidence": [{"id": "E1", "tool": "cgroup_memory", "arguments": {}, "status": "ok",
                "data": {"usage_bytes": 1073741824, "limit_bytes": 1073741824, "...": 0},
                "reason": null, "duration_ms": 0.4}],
  "diagnosis": {"conclusion": "supported", "summary": "...",
                "findings": [{"claim": "...", "evidence": ["E1"]}], "next_steps": ["..."]},
  "failure": null,
  "usage": {"llm_calls": 2, "tool_calls": 1, "prompt_tokens": 812,
            "completion_tokens": 160, "duration_ms": 2410.7}
}
```

A failed investigation has `"status": "failed"`, `"diagnosis": null`, and a
`failure` naming the stage, a stable code, and, for gateway failures, the
gateway's status and error code. The plan and evidence collected before the
failure are included. `plan`, `reason`, and `diagnosis` are model-generated text.

If the client disconnects, the investigation is cancelled, including any gateway
call in flight, and the request is logged with status 499.

HTTP errors are only for requests that never started an investigation, in the
gateway's error envelope: 401 (missing or wrong agent key; gateway keys are not
accepted), 400 (invalid JSON or fields), 413, 415, 429
`concurrency_limit_exceeded` (`AGENT_MAX_CONCURRENT_INVESTIGATIONS` running), and
503 before startup completes. `GET /health/live` and `GET /health/ready` are
unauthenticated; readiness does not contact the gateway.

### Failure codes

| Stage | Codes |
| --- | --- |
| `planning` | `malformed_json`, `schema_violation`, `unknown_tool`, `invalid_tool_arguments`, `path_not_allowed`, `duplicate_tool_request`, `tool_call_limit_exceeded` |
| `diagnostics` | `no_usable_evidence` |
| `diagnosis` | `malformed_json`, `schema_violation`, `unknown_evidence_reference`, `unusable_evidence_reference`, `duplicate_evidence_reference` |
| any | `llm_call_limit_exceeded`, `investigation_timeout`, and the gateway call codes below |

Gateway call codes: `gateway_authentication_failed` (401), `gateway_request_rejected`
(other 4xx, e.g. `model_not_found` or `max_tokens_exceeds_limit`),
`gateway_rate_limited` (429), `gateway_timeout` (504 or a client timeout),
`gateway_unavailable` (other 5xx or a connection failure),
`gateway_unexpected_status`, `gateway_request_too_large`,
`gateway_response_too_large`, `gateway_malformed_response`, and, for a 200 the
runtime cannot use, `completion_truncated` (`finish_reason: length`),
`completion_refused` (`content_filter`), `completion_empty`, and
`completion_unsupported_state` (any other finish reason).

The runtime never retries a gateway call. The gateway already retries attempts
that certainly did not reach a provider. Each client error records internally
whether the gateway may have run the call (true for 5xx, timeouts after sending,
and unusable 200s), for a future retry policy.

## Configuration

All settings are environment variables; there is no configuration file. Invalid
values stop startup.

| Variable | Default | Description |
| --- | --- | --- |
| `AGENT_API_KEYS` | required | Comma-separated keys accepted from callers. |
| `AGENT_ENVIRONMENT` | `production` | Production requires 32-character keys and an `https` gateway URL. |
| `AGENT_GATEWAY_URL` | required | Gateway base URL. No credentials, query, or fragment. |
| `AGENT_GATEWAY_API_KEY` | required | A key from the gateway's `GATEWAY_API_KEYS`. Must differ from every agent key. |
| `AGENT_GATEWAY_ALLOW_INSECURE_HTTP` | `false` | Allow `http://` in production, e.g. on a private network. |
| `AGENT_PLANNER_MODEL`, `AGENT_DIAGNOSIS_MODEL` | required | Gateway model aliases for the two calls. |
| `AGENT_MAX_LLM_CALLS` | `2` | Model calls per investigation, 1 to 8. |
| `AGENT_MAX_TOOL_CALLS` | `6` | Diagnostics per investigation, 1 to 16. |
| `AGENT_MAX_OUTPUT_TOKENS` | `2048` | `max_tokens` on every call, 64 to 16384. Must not exceed the aliases' `max_output_tokens`. Reasoning tokens count toward it. |
| `AGENT_INVESTIGATION_TIMEOUT_SECONDS` | `180` | Whole-investigation deadline, at most 900. |
| `AGENT_MAX_CONCURRENT_INVESTIGATIONS` | `4` | Per process, 1 to 64. Further requests get 429. |
| `AGENT_FILESYSTEM_PATHS` | `/` | Comma-separated absolute, normalized paths the filesystem tools accept, at most 16. |
| `AGENT_TOOL_TIMEOUT_SECONDS` | `5` | Per diagnostic. |
| `AGENT_DIAGNOSTIC_WORKERS` | `4` | Threads for diagnostic reads, 1 to 16. |
| `AGENT_MAX_TOOL_OUTPUT_BYTES` | `16384` | Per diagnostic result, as JSON. |
| `AGENT_PROCFS_ROOT`, `AGENT_CGROUP_ROOT` | `/proc`, `/sys/fs/cgroup` | Where the tools read; changed in tests. |
| `AGENT_GATEWAY_CONNECT_TIMEOUT_SECONDS`, `..._READ_...`, `..._WRITE_...`, `..._POOL_...` | 5, 120, 30, 5 | httpx timeouts for gateway calls. |
| `AGENT_GATEWAY_MAX_CONNECTIONS` | `10` | Gateway connection pool size. |
| `AGENT_GATEWAY_MAX_REQUEST_BYTES`, `AGENT_GATEWAY_MAX_RESPONSE_BYTES` | 262144 each | Bounds on gateway request and response bodies. |
| `AGENT_MAX_REQUEST_BYTES` | `65536` | Bound on request bodies sent to the runtime. |
| `AGENT_LOG_LEVEL` | `INFO` | |

## Running it locally

In-process, with nothing to start:

```sh
uv sync --locked
uv run python tools/agent_demo.py "The API container restarted with exit code 137."
```

The demo builds the gateway with `config/gateway.mock.toml` and the mock
provider, whose `agent-planner` and `agent-diagnosis` aliases return a scripted
plan and diagnosis, then prints the result. The diagnostics read the machine it
runs on.

As three processes, after starting the mock provider and the gateway as in the
README's "Running without Docker":

```sh
export AGENT_API_KEYS=dev-agent-key
AGENT_ENVIRONMENT=development \
AGENT_GATEWAY_URL=http://127.0.0.1:8000 AGENT_GATEWAY_API_KEY=dev-key \
AGENT_PLANNER_MODEL=agent-planner AGENT_DIAGNOSIS_MODEL=agent-diagnosis \
  uv run uvicorn agent_runtime.main:create_app --factory --port 8100 --no-access-log &

curl -s http://127.0.0.1:8100/v1/investigations \
  -H "Authorization: Bearer $AGENT_API_KEYS" -H "Content-Type: application/json" \
  -d '{"incident": "Disk alerts on the database host."}'
```

With Docker Compose, the `agent` profile adds the runtime on 127.0.0.1:8100,
using the stack's gateway, Redis admission, and cost accounting:

```sh
export GATEWAY_API_KEYS=$(openssl rand -hex 32)
export AGENT_API_KEYS=$(openssl rand -hex 32)
export AGENT_GATEWAY_API_KEY=$GATEWAY_API_KEYS
docker compose --profile agent up --build --wait
tools/smoke_agent.sh        # readiness, authentication, one investigation
docker compose --profile agent down -v
```

The container (`agent-runtime` build target) holds only the runtime's package
and dependencies, runs as an unprivileged user with a read-only root filesystem
and no capabilities, and is attached to the frontend network only, so it reaches
the gateway but not Redis or PostgreSQL. Its diagnostics describe its own
container.

To use real models, point the two aliases at real providers in the gateway's
configuration. No real model has been evaluated on this workflow; there are no
measured figures for diagnosis quality or cost.

## Testing

```sh
uv run --directory services/agent_runtime mypy
uv run --directory services/agent_runtime pytest
uv run pytest tests/integration/test_agent_runtime_gateway.py
```

The service's own tests use an httpx transport in place of the gateway and fake
procfs and cgroup trees. The root integration tests run the runtime against the
real gateway application, with the mock provider or an upstream stub, and cover
the request contract, gateway authentication, rejections, provider errors,
truncation, refusals, timeouts, and a Redis-backed 429.

## Security

- **Separate credentials.** Callers use agent keys. The runtime holds one gateway
  key, which callers never see and which must differ from every agent key. Keys
  are compared as SHA-256 digests in constant time and logged only as
  fingerprints (`agent-key-<12 hex>`).
- **No execution.** The package contains no process creation, `eval`, or dynamic
  imports, which a static test enforces. Tools are fixed reads, and a model can
  only choose among them.
- **Model output is untrusted.** It is parsed into strict, bounded schemas.
  Tool names, arguments, paths, and evidence references are checked against what
  the runtime allows and collected. A failed check ends the investigation; it is
  never retried with looser rules.
- **Prompt injection.** The incident is passed as a JSON string, and tool output
  contains no host text. Instructions in the incident can still influence what
  the model writes, but only within the checks above: they cannot add tools,
  widen paths, or cause any action.
- **Bounded work.** Model calls, diagnostics, output tokens, request and response
  sizes, tool output, duration, and concurrency are all limited. The gateway client
  asks for uncompressed responses and refuses encoded ones, so the response
  limit bounds memory, not just bytes on the wire.
- **Logging.** Logs record IDs, counts, codes, and durations. They never include
  keys, incident text, prompts, or model output.
- **Gateway connection.** The gateway URL comes only from configuration.
  Redirects are not followed and proxy environment variables are ignored, so the
  gateway key is sent only to the gateway.

## Limitations

- Investigations are synchronous and in memory: the result is lost if the client
  disconnects, which cancels the investigation, or if the process restarts. A
  cancelled gateway call may already have run, and been charged, at the provider.
- A diagnostic that hangs in the kernel (for example `statvfs` on an unresponsive
  network mount) is abandoned at its timeout, but its worker thread runs until
  the call returns, and the interpreter waits for it on exit. In a container,
  the stop grace period then ends the process.
- Diagnostics cover only the runtime's own host or container.
- The runtime enforces per-investigation limits but no spending limit of its own;
  spending is bounded by the gateway's budgets for its key.
- The prompts are untested against real models.
