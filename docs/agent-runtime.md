# Agent runtime

`services/agent_runtime` is a separate service that runs bounded LLM workflows.
Its first workflow is a read-only investigation of a Linux incident: it asks a
model which diagnostics to run, runs the permitted ones, and asks a model for a
conclusion that must cite the evidence collected.

```text
client ──► investigation API ──► PostgreSQL ◄── durable worker ──HTTP──► gateway ──► providers
                                                     │
                                                     └── read-only diagnostics
```

Investigations are durable: the API stores one and answers 202, and a separate
worker runs it. Either process can restart without losing it. The states, the
recovery rules, and what is never repeated after a crash are specified in
[agent-durable-execution.md](agent-durable-execution.md).

The runtime reaches models only through the gateway's `POST /v1/chat/completions`.
Routing, provider credentials, rate limits, budgets, retries, fallback, and LLM
metrics stay in the gateway and apply to the runtime's gateway key like any other
client. The runtime owns the workflow, the tools, validation of model output,
its own persistence, and its own limits and credentials. Neither package imports
the other (`tests/unit/test_import_boundaries.py`).

| Process | Holds | Does |
| --- | --- | --- |
| `agent_runtime.main` (API) | agent API keys, the database app role | Accepts, stores, and reads investigations. Never calls the gateway. |
| `agent_runtime.worker` | the gateway key, the database app role | Claims and runs investigations, journals model calls. |
| `agent_runtime.persistence` | the database owner role | Creates and upgrades the schema. Run explicitly, never at startup. |

Both long-running processes refuse to start unless the schema is at exactly the
version they expect.

## Investigation workflow

A LangGraph `StateGraph` with one node per step, checkpointed in PostgreSQL after
each one, so a restarted worker resumes at the node it had reached:

```text
plan ─► validate_plan ─► collect_diagnostics ─► diagnose ─► validate_diagnosis ─► finalize
  └────────┴───────────────────┴──────────────────┴──────────────┴──── on failure ────┘
```

1. **Plan** (`plan`). The planner alias receives the tool catalog, the allowed filesystem
   paths, and the incident as a JSON string. It must reply with
   `{"steps": [{"tool", "arguments", "reason"}]}`.
2. **Validate the plan** (`validate_plan`). The reply is parsed strictly: one surrounding code fence
   is removed, nothing else is repaired, and unknown fields fail. Every step must
   name a catalog tool with arguments matching its schema, and paths must exactly
   match `AGENT_FILESYSTEM_PATHS`. Duplicate steps and plans longer than
   `AGENT_MAX_TOOL_CALLS` are rejected. Any rejection happens before any
   diagnostic runs.
3. **Collect evidence** (`collect_diagnostics`). Permitted diagnostics run one at a time, each with a
   timeout and an output size limit, and are numbered `E1`, `E2`, ... A diagnostic
   that is unavailable or fails becomes evidence with that status. If none
   produced data, the investigation ends without a diagnosis call.
4. **Diagnose** (`diagnose`). The diagnosis alias receives the incident and the evidence. It
   must reply with `{"conclusion", "summary", "findings", "next_steps"}`.
5. **Validate the diagnosis** (`validate_diagnosis`). Every finding must cite at least one evidence ID,
   and each must exist and have status `ok`. A `supported` conclusion needs at
   least one finding. `next_steps` are text for an operator; the runtime has no
   way to act on them.
6. **Finalize** (`finalize`). The result and its terminal status are written in
   one transaction that checks the worker still owns the investigation.

The checks ensure citations exist. They do not verify that a claim follows from
the cited values. Each model call is journaled before and after it is sent, so a
call whose outcome is unknown after a crash is never repeated; the stage logic
and validators are Phase 6A's, unchanged.

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
`unrecognized_format`, `file_not_found`, `read_failed`, `timeout`,
`output_too_large`, `diagnostic_workers_busy`).

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

### Accept an investigation

`POST /v1/investigations` with `Authorization: Bearer <agent key>`, and
optionally `Idempotency-Key`:

```json
{"incident": "payments-api restarted three times; dmesg mentions an OOM kill."}
```

`incident` is 1 to 4000 characters; no other fields are accepted. The response is
202, sent only after the investigation is committed, so a client that disconnects
afterwards does not affect it. This replaces Phase 6A's synchronous 200 with the
finished result: a client now polls, or reads the `Location` header.

