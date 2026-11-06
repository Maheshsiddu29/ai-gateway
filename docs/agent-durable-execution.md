# Durable investigation execution

This document defines how Phase 6B runs investigations durably: the states an
investigation and each model call can be in, who may change them, and what the
runtime does after a crash. It is the specification the implementation and its
tests follow. Operating instructions are in [agent-runtime.md](agent-runtime.md).

## Goals and non-goals

- An accepted investigation is in PostgreSQL before the client is told so, and
  is finished by some worker even if the API process, a worker, or the database
  restarts.
- At most one worker advances an investigation at a time, and a worker that has
  lost ownership cannot change it.
- A model call that may have reached the gateway is **never sent again
  automatically**. The gateway does not deduplicate requests, and a stable
  `X-Request-ID` is only a correlation ID, so exactly-once execution cannot be
  provided. The runtime provides at-most-once *dispatch* per journal entry and
  records when the outcome of a dispatch is unknown.
- Spending admission is an estimate against configured prices. It is not an
  invoice bound.

## Processes

| Process | Holds | Does |
| --- | --- | --- |
| API (`agent_runtime.main`) | agent API keys, database app role | Authenticates, validates, inserts investigations, reads them. Never calls the gateway. |
| Worker (`python -m agent_runtime.worker`) | gateway key, database app role | Claims investigations, runs the LangGraph graph, journals model calls, writes results. |
| Migrations (`python -m agent_runtime.persistence`) | database owner role | Creates and upgrades the schema and grants the app role. Run explicitly, never at startup. |

Both long-running processes refuse to start unless the database schema is at the
exact version the code expects.

## Investigation states

```text
accepted ──claim──► running ──► completed
                       │  ▲  ├──► failed
                       └──┘  └──► reconciliation_required
                   (reclaim after lease expiry)
```

| Status | Invariants |
| --- | --- |
| `accepted` | No lease, no operations, no result. |
| `running` | Claimed at least once. A lease may be held, expired, or released. |
| `completed` | Terminal. `result` holds the validated diagnosis. No outstanding reservation. |
| `failed` | Terminal. `failure_code` and a `result` with the failure are set. No outstanding reservation. |
| `reconciliation_required` | Terminal. At least one model call has an unknown outcome. Its reservation stays as an unresolved liability. |

A database trigger rejects every update to a terminal row and every change of an
investigation's owner, request hash, or limits. Terminal transitions happen only
in the `finalize` stage, through a fenced write.

## Graph

LangGraph `StateGraph` with six nodes:

```text
plan ─► validate_plan ─► collect_diagnostics ─► diagnose ─► validate_diagnosis ─► finalize
  └────────┴───────────────────┴──────────────────┴─────────────────┴── on failure ──┘
```

| Node | Input | Output | External effect |
| --- | --- | --- | --- |
| `plan` | incident (read from the registry) | `plan_reply` | One journaled model call |
| `validate_plan` | `plan_reply` | `plan` (permitted tool requests) | None |
| `collect_diagnostics` | `plan` | `evidence` | Read-only local reads |
| `diagnose` | incident, `evidence` | `diagnosis_reply` | One journaled model call |
| `validate_diagnosis` | `diagnosis_reply`, `evidence` | `diagnosis` | None |
| `finalize` | everything | terminal status and result | Fenced registry write |

The validators are Phase 6A's, unchanged. Any failure sets `failure` and routes
to `finalize`. Graph state holds only JSON values (strings, numbers, lists,
mappings) and is re-validated with strict schemas when read, so a checkpoint
cannot carry objects. Checkpoints are written synchronously after every node
(`durability="sync"`) by `AsyncPostgresSaver`, with a serializer that refuses to
construct any type outside LangGraph's built-in safe list
(`allowed_msgpack_modules=None`; see CVE-2026-28277). The incident text is not
put in graph state.

A node that is interrupted runs again on resume. `validate_*` nodes are pure.
`collect_diagnostics` repeats read-only reads. Model calls are protected by the
journal, not by the checkpoint.

## Model call journal

Every gateway call is an `operations` row, written before and after the request:

```text
intent ──► dispatched ──► succeeded
  │             ├──────► failed
  │             └──────► uncertain     (found dispatched by a later owner)
  └──────► abandoned                   (never dispatched; investigation ended)
```

1. **Intent.** In one transaction: check the lease, admit the call against the
   investigation's limits, reserve its estimated cost, insert the row.
2. **Dispatch.** Commit `dispatched` before the HTTP request starts.
3. **Call** the gateway through `GatewayClient`.
4. **Outcome.** In one transaction: record the reply or the error, settle the
   reservation, and close the row.

