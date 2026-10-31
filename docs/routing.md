# Routing

An alias can choose among several upstream models per request. The choice is made
by a configured policy, from facts the gateway already has: what each model
supports, what the request needs, the current price of each model, circuit-breaker
state, and quality measured offline on a task-specific dataset. No model is asked
to classify or route requests, and nothing is decided at random.

Routing is off unless `[routing] enabled = true`. An alias without a `routing`
table, or with `policy = "fixed"`, behaves exactly as in Phase 3.

## Policies

| Policy | Chooses | Quality guarantee |
| --- | --- | --- |
| `fixed` | The alias's primary model, then its configured `fallbacks`. | None beyond operator choice. |
| `lowest_cost` | The cheapest capable, available candidate. | None. It can pick a model that does the task badly. |
| `quality_constrained` | The cheapest candidate whose recorded evaluation passes the alias's thresholds. | The selected model's measured success rate on the alias's dataset passed the configured threshold at the 95% Wilson lower bound. That describes the dataset, not each live request. |

The primary model is always a candidate. `candidates` lists up to seven more.
Dynamic policies require `[cost] enabled = true`, because they rank by price and
every routed attempt must be reserved against budgets at its own price.

### When nothing qualifies

For `quality_constrained`, `on_insufficient_evidence` decides what happens when no
candidate passes the rules:

- `primary` (default): use the alias's primary model, as `fixed` would. The log
  records `routing_reason = no_qualified_candidate` and why each candidate was
  excluded. This keeps the alias available when evidence is missing, stale, or
  failing. It does not claim the primary is qualified.
- `reject`: refuse with 502 `provider_unavailable` / `no_eligible_route` before any
  budget reservation or provider call.

The primary is used only if it can run the request (see capability filtering). If
it cannot, the request fails the same way as under `reject`.

### Rollout

`rollout_percent` (default 100) applies the policy to that share of gateway keys.
A key is assigned by hashing the alias and the key's principal ID, so the same key
always gets the same treatment. Keys outside the share use the primary model and
are logged with `routing_reason = rollout_control`. There is no shadow traffic: a
request is never sent to a second model for comparison.

## Decision lifecycle

1. The request is authenticated and validated as before. The alias's output
   allowance (`max_tokens`, `default_max_tokens`, or `max_output_tokens`) is
   applied. A `max_tokens` above the alias's ceiling is still rejected.
2. The gateway builds one provider request. Every candidate receives this same
   request, differing only in the upstream model name.
3. For each candidate, in configuration order (primary first):
   1. **Capabilities.** The request's needs (`streaming`, `stream_usage`,
      `temperature`, `top_p`, `stop`) must be a subset of the candidate's registry
      `capabilities`.
   2. **Limits.** The output allowance must not exceed the registry
      `max_output_tokens`. The estimated input tokens plus the allowance must fit
      `context_window_tokens`.
   3. **Adapter.** The provider adapter's `validate` must accept the request
      unchanged. Example: Anthropic API version `2026-10-21` rejects `temperature`.
   4. **Routable.** `routable = false` in the registry removes the model.
   5. **Price.** A price must be effective at the decision time. A missing price
      excludes the model; it is never treated as zero.
   6. **Availability.** The provider's circuit breaker must currently admit
      attempts. This check reads breaker state without changing it.
   7. **Evidence** (`quality_constrained` only); see below.
4. Eligible candidates are sorted by maximum estimated cost, cheapest first. Ties
   keep configuration order. The first four form the attempt chain.
5. The chain goes to the existing executor (see *Retries and fallback*).

Rejection reasons are recorded per candidate: `missing_capability`,
`output_limit`, `context_window`, `adapter_rejected`, `not_routable`,
`missing_price`, `circuit_open`, `no_evidence`, `evidence_output_limit`,
`invalid_evidence_time`,
`stale_evidence`, `insufficient_samples`, `below_quality`, `no_latency_evidence`,
and `latency_above_limit`.

If no candidate can run the request (failing steps 3.1 to 3.3), the client gets
the primary adapter's own validation error when it rejected the request,
otherwise 400 `unsupported_capability` / `unsupported_by_route`. If every capable
candidate has an open circuit, the response is 502 `provider_circuit_open`.
Otherwise it is 502 `no_eligible_route`.

### Determinism

`select` in `gateway/routing/router.py` is a pure function. Its inputs are the
alias's policy, the request profile (required capabilities, estimated input
tokens, output allowance), the candidate facts, and the decision time. Identical
inputs give identical output.

