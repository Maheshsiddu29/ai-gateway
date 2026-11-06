# 0009: Model calls are journaled separately from workflow checkpoints

## Status

Accepted

## Context

Phase 6B makes investigations durable. LangGraph's PostgreSQL checkpointer saves
the graph's state after each node, which is enough to resume the pure stages. It
is not enough for the one step with an external, billable effect: a model call
through the gateway.

A checkpoint is written *after* a node returns. If a worker dies during the
`plan` or `diagnose` node, the checkpoint says only that the node did not finish.
Re-running it would send the request again, and the gateway deduplicates nothing:
`X-Request-ID` correlates requests, it does not make them idempotent. Two
executions mean two generations and two charges.

## Decision

- Each gateway call is a row in an `operations` table, written in its own
  transaction before and after the HTTP request: `intent`, then `dispatched`
  before the request starts, then `succeeded` or `failed`.
- On resume the journal, not the checkpoint, decides: an `intent` may be sent,
  because dispatch is committed first and so the request provably never left; a
  recorded outcome is replayed without a call; a `dispatched` row without an
  outcome becomes `uncertain` and the investigation ends as
  `reconciliation_required`. A dispatched call is never sent again.
- Both writes are fenced on the worker's lease, so a worker that lost ownership
  cannot record an outcome a newer owner would contradict.
- Graph state holds only JSON and is re-validated when read, so a checkpoint is
  a resumption hint, never authority.

## Alternatives considered

- **Checkpoints alone.** Simpler, and sufficient for the pure stages, but a
  crash inside a model-calling node is indistinguishable from one before it, so
  recovery would have to choose between repeating paid calls and abandoning every
  interrupted investigation.
- **Replaying from a request hash.** Treat an identical request as already done
  and reuse a cached reply. That only helps when the reply was stored, which is
  exactly what a crash during the call prevents.
- **Provider-side idempotency keys.** Would give real at-most-once execution, but
  the gateway's API does not accept them and the providers behind it do not agree
  on semantics. Adding them means changing the gateway's contract and every
  adapter, which Phase 6B deliberately does not do.
- **Automatic retry when usage is unknown.** Rejected: the runtime cannot tell a
  lost reply from a lost request, so this would knowingly risk double charges.

## Consequences

- Exactly-once execution is not claimed. The guarantee is at-most-once dispatch
  per journal entry, plus an explicit terminal state when the outcome is unknown.
- `reconciliation_required` investigations need a human, who compares the
  journal's `gateway_request_id` with the gateway's ledger. The runtime holds
  their reservation as an unresolved liability rather than guessing.
- A crash in the window between committing `dispatched` and sending the request
  is treated as uncertain even though nothing was sent. The alternative, writing
  the dispatch marker after the request, would risk a resend instead.
- Every model call costs two extra transactions.
