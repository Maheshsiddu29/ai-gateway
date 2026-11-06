"""Per-investigation spending admission and settlement against PostgreSQL."""

import asyncio

import pytest
from psycopg import errors, sql

from agent_runtime.budget import ModelPrice, cost_micro_usd, input_token_bound
from agent_runtime.investigation.graph import OperationType
from agent_runtime.investigation.prompts import planning_messages
from agent_runtime.persistence import migrations
from agent_runtime.persistence.database import Connection, Pool
from agent_runtime.persistence.store import (
    AdmissionRefusedError,
    InvestigationStore,
    Lease,
    ObservedUsage,
    Operation,
    OperationConflictError,
)
from tests.db import PRICE, insert_accepted, insert_running, investigation
from tests.fakes import example_result
from tests.integration.test_worker import ScriptedGateway, worker

BOUND, MAX_TOKENS = 1_000, 512
# 1,000 input tokens at $1/M and 512 output tokens at $2/M.
RESERVATION = cost_micro_usd(ModelPrice.model_validate(PRICE), BOUND, MAX_TOKENS)
ALIASES = {OperationType.PLAN: "planner", OperationType.DIAGNOSE: "diagnosis"}


async def spending(conn: Connection, investigation_id: str) -> tuple[int, int, int]:
    row = await investigation(conn, investigation_id)
    return row["budget_micro_usd"], row["reserved_micro_usd"], row["settled_micro_usd"]


async def intent(
    store: InvestigationStore, lease: Lease, kind: OperationType = OperationType.PLAN
) -> Operation:
    return await store.create_intent(lease, kind, ALIASES[kind], MAX_TOKENS, BOUND)


async def dispatched(
    store: InvestigationStore, lease: Lease, kind: OperationType = OperationType.PLAN
) -> Operation:
    operation = await intent(store, lease, kind)
    await store.mark_dispatched(lease, operation)
    return operation


async def operation_settled(conn: Connection, operation: Operation) -> int | None:
    cursor = await conn.execute(
        "SELECT settled_micro_usd FROM operations WHERE operation_id = %s",
        (operation.operation_id,),
    )
    row = await cursor.fetchone()
    assert row is not None
    settled: int | None = row["settled_micro_usd"]
    return settled


async def test_admission_reserves_the_worst_case_estimate(owner: Connection, pool: Pool) -> None:
    lease = await insert_running(owner)
    operation = await intent(InvestigationStore(pool), lease)

    assert RESERVATION == 2_024
    assert operation.reserved_micro_usd == RESERVATION
    assert await spending(owner, lease.investigation_id) == (1_000_000, RESERVATION, 0)


async def test_known_usage_settles_at_its_cost_and_releases_the_rest(
    owner: Connection, pool: Pool
) -> None:
    lease = await insert_running(owner)
    store = InvestigationStore(pool)
    operation = await dispatched(store, lease)

    await store.record_success(lease, operation, "{}", ObservedUsage(300, 40))

    assert await spending(owner, lease.investigation_id) == (1_000_000, 0, 380)
    assert await operation_settled(owner, operation) == 380


@pytest.mark.parametrize(
    ("may_have_executed", "usage", "settled"),
    [
        (False, None, 0),  # certainly not run: released
        (True, ObservedUsage(100, 0), 100),
        (True, None, RESERVATION),  # unknown cost: charged in full
    ],
)
async def test_failures_settle_by_whether_the_call_may_have_run(
    owner: Connection,
    pool: Pool,
    may_have_executed: bool,
    usage: ObservedUsage | None,
    settled: int,
) -> None:
    lease = await insert_running(owner)
    store = InvestigationStore(pool)
    operation = await dispatched(store, lease)

    await store.record_failure(
        lease,
        operation,
        code="gateway_unavailable",
        may_have_executed=may_have_executed,
        usage=usage,
    )

    assert await spending(owner, lease.investigation_id) == (1_000_000, 0, settled)


async def test_success_without_usage_is_charged_the_full_reservation(
    owner: Connection, pool: Pool
) -> None:
    lease = await insert_running(owner)
    store = InvestigationStore(pool)
    operation = await dispatched(store, lease)

    await store.record_success(lease, operation, "{}", None)

    assert await spending(owner, lease.investigation_id) == (1_000_000, 0, RESERVATION)


