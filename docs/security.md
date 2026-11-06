# Security review

This document records the controls that exist and the risks that remain, as of
Phase 6B. It covers the gateway, the evaluation tooling, and the agent runtime. General request
handling controls are also listed in
[architecture.md](architecture.md#security-controls).

## Trust boundaries

| Party | Trusted with |
| --- | --- |
| Operator | Environment variables, the TOML configuration, the database, and running evaluations. Everything the gateway routes to comes from them. |
| API client (gateway key) | Its own completions, usage, and budgets. Clients cannot choose a provider, read another key's usage, or see routing decisions. |
| Metrics reader (metrics key) | Aggregate Prometheus counters, and nothing else. |
| Provider | Request content sent to it. Its responses are parsed defensively and its error bodies never reach clients. |
| Agent runtime caller (agent key) | Starting investigations and reading their own results. Callers cannot choose tools, paths, or models beyond what the runtime allows, cannot see another caller's investigations, and never see the runtime's gateway key. |
| Agent database (app role) | Storing investigations, their journal, and checkpoints. The role cannot delete rows from the registry or journal, nor change the schema or its triggers. |
| Agent checkpoint contents | Nothing. Graph state is JSON re-validated on every read and deserialized without constructing objects. |
| Model output (in the agent runtime) | Nothing. Plans and diagnoses are parsed and checked as untrusted input and can only select from fixed read-only diagnostics. |

## Controls

### Provider credentials

- Provider keys come only from environment variables named in the configuration
  file. The file itself may not contain secrets, and `base_url` may not embed
  credentials.
- Keys are held as `SecretStr` and never logged. httpx and httpcore loggers are
  kept at WARNING.
- The evaluation CLI reads credentials only after a live plan is approved, and
  only for the providers in that plan. Plans, journals, results files, and the
  evaluation tables contain no credentials. A test checks that a key present in
  the environment does not appear in plan output.

### Metrics authorization

- `GET /metrics` accepts only keys from `GATEWAY_METRICS_API_KEYS`. Gateway keys
  are refused, metrics keys are refused by every other endpoint, and a metrics
  key equal to a gateway key is a startup error.
- Keys are compared as SHA-256 digests with `hmac.compare_digest`, checking every
  configured key, so timing does not reveal which key matched.
- With no metrics key configured, the endpoint refuses everything.

### Evaluation cost controls

- Live evaluation needs `--live`, a cost ceiling, a request limit, and an
  `--approve` digest matching the printed plan. A change in prices, targets,
  endpoints, the dataset, or settings invalidates the approval.
- Targets need a price and a capability registry entry. Every case is checked
  against the context window, output limit, and adapter validation before any
  call.
- A running ceiling stops dispatch. Cases are sent once, never retried. Progress
  is journalled and never resumed automatically.
- The gateway process never loads the evaluation runner, plan, or CLI, so no
  request path can start an evaluation (`tests/unit/test_import_boundaries.py`).
- CI has no provider credentials. Its tests use stubs and the local mock.

### Prompt and output privacy

- Request prompts and model outputs are never logged by the gateway.
- Evaluation results record case IDs, scores, token counts, and timings. They do
  not store prompts or model output text. Journals record the same fields.
- Shipped datasets are synthetic. Real customer prompts must not be added to the
  repository; operators who evaluate on their own data keep it outside the
  repository and are responsible for its handling.

### Evaluation dataset and evidence integrity

- `evaluations/manifest.toml` pins the SHA-256 of every released dataset file,
  and a test fails if a file changes. Each run records its dataset's hash.
- Holdout partitions are checked against their development partitions for
  shared IDs and copied cases. Holdout, simulated, and incomplete runs are never
  routing evidence. The routing experiment refuses holdout runs as evidence.
- Results files are re-verified on load: the summary must match the case
  records, and impossible values are rejected. The database enforces bounds and
  refuses runs dated in the future.
- Evaluation tables are append-only (triggers reject `UPDATE` and `DELETE`).
  Recording is idempotent, a different file reusing a recorded run ID is refused,
  and new rows must carry a results digest.

### Cross-tenant boundaries

- Admission state, ledger rows, and usage queries are keyed by a principal ID
  derived from the authenticated key. Team usage needs an explicit reader grant.
- Routing uses no per-tenant state. Rollout assignment depends only on the
  caller's own principal ID, and responses do not reveal the selected model.
- Metrics carry no per-key labels.

### PostgreSQL roles and privileges

The Compose stack connects as the database owner for simplicity. A shared
deployment should separate three roles. The grants below were checked against
the migrated schema: the gateway's cost, budget, routing, and usage paths pass
with the application role, and evaluation recording passes with the recorder
role.

```sql
-- Owner: runs `alembic upgrade head`; owns the tables and triggers.
-- Gateway process:
CREATE ROLE gateway_app LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE gateway TO gateway_app;
GRANT USAGE ON SCHEMA public TO gateway_app;
GRANT SELECT, INSERT, UPDATE ON cost_attempts, budget_periods TO gateway_app;
GRANT SELECT, INSERT ON attempt_budget_periods TO gateway_app;
GRANT INSERT ON cost_events TO gateway_app;
GRANT USAGE ON SEQUENCE cost_events_event_id_seq TO gateway_app;
GRANT SELECT ON evaluation_runs TO gateway_app;

-- Whoever records evaluation results (python -m gateway.evaluation record):
CREATE ROLE evaluation_recorder LOGIN PASSWORD '...';
GRANT CONNECT ON DATABASE gateway TO evaluation_recorder;
GRANT USAGE ON SCHEMA public TO evaluation_recorder;
GRANT SELECT, INSERT ON evaluation_runs TO evaluation_recorder;
GRANT INSERT ON evaluation_case_results TO evaluation_recorder;
```

With these grants the gateway cannot insert evidence, and neither role can
alter or drop the append-only triggers. The owner role can, so keep it for
migrations only.

### Operator configuration

- Configuration is validated in full at startup. Unknown keys, unpriced routed
  models, capabilities an adapter cannot honour, and inconsistent limits stop the
  gateway from starting.
- Production mode requires 32-character gateway and metrics keys, HTTPS
  providers (unless explicitly allowed per provider), and TLS for Redis.

### HTTP request validation

- Bodies are size-limited, including chunked bodies. The schema is strict:
  unknown fields are rejected, and the counts and ranges of messages, stop
  sequences, and numeric fields are bounded.
- Upstream requests are built from scratch. Client headers are never forwarded,
  redirects are not followed, and proxy environment settings are ignored.

### Failure and retry amplification

- Retries happen only when a provider certainly did not run the request, with at
  most five attempts per provider and three fallbacks. Every attempt is bounded by
  the request deadline. Routed requests try at most four candidates.
- The evaluation runner never retries.

### Local onboarding secrets

- The quickstart generates keys with `openssl rand` into the shell. `.env` and
  `config/gateway.toml` are ignored by Git, and gitleaks scans history in CI.
- The Compose PostgreSQL password is a fixed local-only value on an internal
  network. Neither PostgreSQL nor Redis is published to the host.

### Resource exhaustion during evaluations

- Datasets are limited to 5000 cases and 64 KiB per line. Concurrency is at most
  16, each call has a timeout of at most 600 seconds, and live runs need an
  explicit request limit. Repetitions in the routing experiment are capped at 10.

### Agent runtime

[agent-runtime.md](agent-runtime.md#security) lists the controls: separate
agent keys, a gateway key that must differ from them, no process creation or
code evaluation (checked by a static test), fixed read-only diagnostics, allowed
paths matched exactly, strict schemas for model output, evidence references
checked against collected evidence, limits on every resource, and logs without
incident text, prompts, or output.

Release review of Phase 6A added four controls:

- Diagnostics run on their own bounded thread pool, so reads that hang in the
  kernel cannot block the default executor that resolves the gateway's host
  name. When the pool is full of hung reads, diagnostics fail immediately.
- A client disconnect cancels its investigation and any gateway call in flight,
  releasing the concurrency slot.
- The gateway client refuses compressed responses, so the response size limit
  bounds memory.
- In Compose, the runtime's only network is shared with the gateways, so it
  cannot reach the provider, Redis, or PostgreSQL directly.

Phase 6B split the runtime into an API and a durable worker:

- The API holds the agent keys and no gateway key; the worker holds the gateway
  key and no agent keys. Neither can change the database schema: only the
  migration command, run with the owner's credentials, can.
- The agent's persistence is a separate database from the gateway's billing
  tables, on its own internal Compose network. The worker has no outbound
  access and cannot reach the mock provider, Redis, or the gateway's database.
- Checkpoints are deserialized with `allowed_msgpack_modules=None` and no
  pickle (CVE-2026-28277), and graph state is re-validated when read, so write
  access to the checkpoint tables cannot make the runtime construct an object or
  run an unpermitted tool.
- A model call whose outcome is unknown is never repeated, which bounds
  duplicate provider spending to the gateway's own budget for the runtime's key.
- The worker refuses to start with LangSmith tracing enabled, which would export
  prompts containing incident text.

## Open risks

- **The cost ceiling is not an invoice bound.** It limits what the CLI sends,
  using estimated input tokens. A provider that counts more input tokens, or
  charges for dimensions the gateway does not price, can exceed it. Calls in
  flight when the ceiling is reached are not cancelled.
- **Approval is a digest, not an identity.** Anyone who can run the CLI with
  provider credentials can approve a plan. Access to production provider keys is
  the real control.
- **Clock skew.** `record` refuses runs completed after the database clock. A
  machine whose clock runs ahead must wait before recording. A database whose
  clock runs ahead would accept runs that appear older than they are.
- **Evidence trust.** Anyone with `INSERT` on `evaluation_runs` can add evidence.
  The digest and constraints catch accidental corruption, not a deliberate
  insertion of fabricated results.
- **Static keys.** Gateway and metrics keys are environment lists, changed only
  by restart. There is no per-key expiry, scope beyond the two roles, or
  revocation without restart.
- **Shared breaker state across tenants.** One tenant's failures can open a
  provider's circuit breaker for everyone, as documented since Phase 2.
- **Local Compose runs as the database owner**, which can drop triggers. It is
  not a deployment template.
- **Agent runtime: shared gateway key.** Every investigation uses one gateway key,
  so the gateway's limits and budgets apply to all runtime callers together. One
  caller can use up the key's budget for the others.
- **Agent runtime: grounded is not correct.** The runtime checks that findings
  cite collected evidence, not that the claims are true. Diagnoses and
  `next_steps` are model text and must be reviewed before anyone acts on them.
- **Agent runtime: host information.** Diagnostics reveal memory sizes, cgroup
  limits, and filesystem usage of the runtime's host to its callers and to the
  model provider.
- **Agent runtime: outbound traffic.** The Compose network that publishes the
  runtime's port does not block outbound connections. Nothing in the runtime
  makes them, but a compromised container could.
- **Agent runtime: hung diagnostic threads.** Their threads cannot be killed.
  Enough of them make every diagnostic fail as `diagnostic_workers_busy` until
  they return or the process restarts.
- **Agent runtime: stored incident text.** An accepted investigation's incident
  text, and recorded model replies, live in the agent database until the
  investigation is terminal. Encryption at rest is the operator's.
- **Agent runtime: unresolved provider executions.** A model call interrupted
  after dispatch may have run and been charged. The investigation ends as
  `reconciliation_required` and a human must compare it with the gateway's
  ledger; the runtime never resolves it automatically.
- **Agent runtime: estimated spending is not an invoice bound.** Admission uses
  configured prices and a token upper bound. An alias priced below the model the
  gateway routes it to, or a provider counting more input tokens, exceeds the
  ceiling.
- **Agent runtime: one shared gateway key.** Every investigation uses it, so the
  gateway's limits and budgets for that key are shared by all callers. The API
  can no longer check that this key differs from the agent keys, because they are
  held by different processes; keeping them distinct is an operator requirement.