The decision time is read once per request. All prices come from that one
instant, and quality evidence comes from one snapshot. The completion log
records `routing_policy_version` (a hash of the alias's routing table, the
registry entries, prices, and output limits of its targets) and
`routing_evidence_version` (a hash of the evaluation run IDs in the snapshot).
Together with the request's estimated input tokens and the breaker states, these
identify every input to a decision.

Circuit-breaker state is per process. Two gateway instances can therefore
choose differently while one of them has an open breaker. That is the only
input that differs between instances.

## Capability registry

`[[routing.models]]` entries describe what a model supports through this gateway:

```toml
[[routing.models]]
provider = "anthropic"
model = "your-anthropic-model-id"
context_window_tokens = 200000
max_output_tokens = 8192
capabilities = ["streaming", "stream_usage", "stop"]
routable = true
```

The gateway cannot discover these values, so take them from the vendor's
documentation for the exact model ID. Startup fails in these cases:

- an entry lists a capability its provider adapter does not implement
  (`temperature` or `top_p` on Anthropic `2026-10-21`);
- a routed target has no entry;
- a routed target has no price;
- the alias's default output allowance exceeds a target's `max_output_tokens`.

Both limits are required. A model whose limits are unknown cannot be checked, so
it cannot be routed to. Tools, structured output, images, and other modalities do
not exist as capabilities because the gateway does not implement them.

## Cost prediction

Each candidate's estimate comes from the Phase 3 pricing engine
(`Price.charge_micro_usd`), in integer micro-USD with one rounding:

| Figure | Meaning |
| --- | --- |
| Maximum (`routing_maximum_micro_usd`) | Estimated input tokens × input rate + output allowance × output rate. This is the amount the budget reservation for that attempt holds at the same pricing version. Ranking uses it. |
| Expected (`routing_expected_micro_usd`) | Same input, with output equal to the mean output tokens measured in the candidate's evaluation run, capped at the allowance. Absent without evidence. Logged only, never used for ranking. |
| Actual | The ledger's `settled` charge from provider-reported usage. |
| Unresolved | The ledger's `unresolved` charge, held at the reservation when usage is unknown. |

Ranking uses the maximum for three reasons. Output length cannot be predicted per
request. The maximum is computed the same way for every candidate. And it equals
the budget hold, so the selected candidate is also the one most likely to fit the
remaining budget. Join the completion log's `cost_request_id` with
`cost_attempts.request_id` to compare an estimate with the recorded cost.

Input tokens come from the gateway's provider-neutral estimator (UTF-8 bytes / 3
plus overheads). Providers tokenize differently. The same estimate is applied to
every candidate so the comparison is consistent, and settlement uses each
provider's reported counts. Only input and output token dimensions are priced.
Usage that reports other dimensions (cached input, for example) settles as
unresolved, as in Phase 3.

The decision prices candidates at the gateway's clock. The reservation selects
the price again at the database clock. If a new price takes effect between the
two, the reservation uses the newer price. The ledger stores the version actually
reserved, and the log stores the version used for the estimate. Earlier ledger
rows are never re-priced.

## Quality evidence

Evidence is the latest evaluation run recorded in PostgreSQL for the candidate's
provider and model on the alias's `dataset` and `dataset_version`. Only runs that
were not simulated and were scored by the current evaluator version count. See
[evaluation.md](evaluation.md) for how runs are produced.

For a run to qualify it must:

| Rule | Configuration |
| --- | --- |
| Have been run with `max_tokens` no larger than this request's output allowance; output truncation beyond what was measured is not assumed harmless | request and alias |
| Have completed no longer ago than the limit, and not in the future | `max_evidence_age_days` |
| Contain at least this many cases | `min_samples` |
| Have a 95% Wilson lower bound of the pass rate at least this value | `min_success_rate` |
| Optionally, have a p95 latency at or below the limit, from at least `min_samples` successful calls | `max_p95_latency_ms` |

The rules use only the latest run, so a regression in a newer run takes effect.
Provider errors in a run count as failed cases. The Wilson lower bound accounts
for sample size: 44/44 gives about 0.92 and 10/10 about 0.72.

Latency comes from the evaluation run: wall-clock time of each successful call
from the machine that ran it. It measures the provider under those conditions, not
under the gateway's live traffic, and a p95 limit is not an SLA. Live attempt
latency is exported in `gateway_provider_attempt_duration_seconds` but not used
for routing, because it varies between instances and with load.

The gateway loads a snapshot of evidence at startup. Startup fails if the
evaluation tables are missing. A background task reloads the snapshot every
`evidence_refresh_seconds` (default 60). If a reload fails, the previous snapshot
stays in use and `gateway_routing_evidence_refreshes_total{outcome="failed"}`
increases. Evidence age is computed from each run's completion time, so evidence
from a snapshot that cannot be refreshed still expires. No request reads
evaluation tables.