async def test_an_operation_is_settled_once(owner: Connection, pool: Pool) -> None:
    lease = await insert_running(owner)
    store = InvestigationStore(pool)
    operation = await dispatched(store, lease)
    await store.record_success(lease, operation, "{}", ObservedUsage(300, 40))

    with pytest.raises(OperationConflictError):
        await store.record_success(lease, operation, "{}", ObservedUsage(1, 1))
    with pytest.raises(OperationConflictError):
        await store.record_failure(lease, operation, code="x", may_have_executed=False)
    assert await spending(owner, lease.investigation_id) == (1_000_000, 0, 380)


async def test_an_unknown_outcome_stays_reserved_as_a_liability(
    owner: Connection, pool: Pool
) -> None:
    lease = await insert_running(owner)
    store = InvestigationStore(pool)
    operation = await dispatched(store, lease)

    await store.mark_uncertain(lease, operation)

    assert await spending(owner, lease.investigation_id) == (1_000_000, RESERVATION, 0)
    assert await operation_settled(owner, operation) is None


async def test_the_budget_refuses_calls_that_might_not_fit(owner: Connection, pool: Pool) -> None:
    # After 100 is settled, a second reservation needs 100 + RESERVATION.
    lease = await insert_running(owner, budget_micro_usd=RESERVATION + 99)
    store = InvestigationStore(pool)
    operation = await dispatched(store, lease)
    await store.record_success(lease, operation, "{}", ObservedUsage(80, 10))  # settles 100

    with pytest.raises(AdmissionRefusedError) as caught:
        await intent(store, lease, OperationType.DIAGNOSE)

    assert caught.value.code == "budget_exhausted"
    assert await spending(owner, lease.investigation_id) == (RESERVATION + 99, 0, 100)
    row = await investigation(owner, lease.investigation_id)
    assert row["llm_calls_admitted"] == 1


async def test_a_call_without_a_recorded_price_is_refused(owner: Connection, pool: Pool) -> None:
    lease = await insert_running(owner, pricing={"diagnosis": PRICE})

    with pytest.raises(AdmissionRefusedError) as caught:
        await intent(InvestigationStore(pool), lease)

    assert caught.value.code == "price_unknown"
    assert await spending(owner, lease.investigation_id) == (1_000_000, 0, 0)


async def test_concurrent_admissions_cannot_exceed_the_budget(
    owner: Connection, pool: Pool
) -> None:
    leases = [await insert_running(owner, budget_micro_usd=RESERVATION) for _ in range(5)]
    store = InvestigationStore(pool)

    async def admit_twice(lease: Lease) -> tuple[Operation | BaseException, ...]:
        return await asyncio.gather(
            intent(store, lease, OperationType.PLAN),
            intent(store, lease, OperationType.DIAGNOSE),
            return_exceptions=True,
        )

    results = await asyncio.gather(*(admit_twice(lease) for lease in leases))

    for lease, outcomes in zip(leases, results, strict=True):
        assert sum(isinstance(o, Operation) for o in outcomes) == 1
        # The loser is refused by the budget or by the one-open-operation index.
        assert all(
            isinstance(o, Operation | AdmissionRefusedError | errors.UniqueViolation)
            for o in outcomes
        )
        assert await spending(owner, lease.investigation_id) == (RESERVATION, RESERVATION, 0)


async def test_finishing_releases_undispatched_intents(owner: Connection, pool: Pool) -> None:
    lease = await insert_running(owner)
    store = InvestigationStore(pool)
    await intent(store, lease)
    await owner.execute(
        "UPDATE investigations SET current_stage = 'finalize' WHERE investigation_id = %s",
        (lease.investigation_id,),
    )
    await store.finish(lease, example_result(lease.investigation_id))

    assert await spending(owner, lease.investigation_id) == (1_000_000, 0, 0)


