# 0004: Atomic admission in one Redis script, failing closed

## Status

Accepted

## Context

Limits must hold across any number of gateway instances: requests per minute,
tokens per minute, and concurrent requests per gateway key. A request must not
consume one limit when another rejects it, and two instances admitting at the same
moment must not both take the last unit of capacity. Token usage is only known
after a response, while admission happens before it.

## Decision

- All of a key's admission state lives in Redis under one Cluster hash tag, and one
  Lua script checks every limit and then applies every change, or applies none.
  The script reads the Redis server clock.
- Request and token limits are token buckets with configurable rate and burst.
  Tokens are reserved at admission (estimated input plus output allowance) and a
  second script settles the reservation with the usage actually charged, carrying
  any excess as bounded debt.
- Concurrency is a set of leases whose lifetime is the request deadline plus a
  grace period, so no renewal is needed and a crashed instance's leases expire.
- Settlement is idempotent per reservation ID and leaves a tombstone when it finds
  no reservation, so a lost admission reply can be resolved by releasing the
  reservation regardless of command ordering.
- When Redis cannot give a decision, requests are refused (503). There is no
  in-memory fallback.

## Alternatives considered

- **Separate Redis commands or a MULTI transaction per limit.** Simpler individual
  steps, but checking and updating several keys without a script either races or
  needs WATCH retries, and a failed later check leaves earlier limits consumed.
- **Sliding-window counters.** Exact per-window counts, but more memory per key and
  no natural way to express token reservations and refunds.
- **Per-process limits.** No external dependency, but limits multiply with the
  number of instances.
- **Failing open on Redis errors.** Higher availability, but an outage would
  silently remove every limit.
- **Renewable short leases.** Faster recovery after a crash, but a periodic task per
  request and a correctness dependency on renewal timing.

## Consequences

- Redis is on the request path: each admitted request costs two script calls, and
  a Redis outage refuses traffic while admission control is enabled.
- Limits are only as durable as Redis; data loss resets buckets and forgets leases.
- Token limits are enforced on estimates and reported usage, so they are
  approximate with respect to provider token counts.
- A crashed instance's concurrency slots stay held until its leases expire.
