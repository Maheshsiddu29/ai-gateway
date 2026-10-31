"""PostgreSQL-backed financial invariants; set GATEWAY_TEST_DATABASE_URL."""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from multiprocessing import get_context

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from gateway.cost.config import CostConfig, SpendingLimits
from gateway.cost.pricing import Price, PriceCatalog
from gateway.cost.store import CostStore
from gateway.errors import AdmissionUnavailableError, RateLimitedError
from gateway.providers.base import Usage


def price(version: str = "v1", rate: str = "1") -> Price:
    return Price(
        provider="mock",
        model="model",
        version=version,
        effective_at=datetime(2020, 1, 1, tzinfo=UTC),
        input_per_million_usd=Decimal(rate),
        output_per_million_usd=Decimal("0"),
    )


@pytest.fixture
async def store() -> AsyncIterator[CostStore]:
    url = os.environ.get("GATEWAY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set GATEWAY_TEST_DATABASE_URL for PostgreSQL tests")
    config = CostConfig(
        enabled=True,
        pricing=[price()],
        default_key_limits=SpendingLimits(daily_micro_usd=10, monthly_micro_usd=10),
    )
    result = CostStore.from_url(url, config)
    try:
        assert await result.ping(), "run Alembic migrations before PostgreSQL tests"
        yield result
    finally:
        await result.close()


async def reserve(store: CostStore, principal: str, tokens: int = 1) -> uuid.UUID:
    attempt_id = uuid.uuid4()
    await store.reserve(
        attempt_id=attempt_id,
        request_id=uuid.uuid4(),
        principal_id=principal,
        provider="mock",
        model="model",
        estimated_input_tokens=tokens,
        maximum_output_tokens=0,
    )
    return attempt_id


def _reserve_in_process(url: str, principal: str) -> int:
    async def run() -> int:
        config = CostConfig(
            enabled=True,
            pricing=[price()],
            default_key_limits=SpendingLimits(daily_micro_usd=10, monthly_micro_usd=10),
        )
        independent = CostStore.from_url(url, config)
        try:
            results = await asyncio.gather(
                *(reserve(independent, principal) for _ in range(10)),
                return_exceptions=True,
            )
            unexpected = [
                value for value in results if not isinstance(value, (uuid.UUID, RateLimitedError))
            ]
            if unexpected:
                raise RuntimeError(f"unexpected admission result: {type(unexpected[0]).__name__}")
            return sum(isinstance(value, uuid.UUID) for value in results)
        finally:
            await independent.close()

    return asyncio.run(run())


async def test_concurrent_budget_admission_and_duplicate_settlement(store: CostStore) -> None:
    principal = f"test-{uuid.uuid4()}"
    results = await asyncio.gather(
        *(reserve(store, principal) for _ in range(100)), return_exceptions=True
    )
    accepted = [item for item in results if isinstance(item, uuid.UUID)]
    rejected = [item for item in results if isinstance(item, RateLimitedError)]
    assert len(accepted) == 10
    assert len(rejected) == 90
    await asyncio.gather(*(store.dispatch(attempt) for attempt in accepted))
    await asyncio.gather(
        *(
            store.settle(attempt, Usage(1, 0, 1), may_have_processed=True, classification="success")
            for attempt in accepted
        )
    )
    await store.settle(
        accepted[0], Usage(1, 0, 1), may_have_processed=True, classification="success"
    )
    async with store.engine.connect() as connection:
        totals = (
            await connection.execute(
                text("""SELECT SUM(reserved_micro_usd),
            SUM(spent_micro_usd) FROM budget_periods WHERE subject_id=:principal"""),
                {"principal": principal},
            )
        ).one()
        assert totals == (0, 20)
        charges = (
            await connection.execute(
                text("""SELECT COUNT(*) FROM cost_events
            WHERE attempt_id=CAST(:id AS uuid) AND event_type='settled'"""),
                {"id": str(accepted[0])},
            )
        ).scalar_one()
        assert charges == 1


async def test_multiple_processes_share_one_budget(store: CostStore) -> None:
    url = os.environ["GATEWAY_TEST_DATABASE_URL"]
    principal = f"test-{uuid.uuid4()}"
    loop = asyncio.get_running_loop()
    with ProcessPoolExecutor(max_workers=4, mp_context=get_context("spawn")) as pool:
        counts = await asyncio.gather(
            *(loop.run_in_executor(pool, _reserve_in_process, url, principal) for _ in range(4))
        )
    assert sum(counts) == 10


async def test_duplicate_reservation_rolls_back_budget_updates(store: CostStore) -> None:
    principal = f"test-{uuid.uuid4()}"
    attempt = await reserve(store, principal, 3)
    with pytest.raises(AdmissionUnavailableError):
        await store.reserve(
            attempt_id=attempt,
            request_id=uuid.uuid4(),
            principal_id=principal,
            provider="mock",
            model="model",
            estimated_input_tokens=3,
            maximum_output_tokens=0,
        )
    async with store.engine.connect() as connection:
        value = (
            await connection.execute(
                text("""SELECT reserved_micro_usd
            FROM budget_periods WHERE subject_id=:principal AND period_kind='daily'"""),
                {"principal": principal},
            )
        ).scalar_one()
    assert value == 3