async def test_only_reconciliation_may_finish_with_a_liability(
    owner: Connection, pool: Pool
) -> None:
    lease = await insert_running(owner)
    store = InvestigationStore(pool)
    await store.mark_uncertain(lease, await dispatched(store, lease))

    terminal = (
        "UPDATE investigations SET status = %s, failure_code = %s, result = '{}',"
        " incident = NULL, completed_at = now(), lease_owner = NULL, lease_expires_at = NULL"
        " WHERE investigation_id = %s"
    )
    with pytest.raises(errors.CheckViolation, match="settled_when_finished"):
        await owner.execute(terminal, ("failed", "x", lease.investigation_id))
    with pytest.raises(errors.CheckViolation, match="settled_when_finished"):
        await owner.execute(terminal, ("completed", None, lease.investigation_id))
    await owner.execute(
        "UPDATE investigations SET status = 'reconciliation_required', failure_code = 'x',"
        " result = '{}', incident = NULL, completed_at = now(), lease_owner = NULL,"
        " lease_expires_at = NULL WHERE investigation_id = %s",
        (lease.investigation_id,),
    )


@pytest.mark.parametrize(
    "assignment",
    ["budget_micro_usd = 5", "pricing = '{}'", "settled_micro_usd = 0"],
)
async def test_budget_records_cannot_be_rewritten(
    owner: Connection, pool: Pool, assignment: str
) -> None:
    lease = await insert_running(owner)
    store = InvestigationStore(pool)
    await store.record_success(lease, await dispatched(store, lease), "{}", ObservedUsage(5, 5))

    with pytest.raises(errors.CheckViolation):
        await owner.execute(
            sql.SQL("UPDATE investigations SET {} WHERE investigation_id = %s").format(
                sql.SQL(assignment)
            ),
            (lease.investigation_id,),
        )


async def test_investigations_accepted_before_budgets_are_not_admitted(
    owner: Connection, pool: Pool, schema: str
) -> None:
    await migrations.downgrade(owner, schema, target=1, allow_data_loss=False)
    old = f"inv-{'1' * 32}"
    await owner.execute(
        "INSERT INTO investigations (investigation_id, principal_id, request_hash, incident,"
        " workflow_version, prompt_version, max_llm_calls, max_tool_calls, max_output_tokens,"
        " deadline_at, status, fencing_token, attempts, lease_owner, lease_expires_at)"
        " VALUES (%s, 'agent-key-0123456789ab', decode(repeat('ab', 32), 'hex'), 'x', 'v', 'p',"
        " 2, 6, 512, now() + interval '1 hour', 'running', 1, 1, 'w',"
        " now() + interval '1 minute')",
        (old,),
    )
    assert await migrations.upgrade(owner, schema) == [2]

    with pytest.raises(AdmissionRefusedError) as caught:
        await intent(InvestigationStore(pool), Lease(old, "w", 1))
    assert caught.value.code == "price_unknown"


async def test_a_worker_stops_when_the_budget_is_spent(owner: Connection, pool: Pool) -> None:
    # Room for the planner's reservation, which is charged in full because the
    # gateway reports no usage, and not for any diagnosis call after it.
    incident = "The database host is out of memory."
    bound = input_token_bound(planning_messages(incident, frozenset({"/"}), 6))
    planner = cost_micro_usd(ModelPrice.model_validate(PRICE), bound, 512)
    investigation_id = await insert_accepted(
        owner, incident=incident, budget_micro_usd=planner + 10
    )
    gateway = ScriptedGateway()
    gateway.usage = None

    async with worker(pool, gateway) as w:
        await w.run_until_idle()

    row = await investigation(owner, investigation_id)
    assert (row["status"], row["failure_code"]) == ("failed", "budget_exhausted")
    assert row["result"]["failure"]["stage"] == "diagnosis"
    assert gateway.requests == {"planner": 1}
    assert row["reserved_micro_usd"] == 0
    assert row["settled_micro_usd"] == planner


async def test_unknown_usage_is_charged_in_full_by_the_worker(
    owner: Connection, pool: Pool
) -> None:
    investigation_id = await insert_accepted(owner)
    gateway = ScriptedGateway()
    gateway.usage = None
    async with worker(pool, gateway) as w:
        await w.run_until_idle()

    row = await investigation(owner, investigation_id)
    cursor = await owner.execute(
        "SELECT sum(reserved_micro_usd) AS reserved FROM operations WHERE investigation_id = %s",
        (investigation_id,),
    )
    reserved = (await cursor.fetchone() or {})["reserved"]
    assert row["status"] == "completed"
    assert row["result"]["usage"]["prompt_tokens"] == 0
    assert row["settled_micro_usd"] == reserved > 0
    assert row["reserved_micro_usd"] == 0