```json
{
  "investigation_id": "inv-5f0c...", "status": "accepted", "stage": null,
  "created_at": "2026-11-06T02:37:47.351Z", "updated_at": "2026-11-06T02:37:47.351Z",
  "completed_at": null,
  "spending": {"budget_micro_usd": 100000, "reserved_micro_usd": 0, "settled_micro_usd": 0},
  "result": null
}
```

An `Idempotency-Key` (1 to 128 characters from `A-Z a-z 0-9 . _ : -`) is scoped to
the authenticated caller. Resubmitting the same body with the same key returns the
original investigation with `Idempotent-Replayed: true`; the same key with a
different body returns 409 `idempotency_key_reused`. Concurrent submissions with
one key create one investigation.

### Read an investigation

`GET /v1/investigations/{investigation_id}` returns the same shape. `status` is
`accepted`, `running`, `completed`, `failed`, or `reconciliation_required`, and
`stage` is the graph node last entered. When the investigation is finished,
`result` holds the Phase 6A result object:

```json
{
  "investigation_id": "inv-5f0c...", "status": "completed", "stage": "finalize",
  "spending": {"budget_micro_usd": 100000, "reserved_micro_usd": 0, "settled_micro_usd": 559},
  "result": {
    "investigation_id": "inv-5f0c...", "status": "completed",
    "prompt_version": "linux-investigation-1",
    "plan": [{"tool": "cgroup_memory", "arguments": {}, "reason": "..."}],
    "evidence": [{"id": "E1", "tool": "cgroup_memory", "arguments": {}, "status": "ok",
                  "data": {"usage_bytes": 1073741824, "limit_bytes": 1073741824},
                  "reason": null, "duration_ms": 0.4}],
    "diagnosis": {"conclusion": "supported", "summary": "...",
                  "findings": [{"claim": "...", "evidence": ["E1"]}], "next_steps": ["..."]},
    "failure": null,
    "usage": {"llm_calls": 2, "tool_calls": 1, "prompt_tokens": 812,
              "completion_tokens": 160, "duration_ms": 2410.7}
  }
}
```