## Budgets, retries, and fallback

Routing only chooses and orders candidates. The executor still applies the request
deadline, circuit breakers, bounded retries, fallback, streaming rules,
cancellation, and settlement unchanged:

- Redis admission happens once per request, after routing, with the same token
  reservation as before.
- Each attempt in the chain obtains its own PostgreSQL reservation at its own
  model's price immediately before dispatch, and is settled separately. A
  fallback is not charged at the first candidate's price, and a logical request
  is not double-charged: a certain pre-execution failure releases its reservation.
- A budget rejection (429) ends the request. The first candidate has the
  smallest reservation of all eligible candidates, so no other eligible
  candidate would fit either.
- A failure where the provider certainly did not run the request (connection
  refused, explicit 429, an open circuit at dispatch) moves to the next
  candidate. A failure that may have run (timeout after sending, 5xx, broken
  stream) ends the request and keeps the uncertain charge. That is the Phase 2
  rule.
- A stream is never moved to another model after its first event.

**A candidate becomes unavailable between selection and dispatch.** The breaker
check during routing is advisory. At dispatch the executor acquires a breaker
permit. If the breaker opened in between, the attempt is refused without a
reservation or provider call, and the next candidate in the chain is tried within
the original deadline. If it was the last candidate, the client gets 502
`provider_circuit_open`.

For `quality_constrained`, every fallback in the chain has also qualified. When
the policy falls back to the primary for lack of evidence, the chain is the
primary alone. Routed aliases cannot set `fallbacks`; their fallbacks are their
other eligible candidates.

## Observability

The `completion finished` log event gains these fields for every alias:
`routing_policy`, `cost_request_id`, and `upstream_model` (the model of the last
attempt). Routed requests also get `routing_reason`, `routing_policy_version`,
`routing_evidence_version`, `routing_candidates`, `routing_eligible`,
`routing_exclusions` (`provider/model:reason`, at most eight),
`routing_chain`, `routing_pricing_version`, `routing_estimated_input_tokens`,
`routing_maximum_micro_usd`, `routing_expected_micro_usd`, and
`routing_evidence_run_id`. A refused routing decision logs `routing rejected` with
the alias, policy, and error code. No prompt or output text is logged.

Prometheus metrics, labelled only by configured aliases and providers and by
fixed enums:

- `gateway_routing_decisions_total{alias, policy, reason, provider}`; `reason` is
  `lowest_cost`, `qualified_lowest_cost`, `no_qualified_candidate`, or
  `rejected` (with `provider="none"`).
- `gateway_routing_exclusions_total{alias, reason}`.
- `gateway_routing_decision_duration_seconds{policy}`: time to plan a request,
  including adapter validation and routing.
- `gateway_routing_evidence_refreshes_total{outcome}`.

## Security

- Routing inputs are configuration, the gateway's own state, and the request
  fields the schema already accepts. The task is fixed by the alias, and the
  strict schema rejects unknown fields such as `provider` or `metadata`. Headers
  play no part. A client therefore cannot pick a provider, task, or price.
- Every upstream URL and credential comes from operator configuration. A provider
  whose key variable is unset fails startup, so no route can lead to a provider
  without credentials.
- Decisions use no per-tenant state. Rollout assignment depends only on the
  caller's own principal ID. Circuit-breaker state is shared by all callers, as in
  Phase 2. A tenant whose traffic opens a breaker therefore affects routing for
  others.
- Responses do not reveal the selected provider or model. Callers can see the
  provider and model of their own attempts through `/v1/usage`, as in Phase 3.
- Per-request routing work is bounded: at most eight candidates, at most four
  attempts per request, and no I/O. The decision measured 6–27 µs
  ([benchmarks.md](benchmarks.md)).
- Evaluation tables are append-only (a trigger rejects updates and deletes), and
  runs dated in the future are refused when recorded and ignored when routing.
  Anyone who can insert rows can still add evidence. Give the gateway's database
  role `SELECT` on `evaluation_runs`, and limit `INSERT` to the role that records
  evaluations.

## Configuration example

```toml
[models.classify]
provider = "anthropic"
model = "large-model-id"
max_output_tokens = 16

[models.classify.routing]
policy = "quality_constrained"
candidates = [
    { provider = "openai", model = "small-model-id" },
    { provider = "anthropic", model = "medium-model-id" },
]
dataset = "support-ticket-category"
dataset_version = "1"
min_success_rate = 0.9
min_samples = 40
max_evidence_age_days = 30
on_insufficient_evidence = "primary"

[routing]
enabled = true
# One [[routing.models]] entry per target, and a [[cost.pricing]] entry for each.
```

[configuration.md](configuration.md#routing) lists every field.
