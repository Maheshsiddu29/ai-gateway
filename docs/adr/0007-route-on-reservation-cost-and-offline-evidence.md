# ADR 0007: Route on reservation cost and offline evaluation evidence

Status: accepted for Phase 4.

## Context

An alias should be able to use a cheaper model when that model is good enough for
the alias's task. Both "cheaper" and "good enough" need definitions that the
gateway can compute on every request, without I/O, the same way on every
instance. Each definition also has to be explainable afterwards.

## Decision

**Cost.** Candidates are ranked by the maximum estimated cost of the request:
estimated input tokens and the full output allowance, priced with the Phase 3
pricing engine. Ties keep configuration order. This is the amount each attempt's
budget reservation holds.

An expected cost from measured output length is computed when evidence exists,
but it is only logged.

**Quality.** "Good enough" means a recorded offline evaluation on a dataset that
the operator assigned to the alias. The run must be:

- non-simulated;
- scored by the current evaluator version;
- fresh;
- at least `min_samples` cases;
- above `min_success_rate` at the lower bound of the 95% Wilson interval.

Evidence lives in append-only PostgreSQL tables. The request path reads it from an
immutable in-memory snapshot that is replaced periodically.

When no candidate qualifies, the alias's primary model is used, or the request is
rejected, as configured.

## Alternatives considered

- **Rank by expected cost.** Per-request output length is unknown. A mean from an
  evaluation dataset can differ from live traffic in either direction. Ranking on
  it would also break the property that the chosen candidate has the smallest
  budget reservation.
- **Score requests with a classifier or another model.** This adds latency, cost,
  a new failure mode, and a decision that cannot be reproduced from configuration.
- **Use live success or latency from production traffic.** Live outcomes have no
  ground truth for quality. Live latency differs between instances and with load,
  which would break determinism. Pooled metrics would also let one tenant's
  traffic shape another tenant's routing.
- **Compare thresholds with the point estimate.** A perfect score on ten cases
  would qualify a model. The Wilson lower bound requires more evidence from
  smaller samples and is cheap to compute.

## Consequences

- A verbose cheap model can be ranked below a terse expensive one even if its
  actual cost would be lower. Ranking accepts that in exchange for a bound that
  holds for every request.
- Quality guarantees apply to the evaluation dataset. They are only as
  representative as the dataset and only as current as the latest run.
- Selection depends on circuit-breaker state, which is per process. Instances can
  differ while a breaker is open; every other input is shared configuration or
  shared database state.
- Without recorded evidence, a `quality_constrained` alias behaves like `fixed` by
  default, so enabling it is safe before evaluations exist.