A partial unique index allows one open (`intent` or `dispatched`) operation per
investigation. A trigger rejects any change to a closed operation.

### What a worker does with the journal on resume

| Latest operation for the stage | Action |
| --- | --- |
| none | Create an intent and dispatch it. |
| `intent` | Dispatch it. The request was never sent: dispatch is committed first. |
| `dispatched` | Mark it `uncertain` and end the investigation as `reconciliation_required` (`llm_outcome_unknown`). Never resend. |
| `succeeded` | Reuse the recorded reply. No call. |
| `failed` | Reproduce the recorded failure. No call. |
| `uncertain` | `reconciliation_required`. |

### Failures

| Event | Operation | Investigation |
| --- | --- | --- |
| Gateway error with `may_have_executed = false` | `failed`, settled at 0 | `failed` with the Phase 6A code |
| Gateway error with `may_have_executed = true` | `failed`, settled at known usage, else the full reservation | `failed` with the Phase 6A code |
| Deadline reached while dispatched | `failed` (`investigation_timeout`), settled at the full reservation | `failed` |
| Crash, kill, or lease loss while dispatched | stays `dispatched`, then `uncertain` | `reconciliation_required` |
| Crash after a successful reply, before it is recorded | same as above | `reconciliation_required` |

## Worker ownership

- **Claim.** `UPDATE … WHERE investigation_id = (SELECT … FOR UPDATE SKIP LOCKED)`
  takes an `accepted` investigation, or a `running` one whose lease has expired,
  sets `lease_owner`, `lease_expires_at = now() + lease`, and increments
  `fencing_token`. A transaction-scoped advisory lock serializes claims so the
  global limit on running investigations holds across workers.
- **Clock.** Lease expiry and investigation deadlines use PostgreSQL's `now()`.
  Workers compare nothing against their own wall clock.
- **Heartbeat.** The owner renews the lease at a third of its length. If renewal
  fails or the token changed, the worker cancels its local work. If it cannot
  renew before its lease would expire, measured on its monotonic clock with a
  margin, it also cancels, so it has stopped before another worker can claim.
- **Fencing.** Every write locks the investigation row and requires the
  worker's fencing token and `status = 'running'`. A stale worker's writes
  affect nothing.
- **Recovery limit.** Each claim increments `attempts`. An investigation claimed
  more than `AGENT_MAX_RECOVERY_ATTEMPTS` times fails with
  `recovery_attempts_exhausted`, so a crashing input cannot loop forever.
- **Shutdown.** On SIGTERM a worker stops claiming. Running investigations
  stop at the next node boundary and release their lease for another worker.
  Model calls in flight get a grace period; one that is still running when it
  ends is cancelled and its investigation fails with
  `worker_shutdown_interrupted`.

## Budgets

Recorded per investigation at acceptance: limits on model calls, diagnostics,
output tokens per call, duration (as a database-time deadline), and estimated
spending, plus a snapshot of the configured prices.

- **Admission** of a model call is one conditional `UPDATE` that requires
  `llm_calls_admitted < max_llm_calls` and
  `reserved + settled + estimate <= budget`. Concurrent workers cannot
  over-admit, and reservations survive restarts.
- **Estimate.** Input tokens are bounded by the UTF-8 byte length of the
  messages plus a per-message allowance, and output tokens by `max_tokens`.
  Each alias's price must be at least that of the most expensive model the
  gateway may route it to. An alias without a configured price cannot be
  admitted.
- **Settlement.** Known usage settles at its estimated cost. Unknown usage after
  possible execution settles at the full reservation. A reservation is released
  only when the gateway reported that the request did not run. Each operation
  settles once, in the same transaction that closes it.

| Quantity | Where | Meaning |
| --- | --- | --- |
| Workflow admission | agent `investigations` row | What the runtime let this investigation commit to. |
| Estimated cost | agent `operations` row | Reservation and settlement at snapshot prices. |
| Observed tokens | agent `operations.usage` | Token counts the gateway returned, when it did. |
| Unresolved liability | `reserved` of a `reconciliation_required` investigation | Calls whose outcome and cost are unknown. |
| Gateway budget ledger | gateway database | Authoritative per-key spending enforcement; unchanged. |
| Provider invoice | the provider | What is actually owed. None of the above guarantees it. |

## Data retention

The incident text is stored so a restarted worker can resume. It is visible only
to the agent database roles, never logged, and cleared when the investigation
becomes terminal, together with the recorded model replies and the LangGraph
checkpoints. Results, failure codes, usage, and operation metadata are kept.
Encryption at rest is the database operator's responsibility; production
connections require TLS.
