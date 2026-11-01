# Security review

This document records the controls that exist and the risks that remain, as of
Phase 5. It covers the gateway and the evaluation tooling. General request
handling controls are also listed in
[architecture.md](architecture.md#security-controls).

## Trust boundaries

| Party | Trusted with |
| --- | --- |
| Operator | Environment variables, the TOML configuration, the database, and running evaluations. Everything the gateway routes to comes from them. |
| API client (gateway key) | Its own completions, usage, and budgets. Clients cannot choose a provider, read another key's usage, or see routing decisions. |
| Metrics reader (metrics key) | Aggregate Prometheus counters, and nothing else. |
| Provider | Request content sent to it. Its responses are parsed defensively and its error bodies never reach clients. |

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
