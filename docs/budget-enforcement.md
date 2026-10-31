# Spending budgets

## Configuration and identity

When `[cost] enabled = true`, PostgreSQL and an applicable price for every route are required. A default key limit can be overridden by a `key_limits` entry indexed by the existing `key-<12 hex>` principal ID. `principal_teams` maps those IDs to configured teams; each team can have daily and monthly limits. No organization management API exists. A key's own spending is visible to that key. Team spending is visible only to principals named in `team_readers` for that team. A caller cannot obtain another principal's spending by supplying an ID.

A missing limit for a period means that period is uncapped. A zero limit denies positive reservations. A configured limit applies to reserved plus spent capacity. The analytics API reports `limit_micro_usd`, `reserved_micro_usd`, `spent_micro_usd`, available capacity, and debt. Budget reads show the current UTC day and month. Usage reads are bounded to 31 days, 100 rows per page, and 10,000 offset.

## Concurrency and periods

A reservation creates missing period rows and locks each applicable row in a stable order with `SELECT ... FOR UPDATE`. It compares `reserved + spent + new` to the configured limit, updates counters, inserts the attempt and its period links, and commits atomically. If any limit rejects, the whole transaction rolls back. Independent FastAPI processes share these PostgreSQL locks. The database's unique attempt and event constraints are additional idempotency guards.

Daily periods begin at 00:00 UTC; monthly periods begin on the first day at 00:00 UTC. An attempt belongs to the periods in force when reserved. A request spanning midnight remains attributed to its original periods. Late settlement adjusts those same period rows, rather than the day on which usage arrived. An actual charge above the reservation becomes debt in its original periods. New requests in a period with debt are denied until a later period or a corrected charge restores capacity.

## Redis coordination and failure behavior

The request first obtains Redis admission if enabled. PostgreSQL then reserves each possible provider attempt before dispatch. A PostgreSQL denial or failure prevents dispatch and the request's Redis token/concurrency reservation is settled through the existing cleanup path. A process crash between Redis and PostgreSQL leaves only a bounded Redis lease; a crash after PostgreSQL reservation is handled by the recovery loop. There is no cross-store transaction.

A provider attempt is never sent before its PostgreSQL reservation and dispatch mark commit. Database unavailability fails closed. Readiness checks the accounting table whenever cost accounting is enabled. A financial settlement error is surfaced, logged without SQL parameters, and left for conservative expiry recovery; it is not treated as a zero charge. Redis outages remain governed by the existing admission policy. Running without Redis admission is allowed only when that separate control is intentionally disabled; PostgreSQL budgets still apply when cost accounting is enabled.

The database user needs read/write privileges on accounting tables; migrations require DDL privileges and run before gateway startup. Use distinct operational roles in production. The application does not create tables automatically. Review retention and backup policies for the ledger and configure PostgreSQL durability appropriate to financial records. This implementation does not create invoices or collect payment.
