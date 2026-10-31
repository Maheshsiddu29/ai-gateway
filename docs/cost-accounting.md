# Cost accounting

## Units and pricing

The authoritative monetary store is PostgreSQL. Amounts in the database and API are integer micro-USD (one millionth of one USD). Prices in TOML are decimal strings in USD per million input or output tokens. An attempt's charge is `round_half_up(input_tokens × input_rate + output_tokens × output_rate)` micro-USD, rounded once per attempt. Binary floating point is never used in this calculation. The gateway supports these two token dimensions only. Provider discounts, cached-token rates, image/audio units, taxes, and vendor invoice rounding are outside the model; reported gateway cost is not an invoice.

Each configured price has a provider, upstream model, version, and UTC effective timestamp. At reservation, the gateway chooses the latest effective entry for that exact pair. It stores the version and both rates on the attempt row. Later catalog changes do not recalculate historical charges. A missing applicable price refuses the request before provider execution. Every configured primary and fallback pair must have an effective price at startup. Maximum output tokens are required so the gateway can reserve a bounded exposure.

## Records

Migration `0001_cost_ledger` creates:

- `cost_attempts`: one UUID per upstream attempt, a logical request UUID, principal and optional team, provider/model, pricing snapshot, estimated input and maximum output tokens, reservation, charge, usage, state, classification, and timestamps.
- `budget_periods`: current reserved and spent micro-USD for a key or team daily/monthly period. Its composite key and row locks serialize distributed admission.
- `attempt_budget_periods`: links an attempt to the exact period rows it charged. Reconciliation uses these links even after a UTC boundary or configuration change.
- `cost_events`: append-only reservation, dispatch, settlement, release, expiration, and late-reconciliation events. A database trigger rejects updates and deletes. `attempt_id` plus `event_type` is unique.

Migration `0002_budget_link_integrity` adds a foreign key from each attempt-period link to its budget counter and a trigger that prevents changes to the attempt's pricing and identity snapshot.

The ledger stores no prompts, outputs, upstream credentials, or gateway keys. The principal ID is a truncated SHA-256 key fingerprint from existing authentication. Request IDs in the ledger are generated UUIDs, independent of caller-supplied HTTP correlation IDs.

## Lifecycle

1. Redis admission, when enabled, reserves request/token/concurrency capacity.
2. Before **each** provider attempt, PostgreSQL reserves estimated input plus the configured maximum output at that attempt's price. All applicable key and team daily/monthly rows are checked and incremented in one short transaction.
3. A separate transaction marks the attempt dispatched immediately before provider I/O. No database transaction remains open during an upstream call.
4. Reported usage settles with the observed charge. Unused capacity is released, or an overage becomes spent debt. A certain pre-execution rejection releases the entire reservation.
5. A timeout, interrupted stream, 5xx, cancellation, successful response without usage, or response with unpriced usage dimensions is marked unresolved and conservatively charged at the reservation amount. Known token counts are retained even when their charge remains unknown. A later trusted usage report can reconcile this state once; the event stores the adjustment.

A retry or fallback is a new attempt with its own UUID, price, reservation, and settlement. A 5xx does not prove the provider did no work. Duplicate settlement callbacks see an already final state and do not change counters or create another event. PostgreSQL constraints enforce UUID and event uniqueness; updates to budget counters and ledger rows share one transaction.

The reservation expiry is the configured request deadline plus 60 seconds. A recovery loop scans expired records every 30 seconds. An expired `reserved` attempt is released because dispatch was never recorded. An expired `dispatched` attempt becomes unresolved and its full reservation becomes spent liability. If a dispatch commit response is lost, this policy may retain an exposure for an attempt that never reached the provider. It errs toward accounting for possible execution. A later observed usage report can adjust an unresolved attempt.

Database admission uses a 10-connection pool with up to five overflow connections, a 2-second pool wait, 3-second statement/command timeout, and 2-second lock timeout. Serialization failures and deadlocks during reservation or settlement are retried at most twice with short waits. Other database failures fail closed. A lost commit response is not blindly retried as a new attempt; the stable UUID and constraints prevent duplicate financial entries.

## Limitations

Input tokens are estimated from the existing request estimator for reservation. A provider can report more than estimated; the full observed charge is recorded and the resulting spent total may exceed the configured limit. The limit prevents concurrent over-commitment against configured reservations, not arbitrary vendor overages. Missing usage and reported cache or other unpriced usage dimensions remain conservative liabilities and can overstate or understate final vendor cost until reconciled. No automatic vendor usage retrieval or invoice reconciliation exists.