A failed investigation has `"status": "failed"`, `"diagnosis": null` inside
`result`, and a `failure` naming the stage, a stable code, and, for gateway
failures, the gateway's status and error code. The plan and evidence collected
before the failure are included. `plan`, `reason`, and `diagnosis` are
model-generated text. `reconciliation_required` means at least one model call's
outcome is unknown: see [below](#investigations-needing-reconciliation).

A caller reads only its own investigations. Another caller's ID is answered
exactly like an unknown one, 404 `investigation_not_found`, so the API never
reveals that it exists.

Other HTTP errors, in the gateway's error envelope: 401 `missing_api_key` or
`invalid_api_key` (gateway keys are not accepted), 400 `invalid_json`,
`invalid_parameter`, `unknown_parameter`, `invalid_idempotency_key`, or
`invalid_header`, 413 `request_too_large`, 415 `unsupported_media_type`,
429 `active_investigation_limit`
(`AGENT_MAX_ACTIVE_INVESTIGATIONS_PER_PRINCIPAL` unfinished), 503
`persistence_unavailable` when the database cannot be reached, and 503
`not_ready` before startup completes. `GET /health/live` and `GET /health/ready`
are unauthenticated; readiness checks the database and does not contact the
gateway.

### Failure codes

| Stage | Codes |
| --- | --- |
| `planning` | `malformed_json`, `schema_violation`, `unknown_tool`, `invalid_tool_arguments`, `path_not_allowed`, `duplicate_tool_request`, `tool_call_limit_exceeded` |
| `diagnostics` | `no_usable_evidence`, `tool_call_limit_exceeded` |
| `diagnosis` | `malformed_json`, `schema_violation`, `unknown_evidence_reference`, `unusable_evidence_reference`, `duplicate_evidence_reference` |
| any | `llm_call_limit_exceeded`, `budget_exhausted`, `price_unknown`, `investigation_timeout`, `worker_shutdown_interrupted`, `recovery_attempts_exhausted`, `checkpoint_invalid`, `llm_outcome_unknown`, and the gateway call codes below |

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
that certainly did not reach a provider. Each error records whether the gateway
may have run the call (true for 5xx, timeouts after sending, and unusable 200s),
which decides how its reservation settles.

### Investigations needing reconciliation

An investigation ends as `reconciliation_required` with `llm_outcome_unknown`
when a model call was marked dispatched but no outcome was ever recorded, which
happens if a worker is killed, or loses its lease, between committing the
dispatch and committing the reply. The call may have run and been charged at the
provider, so the runtime never repeats it.

Such an investigation is terminal, holds `reserved_micro_usd` as an unresolved
liability, and needs a human: compare its operations with the gateway's ledger
for the matching `X-Request-ID` (`<investigation_id>-<plan|diagnose>-1`), then
submit a new investigation if the incident still needs answering. The runtime
provides no automatic reconciliation.

```sql
-- Investigations awaiting reconciliation, with the calls whose outcome is unknown.
SELECT i.investigation_id, i.completed_at, i.reserved_micro_usd,
       o.operation_type, o.gateway_request_id, o.dispatched_at
  FROM investigations i JOIN operations o USING (investigation_id)
 WHERE i.status = 'reconciliation_required' AND o.state = 'uncertain'
 ORDER BY i.completed_at DESC;
```


## Configuration

All settings are environment variables; there is no configuration file. Invalid
values stop startup. Each process reads only what it needs, so neither holds the
other's credentials.

### Both processes

| Variable | Default | Description |
| --- | --- | --- |
| `AGENT_ENVIRONMENT` | `production` | Production requires 32-character keys, an `https` gateway URL, and TLS to the database. |
| `AGENT_DATABASE_URL` | required | `postgresql://` URL for the unprivileged application role. It may not set `options`; the runtime pins `search_path` itself. |
| `AGENT_DATABASE_SCHEMA` | `agent_runtime` | The schema holding the runtime's tables and LangGraph's checkpoints. |
| `AGENT_DATABASE_ALLOW_INSECURE` | `false` | Allow a URL without `sslmode` in production, e.g. on a private network. |
| `AGENT_DATABASE_POOL_SIZE` | `10` | Connections per process, 2 to 100. |
| `AGENT_DATABASE_TIMEOUT_SECONDS` | `10` | Wait for a pooled connection. |
| `AGENT_LOG_LEVEL` | `INFO` | |

### The API (`agent_runtime.main`)

| Variable | Default | Description |
| --- | --- | --- |
| `AGENT_API_KEYS` | required | Comma-separated keys accepted from callers. |
| `AGENT_PLANNER_MODEL`, `AGENT_DIAGNOSIS_MODEL` | required | Gateway model aliases, recorded on each investigation with their prices. |
| `AGENT_MODEL_PRICES` | required | JSON mapping each alias to `{"input_micro_usd_per_million_tokens", "output_micro_usd_per_million_tokens"}`. Startup fails if either alias is unpriced. |
| `AGENT_MAX_INVESTIGATION_COST_MICRO_USD` | `100000` | Estimated spending ceiling per investigation ($0.10). |
| `AGENT_MAX_LLM_CALLS` | `2` | Model calls per investigation, 1 to 8. |
| `AGENT_MAX_TOOL_CALLS` | `6` | Diagnostics per investigation, 1 to 16. |
| `AGENT_MAX_OUTPUT_TOKENS` | `2048` | `max_tokens` on every call, 64 to 16384. Must not exceed the aliases' `max_output_tokens`. Reasoning tokens count toward it. |
| `AGENT_INVESTIGATION_TIMEOUT_SECONDS` | `180` | Whole-investigation deadline, at most 900, measured on database time from acceptance. |
| `AGENT_MAX_ACTIVE_INVESTIGATIONS_PER_PRINCIPAL` | `10` | Unfinished investigations one caller may have, 1 to 10000. More are refused with 429. |
| `AGENT_MAX_REQUEST_BYTES` | `65536` | Bound on request bodies sent to the runtime. |

The five limit settings are recorded on each investigation when it is accepted
and are not re-read while it runs, so changing them does not affect work in
flight.

### The worker (`agent_runtime.worker`)

| Variable | Default | Description |
| --- | --- | --- |
| `AGENT_GATEWAY_URL` | required | Gateway base URL. No credentials, query, or fragment. |
| `AGENT_GATEWAY_API_KEY` | required | A key from the gateway's `GATEWAY_API_KEYS`. It must not be one of `AGENT_API_KEYS`; since Phase 6B the two live in different processes, so this is an operator requirement the runtime cannot check. |
| `AGENT_GATEWAY_ALLOW_INSECURE_HTTP` | `false` | Allow `http://` in production, e.g. on a private network. |
| `AGENT_PLANNER_MODEL`, `AGENT_DIAGNOSIS_MODEL` | required | The aliases it calls. They must match the API's, or its investigations are never claimed. |
| `AGENT_WORKER_CONCURRENCY` | `2` | Investigations one worker runs at once, 1 to 32. |
| `AGENT_MAX_RUNNING_INVESTIGATIONS` | `8` | Live leases across all workers, enforced when claiming, 1 to 1024. |
| `AGENT_LEASE_SECONDS` | `30` | Lease length, 3 to 600. Renewed at a third of it. |
| `AGENT_POLL_SECONDS` | `1` | Idle polling interval. |
| `AGENT_SHUTDOWN_GRACE_SECONDS` | `20` | After SIGTERM, how long a model call in flight may finish. Keep the container's stop grace period above it. |
| `AGENT_MAX_RECOVERY_ATTEMPTS` | `3` | Claims of one investigation before it fails as `recovery_attempts_exhausted`, 1 to 20. |
| `AGENT_WORKER_ID` | hostname, pid, random | Recorded as the lease owner. |
| `AGENT_FILESYSTEM_PATHS` | `/` | Comma-separated absolute, normalized paths the filesystem tools accept, at most 16. |
| `AGENT_TOOL_TIMEOUT_SECONDS` | `5` | Per diagnostic. |
| `AGENT_DIAGNOSTIC_WORKERS` | `4` | Threads for diagnostic reads, 1 to 16. |
| `AGENT_MAX_TOOL_OUTPUT_BYTES` | `16384` | Per diagnostic result, as JSON. |
| `AGENT_PROCFS_ROOT`, `AGENT_CGROUP_ROOT` | `/proc`, `/sys/fs/cgroup` | Where the tools read; changed in tests. |
| `AGENT_GATEWAY_CONNECT_TIMEOUT_SECONDS`, `..._READ_...`, `..._WRITE_...`, `..._POOL_...` | 5, 120, 30, 5 | httpx timeouts for gateway calls. |
| `AGENT_GATEWAY_MAX_CONNECTIONS` | `10` | Gateway connection pool size. |
| `AGENT_GATEWAY_MAX_REQUEST_BYTES`, `AGENT_GATEWAY_MAX_RESPONSE_BYTES` | 262144 each | Bounds on gateway request and response bodies. |

LangSmith tracing would export prompts containing incident text, so the worker
refuses to start when `LANGSMITH_TRACING`, `LANGSMITH_TRACING_V2`,
`LANGCHAIN_TRACING`, or `LANGCHAIN_TRACING_V2` is enabled.

### Migrations (`agent_runtime.persistence`)

| Variable | Default | Description |
| --- | --- | --- |
| `AGENT_DATABASE_OWNER_URL` | required | The schema owner's connection. Used only by this command. |
| `AGENT_DATABASE_APP_ROLE` | none | Granted the application privileges after each upgrade. |
| `AGENT_DATABASE_SCHEMA` | `agent_runtime` | The schema to create or upgrade. |

```sh
python -m agent_runtime.persistence status
python -m agent_runtime.persistence upgrade [--grant-to ROLE]
python -m agent_runtime.persistence downgrade --to VERSION [--allow-data-loss]
```

`upgrade` applies the runtime's SQL migrations and LangGraph's checkpoint tables,
each in a transaction, serialized across processes by an advisory lock, and
records a checksum per migration. `downgrade` is refused while any investigation
is unfinished, and deleting stored investigations needs `--allow-data-loss`. The
API and worker only verify the schema: they refuse to start if it is behind,
ahead, or altered, and they cannot change it.

### Spending, and what the numbers mean

`spending` in the API is estimated admission control, not billing. Six distinct
quantities are involved; only the last is what is actually owed:

| Quantity | Where | Meaning |
| --- | --- | --- |
| Workflow admission | `budget_micro_usd` | What the runtime let this investigation commit to. |
| Estimated cost | `operations.reserved_micro_usd` | Worst case at the recorded prices: UTF-8 bytes plus an allowance per message for input, `max_tokens` for output. |
| Observed tokens | `operations.usage` | What the gateway reported, when it reported anything. |
| Settled | `settled_micro_usd` | Known usage at the recorded prices, or the full reservation when usage is unknown and the call may have run. |
| Unresolved liability | `reserved_micro_usd` of a `reconciliation_required` investigation | Calls whose outcome and cost are unknown. |
| Gateway budget ledger | the gateway's database | Authoritative per-key enforcement; unchanged by Phase 6B. |

A price must be at least that of the most expensive model the gateway may route
the alias to. The runtime cannot see the gateway's routing decision, so it cannot
check this: set the prices accordingly. An alias without a recorded price is
never admitted, so unknown cost is never treated as zero.

## Running it locally

### The demonstration, with nothing to start

```sh
uv sync --locked
uv run python tools/agent_demo.py "The API container restarted with exit code 137."
```

The demo builds the gateway with `config/gateway.mock.toml` and the mock
provider, whose `agent-planner` and `agent-diagnosis` aliases return a scripted
plan and diagnosis, runs the graph in memory, and prints the result. It needs no
network and no database, so it exercises the stages but not durability. The
diagnostics read the machine it runs on.

### The durable stack with Docker Compose

The `agent` profile adds the runtime's own PostgreSQL, a migration job, the
investigation API on 127.0.0.1:8100, and a durable worker, alongside the stack's
gateway, Redis admission, and cost accounting:

```sh
export GATEWAY_API_KEYS=$(openssl rand -hex 32)
export GATEWAY_METRICS_API_KEYS=$(openssl rand -hex 32)
export AGENT_API_KEYS=$(openssl rand -hex 32)
export AGENT_GATEWAY_API_KEY=$GATEWAY_API_KEYS
docker compose --profile agent up --build --wait
tools/smoke_agent.sh   # readiness, authentication, one investigation, an idempotent retry
docker compose --profile agent down -v
```

Both services use the `agent-runtime` build target: only the runtime's package
and dependencies, an unprivileged user, a read-only root filesystem, and no
capabilities. Their networks were verified from inside the containers:

| Service | Networks | Reaches | Cannot reach |
| --- | --- | --- | --- |
| `agent-runtime` (API) | `agent-api`, `agent-data` | its database | the gateway, the mock provider, Redis, the gateway's database |
| `agent-worker` | `agent` (internal), `agent-data` | the gateway, its database | the mock provider, Redis, the gateway's database, the internet |

`agent` and `agent-data` are internal, so the worker has no outbound access. The
API's network is not internal, because its port is published to the host; it
holds no gateway or provider credentials. The worker's diagnostics describe its
own container.

Migrations run as `agent_owner` in the one-shot `agent-migrate` job;
`config/agent-postgres/10-app-role.sh` creates the unprivileged `agent_app` role
that the API and worker use. `agent_app` has `SELECT`, `INSERT`, and `UPDATE` on
the registry and journal (no `DELETE`), full access to LangGraph's checkpoint
tables, and no schema privileges. A test asserts that it cannot delete
investigations, disable the triggers, or alter the schema.

### Scaling and operations

Run more workers for more throughput; `AGENT_MAX_RUNNING_INVESTIGATIONS` caps
live leases across all of them. Deploy order matters: run
`python -m agent_runtime.persistence upgrade` first, because the API and worker
refuse to start against a schema that is not exactly their version. Rolling a
new `WORKFLOW_VERSION` is safe: a worker claims only investigations written by
its own version, so old ones wait for a worker that still understands them, or
are failed by an operator.

Stopping a worker with SIGTERM releases its investigations at the next stage
boundary, so another worker continues them; keep the container's stop grace
period above `AGENT_SHUTDOWN_GRACE_SECONDS`. Investigations whose worker
disappears are picked up again once their lease expires, at most
`AGENT_LEASE_SECONDS` later.

Worth watching: investigations in `reconciliation_required` (each needs a human),
`recovery_attempts_exhausted` and `checkpoint_invalid` failures (a crash loop or
corrupted state), investigations `accepted` for longer than a lease (no worker is
claiming), and `reserved_micro_usd` on terminal rows (unresolved liability).

To use real models, point the two aliases at real providers in the gateway's
configuration and set `AGENT_MODEL_PRICES` to at least their prices. No real
model has been evaluated on this workflow; there are no measured figures for
diagnosis quality or cost.

## Testing

```sh
# A PostgreSQL server whose user may create schemas and roles.
export AGENT_TEST_DATABASE_URL=postgresql://agent_owner:...@127.0.0.1:5433/agent_test
export AGENT_REQUIRE_DATABASE_TESTS=1   # fail instead of skipping
uv run --directory services/agent_runtime mypy
uv run --directory services/agent_runtime pytest
uv run pytest tests/integration/test_agent_runtime_gateway.py
```

Each database test creates and drops its own schema. Without
`AGENT_TEST_DATABASE_URL` the database tests skip, which `AGENT_REQUIRE_DATABASE_TESTS=1`
turns into a failure, as in CI.

The service's tests cover the schema and its guards, the application role's
privileges, checkpoints (including that a planted object in a checkpoint is never
instantiated), the journal, leases and fencing, budgets, and the API. The
crash-recovery tests (`tests/integration/test_crash_recovery.py`) SIGKILL real
worker subprocesses at each point in the lifecycle and assert the request count
at a recording gateway, so a repeated model call would fail the test. The root
integration tests run the runtime against the real gateway application, with the
mock provider or an upstream stub, and cover the request contract, gateway
authentication, rejections, provider errors, truncation, refusals, timeouts, a
Redis-backed 429, and one durable investigation end to end.

## Security

- **Separate credentials and processes.** Callers use agent keys, held only by
  the API. The gateway key is held only by the worker, and callers never see it.
  Both connect to the database as an unprivileged role; only the migration
  command uses the owner. Keys are compared as SHA-256 digests in constant time
  and logged only as fingerprints (`agent-key-<12 hex>`).
- **No execution.** The package contains no process creation, `eval`, or dynamic
  imports, which a static test enforces. Tools are fixed reads, and a model can
  only choose among them. A model never reaches the database: it only returns
  JSON, which the runtime validates and then acts on itself.
- **Model output is untrusted.** It is parsed into strict, bounded schemas.
  Tool names, arguments, paths, and evidence references are checked against what
  the runtime allows and collected. A failed check ends the investigation; it is
  never retried with looser rules.
- **Checkpoints are untrusted.** Graph state holds only JSON values and is
  re-validated on every read, and checkpoints are deserialized with
  `allowed_msgpack_modules=None` and no pickle (CVE-2026-28277), so a checkpoint
  cannot carry an object. Invalid state ends the investigation with
  `checkpoint_invalid`.
- **Prompt injection.** The incident is passed as a JSON string, and tool output
  contains no host text. Instructions in the incident can still influence what
  the model writes, but only within the checks above: they cannot add tools,
  widen paths, reach the database, or cause any action.
- **Bounded work.** Model calls, diagnostics, output tokens, request and response
  sizes, tool output, duration, concurrent investigations per caller, live leases,
  recovery attempts, and estimated spending are all limited. The gateway client
  asks for uncompressed responses and refuses encoded ones, so the response
  limit bounds memory, not just bytes on the wire.
- **Logging.** Logs record IDs, counts, codes, and durations. They never include
  keys, incident text, prompts, or model output. The worker refuses to start with
  LangSmith tracing enabled, because it would export prompts.
- **Gateway connection.** The gateway URL comes only from configuration.
  Redirects are not followed and proxy environment variables are ignored, so the
  gateway key is sent only to the gateway.

### Stored incident text

The incident is stored so a restarted worker can resume, and recorded model
replies are stored so a resumed worker need not call again. Both are deleted when
the investigation becomes terminal, together with its checkpoints. Results,
failure codes, usage, and operation metadata are kept indefinitely; there is no
retention job for them. The data is reachable only by the agent database roles
and never logged. Encryption at rest is the database operator's responsibility;
production connections require TLS.

## Limitations

- **Exactly-once execution is not provided, and cannot be.** The gateway does not
  deduplicate requests, so a model call whose outcome is unknown is never
  repeated and its investigation ends as `reconciliation_required`, needing a
  human. The runtime guarantees at-most-once dispatch per journal entry, nothing
  stronger.
- **Spending figures are estimates.** They bound what the runtime admits at
  configured prices, not what a provider invoices. A provider that counts more
  input tokens than the runtime's byte bound, or an alias priced below the model
  the gateway routes it to, can exceed the ceiling. Unknown usage is charged at
  the full estimate, so settled amounts may overstate the real cost.
- **A crash between committing a dispatch and sending the request** is treated
  like a lost reply, because the journal cannot tell them apart. Such an
  investigation needs reconciliation even though nothing was sent.
- **Recovery repeats work inside a stage.** Diagnostics are re-read when a stage
  runs again, which is harmless because they only read, but the elapsed time and
  attempt count grow.
- A diagnostic that hangs in the kernel (for example `statvfs` on an unresponsive
  network mount) is abandoned at its timeout, but its thread runs until the call
  returns, and the interpreter waits for it on exit. In a container, the stop
  grace period then ends the process.
- Diagnostics cover only the runtime's own container.
- There is no cancellation API: an accepted investigation runs until it finishes
  or reaches its deadline. There is no listing endpoint; a caller needs the ID it
  was given.
- The API cannot check that the gateway key differs from the agent keys, because
  the two now live in different processes. Keep them different.
- Per-caller spending is not separated: all investigations use one gateway key,
  so the gateway's budget for that key is shared.
- The prompts are untested against real models.