async def test_reconnects_after_postgres_terminates_a_pooled_connection(
    store: CostStore,
) -> None:
    async with store.engine.connect() as other:
        async with store.engine.connect() as doomed:
            pid = (await doomed.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        await other.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        assert await store.ping()
    await reserve(store, f"test-{uuid.uuid4()}")


async def test_unknown_usage_and_late_reconciliation(store: CostStore) -> None:
    principal = f"test-{uuid.uuid4()}"
    attempt = await reserve(store, principal, 3)
    await store.dispatch(attempt)
    await store.settle(attempt, None, may_have_processed=True, classification="timeout")
    async with store.engine.connect() as connection:
        state = (
            await connection.execute(
                text("""SELECT state, charge_micro_usd
            FROM cost_attempts WHERE attempt_id=CAST(:id AS uuid)"""),
                {"id": str(attempt)},
            )
        ).one()
        assert state == ("unresolved", 3)
    await store.settle(
        attempt, Usage(2, 0, 2), may_have_processed=True, classification="reconciled"
    )
    async with store.engine.connect() as connection:
        total = (
            await connection.execute(
                text("""SELECT spent_micro_usd FROM budget_periods
            WHERE subject_id=:principal AND period_kind='daily'"""),
                {"principal": principal},
            )
        ).scalar_one()
        assert total == 2


async def test_unpriced_usage_dimension_remains_unresolved(store: CostStore) -> None:
    attempt = await reserve(store, f"test-{uuid.uuid4()}", 7)
    await store.dispatch(attempt)
    await store.settle(
        attempt,
        Usage(2, 1, 3, billing_dimensions_supported=False),
        may_have_processed=True,
        classification="success",
    )
    async with store.engine.connect() as connection:
        row = (
            await connection.execute(
                text("""SELECT state, charge_micro_usd,
            input_tokens, output_tokens FROM cost_attempts
            WHERE attempt_id=CAST(:id AS uuid)"""),
                {"id": str(attempt)},
            )
        ).one()
    assert row == ("unresolved", 7, 2, 1)


async def test_pricing_change_preserves_old_snapshot_and_overage_becomes_debt(
    store: CostStore,
) -> None:
    principal = f"test-{uuid.uuid4()}"
    first = await reserve(store, principal, 1)
    store.catalog = PriceCatalog(
        [
            price(),
            Price(
                provider="mock",
                model="model",
                version="v2",
                effective_at=datetime(2021, 1, 1, tzinfo=UTC),
                input_per_million_usd=Decimal("2"),
                output_per_million_usd=Decimal("0"),
            ),
        ]
    )
    second = await reserve(store, principal, 1)
    await store.dispatch(first)
    await store.dispatch(second)
    await store.settle(first, Usage(1, 0, 1), may_have_processed=True, classification="success")
    await store.settle(second, Usage(10, 0, 10), may_have_processed=True, classification="success")
    async with store.engine.connect() as connection:
        rows = (
            await connection.execute(
                text("""SELECT pricing_version, charge_micro_usd
            FROM cost_attempts WHERE attempt_id IN
            (CAST(:first AS uuid), CAST(:second AS uuid))
            ORDER BY pricing_version"""),
                {
                    "first": str(first),
                    "second": str(second),
                },
            )
        ).all()
    assert rows == [("v1", 1), ("v2", 20)]
    with pytest.raises(RateLimitedError):
        await reserve(store, principal, 1)


async def test_pre_dispatch_release_and_expiry(store: CostStore) -> None:
    principal = f"test-{uuid.uuid4()}"
    attempt = await reserve(store, principal, 5)
    await store.settle(attempt, None, may_have_processed=False, classification="not_dispatched")
    another = await reserve(store, principal, 10)
    async with store.engine.begin() as connection:
        await connection.execute(
            text("""UPDATE cost_attempts SET expires_at=clock_timestamp()-interval '1 second'
            WHERE attempt_id=CAST(:id AS uuid)"""),
            {"id": str(another)},
        )
    await store.recover_expired()
    async with store.engine.connect() as connection:
        state = (
            await connection.execute(
                text("""SELECT state FROM cost_attempts
            WHERE attempt_id=CAST(:id AS uuid)"""),
                {"id": str(another)},
            )
        ).scalar_one()
        assert state == "released"


async def test_expired_dispatched_attempt_retains_liability(store: CostStore) -> None:
    principal = f"test-{uuid.uuid4()}"
    attempt = await reserve(store, principal, 7)
    await store.dispatch(attempt)
    async with store.engine.begin() as connection:
        await connection.execute(
            text("""UPDATE cost_attempts
            SET expires_at=clock_timestamp()-interval '1 second'
            WHERE attempt_id=CAST(:id AS uuid)"""),
            {"id": str(attempt)},
        )
    await store.recover_expired()
    async with store.engine.connect() as connection:
        state = (
            await connection.execute(
                text("""SELECT state, charge_micro_usd
            FROM cost_attempts WHERE attempt_id=CAST(:id AS uuid)"""),
                {"id": str(attempt)},
            )
        ).one()
    assert state == ("unresolved", 7)


async def test_financial_events_cannot_be_changed(store: CostStore) -> None:
    attempt = await reserve(store, f"test-{uuid.uuid4()}")
    with pytest.raises(DBAPIError):
        async with store.engine.begin() as connection:
            await connection.execute(
                text("""UPDATE cost_events SET amount_micro_usd=0
                WHERE attempt_id=CAST(:id AS uuid)"""),
                {"id": str(attempt)},
            )


async def test_pricing_snapshot_and_budget_links_are_database_protected(
    store: CostStore,
) -> None:
    principal = f"test-{uuid.uuid4()}"
    attempt = await reserve(store, principal)
    with pytest.raises(DBAPIError):
        async with store.engine.begin() as connection:
            await connection.execute(
                text("""UPDATE cost_attempts SET input_rate=999
                WHERE attempt_id=CAST(:id AS uuid)"""),
                {"id": str(attempt)},
            )
    with pytest.raises(DBAPIError):
        async with store.engine.begin() as connection:
            await connection.execute(
                text("""DELETE FROM budget_periods
                WHERE subject_id=:principal"""),
                {"principal": principal},
            )
