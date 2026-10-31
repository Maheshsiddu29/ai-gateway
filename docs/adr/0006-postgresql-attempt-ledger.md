# ADR 0006: PostgreSQL per-attempt cost ledger

Status: accepted for Phase 3.

Each upstream attempt gets a UUID and a committed PostgreSQL reservation before dispatch. Budget counters and immutable ledger events are written in the same transaction. The attempt row tracks the current state, while append-only events preserve the financial transition history. PostgreSQL row locks serialize key/team daily/monthly admission across processes. Prices are selected by exact provider/model and effective time, then snapshotted on the attempt.

Redis admission precedes PostgreSQL reservation. Cross-store atomicity is unavailable; Redis leases expire and PostgreSQL recovery distinguishes attempts without a dispatch mark from possible upstream execution. The latter retain conservative liability. This design favors bounded overstatement over silent undercounting after a crash.

The preflight reservation uses estimated input and maximum output. Provider usage can exceed that estimate, creating debt. Unknown usage remains a liability until an observed report is supplied. The ledger models input and output token charges only, so it cannot assert vendor invoice accuracy.
