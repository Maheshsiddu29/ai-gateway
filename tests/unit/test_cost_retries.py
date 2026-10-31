"""Bounded retries for PostgreSQL serialization and deadlock errors."""

import uuid

import pytest
from sqlalchemy.exc import DBAPIError

from gateway.cost.config import CostConfig
from gateway.cost.store import CostStore
from gateway.errors import AdmissionUnavailableError


@pytest.mark.parametrize("sqlstate", ["40P01", "40001"])
async def test_budget_reservation_retries_transaction_abort(
    monkeypatch: pytest.MonkeyPatch,
    sqlstate: str,
) -> None:
    class AbortedError(Exception):
        pass

    AbortedError.sqlstate = sqlstate  # type: ignore[attr-defined]
    store = CostStore.from_url("postgresql+asyncpg://localhost/test", CostConfig())
    calls = 0

    async def reserve_once(**_: object) -> tuple[int, list[tuple[str, str, float]]]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise DBAPIError(None, None, AbortedError("retry"))
        return 1, []

    monkeypatch.setattr(store, "_reserve", reserve_once)
    try:
        result = await store.reserve(
            attempt_id=uuid.uuid4(),
            request_id=uuid.uuid4(),
            principal_id="key-test",
            provider="mock",
            model="model",
            estimated_input_tokens=1,
            maximum_output_tokens=0,
        )
        assert result == 1
        assert calls == 2
    finally:
        await store.close()


async def test_settlement_retries_deadlock_once(monkeypatch: pytest.MonkeyPatch) -> None:
    class DeadlockError(Exception):
        sqlstate = "40P01"

    store = CostStore.from_url("postgresql+asyncpg://localhost/test", CostConfig())
    calls = 0

    async def settle_once(*_: object, **__: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise AdmissionUnavailableError("temporary") from DBAPIError(
                None, None, DeadlockError("retry")
            )

    monkeypatch.setattr(store, "_settle_once", settle_once)
    try:
        await store.settle(uuid.uuid4(), None, may_have_processed=True, classification="timeout")
        assert calls == 2
    finally:
        await store.close()
