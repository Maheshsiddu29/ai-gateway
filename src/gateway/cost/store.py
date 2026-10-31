"""PostgreSQL transactions for per-attempt budget reservation and settlement."""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from gateway.core.metrics import Metrics
from gateway.cost.config import CostConfig
from gateway.cost.pricing import Price, PriceCatalog
from gateway.errors import AdmissionUnavailableError, RateLimitedError
from gateway.providers.base import Usage

logger = logging.getLogger(__name__)


def _retryable(exc: BaseException) -> bool:
    if not isinstance(exc, DBAPIError):
        return False
    return getattr(exc.orig, "sqlstate", None) in {"40001", "40P01"}


def period_start(at: datetime, kind: str) -> datetime:
    utc = at.astimezone(UTC)
    if kind == "daily":
        return utc.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "monthly":
        return utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    raise ValueError(f"unknown period: {kind}")


class CostStore:
    def __init__(
        self,
        engine: AsyncEngine,
        config: CostConfig,
        *,
        reservation_ttl: int = 600,
        metrics: Metrics | None = None,
    ) -> None:
        self.engine = engine
        self.config = config
        self.catalog = PriceCatalog(config.pricing)
        self.reservation_ttl = reservation_ttl
        self.metrics = metrics

    @classmethod
    def from_url(
        cls,
        url: str,
        config: CostConfig,
        *,
        reservation_ttl: int = 3700,
        metrics: Metrics | None = None,
    ) -> "CostStore":
        engine = create_async_engine(
            url,
            pool_size=10,
            max_overflow=5,
            pool_timeout=2,
            pool_pre_ping=True,
            connect_args={
                "command_timeout": 3,
                "server_settings": {
                    "statement_timeout": "3000",
                    "lock_timeout": "2000",
                    "idle_in_transaction_session_timeout": "5000",
                },
            },
        )
        return cls(engine, config, reservation_ttl=reservation_ttl, metrics=metrics)

    async def close(self) -> None:
        await self.engine.dispose()

    async def ping(self) -> bool:
        try:
            async with self.engine.connect() as connection:
                await connection.execute(text("SELECT 1 FROM cost_attempts LIMIT 0"))
            return True
        except (SQLAlchemyError, OSError):
            return False

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncConnection]:
        async with self.engine.begin() as connection:
            yield connection

    async def reserve(
        self,
        *,
        attempt_id: uuid.UUID,
        request_id: uuid.UUID,
        principal_id: str,
        provider: str,
        model: str,
        estimated_input_tokens: int,
        maximum_output_tokens: int,
    ) -> int:
        if estimated_input_tokens < 0 or maximum_output_tokens < 0:
            raise ValueError("reservation token counts cannot be negative")
        started = time.perf_counter()
        try:
            for retry in range(3):
                try:
                    amount, utilization = await self._reserve(
                        attempt_id=attempt_id,
                        request_id=request_id,
                        principal_id=principal_id,
                        provider=provider,
                        model=model,
                        estimated_input_tokens=estimated_input_tokens,
                        maximum_output_tokens=maximum_output_tokens,
                    )
                    break
                except DBAPIError as exc:
                    if not _retryable(exc) or retry == 2:
                        raise
                    await asyncio.sleep(0.01 * (retry + 1))
            if self.metrics is not None:
                self.metrics.cost_admissions.labels("accepted").inc()
                self.metrics.cost_reserved_micro_usd.inc(amount)
                for scope, kind, ratio in utilization:
                    self.metrics.cost_budget_utilization.labels(scope, kind).observe(ratio)
            return amount
        except RateLimitedError:
            if self.metrics is not None:
                self.metrics.cost_admissions.labels("rejected").inc()
            raise
        except (SQLAlchemyError, OSError) as exc:
            if self.metrics is not None:
                self.metrics.cost_admissions.labels("unavailable").inc()
                self.metrics.cost_transaction_errors.labels("reserve").inc()
            logger.error("cost reservation transaction failed: %s", type(exc).__name__)
            raise AdmissionUnavailableError("Spending admission is unavailable.") from exc
        finally:
            if self.metrics is not None:
                self.metrics.cost_admission_duration.observe(time.perf_counter() - started)

    async def _reserve(
        self,
        *,
        attempt_id: uuid.UUID,
        request_id: uuid.UUID,
        principal_id: str,
        provider: str,
        model: str,
        estimated_input_tokens: int,
        maximum_output_tokens: int,
    ) -> tuple[int, list[tuple[str, str, float]]]:
        async with self._transaction() as connection:
            at = (await connection.execute(text("SELECT clock_timestamp()"))).scalar_one()
            price = self.catalog.select(provider, model, at)
            amount = price.charge_micro_usd(estimated_input_tokens, maximum_output_tokens)
            team_id = self.config.principal_teams.get(principal_id)
            periods: list[tuple[str, str, str, datetime, int]] = []
            for scope, subject_id, limits in self.config.limits_for(principal_id):
                for kind, limit in (
                    ("daily", limits.daily_micro_usd),
                    ("monthly", limits.monthly_micro_usd),
                ):
                    if limit is not None:
                        periods.append((scope, subject_id, kind, period_start(at, kind), limit))
            periods.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
            utilization: list[tuple[str, str, float]] = []
            for scope, subject_id, kind, start, limit in periods:
                params = {"scope": scope, "subject_id": subject_id, "kind": kind, "start": start}
                await connection.execute(
                    text("""INSERT INTO budget_periods
                    (scope, subject_id, period_kind, period_start)
                    VALUES (:scope, :subject_id, :kind, :start) ON CONFLICT DO NOTHING"""),
                    params,
                )
                balance = (
                    await connection.execute(
                        text("""SELECT reserved_micro_usd, spent_micro_usd FROM budget_periods
                    WHERE scope=:scope AND subject_id=:subject_id AND period_kind=:kind
                    AND period_start=:start FOR UPDATE"""),
                        params,
                    )
                ).one()
                if balance.reserved_micro_usd + balance.spent_micro_usd + amount > limit:
                    raise RateLimitedError(
                        "Spending budget exceeded.", code=f"{scope}_{kind}_budget_exceeded"
                    )
                ratio = (
                    float(
                        Decimal(balance.reserved_micro_usd + balance.spent_micro_usd + amount)
                        / Decimal(limit)
                    )
                    if limit
                    else 0.0
                )
                utilization.append((scope, kind, ratio))
                await connection.execute(
                    text("""UPDATE budget_periods SET reserved_micro_usd=reserved_micro_usd+:amount
                    WHERE scope=:scope AND subject_id=:subject_id AND period_kind=:kind
                    AND period_start=:start"""),
                    {**params, "amount": amount},
                )
            await connection.execute(
                text("""INSERT INTO cost_attempts (
                attempt_id, request_id, principal_id, team_id, provider, model, pricing_version,
                input_rate, output_rate, estimated_input_tokens, maximum_output_tokens,
                reserved_micro_usd, state, created_at, expires_at)
                VALUES (CAST(:attempt_id AS uuid), CAST(:request_id AS uuid), :principal_id,
                :team_id, :provider, :model, :version, :input_rate, :output_rate,
                :estimated_input, :maximum_output, :amount, 'reserved', :at, :expires)"""),
                {
                    "attempt_id": str(attempt_id),
                    "request_id": str(request_id),
                    "principal_id": principal_id,
                    "team_id": team_id,
                    "provider": provider,
                    "model": model,
                    "version": price.version,
                    "input_rate": price.input_per_million_usd,
                    "output_rate": price.output_per_million_usd,
                    "estimated_input": estimated_input_tokens,
                    "maximum_output": maximum_output_tokens,
                    "amount": amount,
                    "at": at,
                    "expires": at + timedelta(seconds=self.reservation_ttl),
                },
            )
            for scope, subject_id, kind, start, _ in periods:
                await connection.execute(
                    text("""INSERT INTO attempt_budget_periods
                    (attempt_id, scope, subject_id, period_kind, period_start)
                    VALUES (CAST(:attempt_id AS uuid), :scope, :subject_id, :kind, :start)"""),
                    {
                        "attempt_id": str(attempt_id),
                        "scope": scope,
                        "subject_id": subject_id,
                        "kind": kind,
                        "start": start,
                    },
                )
            await self._event(connection, attempt_id, "reserved", amount, at)
            return amount, utilization

    async def dispatch(self, attempt_id: uuid.UUID) -> None:
        try:
            async with self._transaction() as connection:
                result = await connection.execute(
                    text("""UPDATE cost_attempts
                    SET state='dispatched', dispatched_at=clock_timestamp()
                    WHERE attempt_id=CAST(:id AS uuid) AND state='reserved'"""),
                    {"id": str(attempt_id)},
                )
                if result.rowcount != 1:
                    raise AdmissionUnavailableError("Spending reservation is no longer active.")
                await self._event(connection, attempt_id, "dispatched", 0)
        except (SQLAlchemyError, OSError) as exc:
            if self.metrics is not None:
                self.metrics.cost_transaction_errors.labels("dispatch").inc()
            logger.error("cost dispatch transaction failed: %s", type(exc).__name__)
            raise AdmissionUnavailableError("Spending admission is unavailable.") from exc

    async def settle(
        self,
        attempt_id: uuid.UUID,
        usage: Usage | None,
        *,
        may_have_processed: bool,
        classification: str,
    ) -> None:
        for retry in range(3):
            try:
                await self._settle_once(
                    attempt_id,
                    usage,
                    may_have_processed=may_have_processed,
                    classification=classification,
                )
                return
            except AdmissionUnavailableError as exc:
                if exc.__cause__ is None or not _retryable(exc.__cause__) or retry == 2:
                    raise
                await asyncio.sleep(0.01 * (retry + 1))

    async def _settle_once(
        self,
        attempt_id: uuid.UUID,
        usage: Usage | None,
        *,
        may_have_processed: bool,
        classification: str,
    ) -> None:
        settled_metric: tuple[str, int] | None = None
        try:
            async with self._transaction() as connection:
                row = (
                    (
                        await connection.execute(
                            text("""SELECT * FROM cost_attempts
                    WHERE attempt_id=CAST(:id AS uuid) FOR UPDATE"""),
                            {"id": str(attempt_id)},
                        )
                    )
                    .mappings()
                    .one()
                )
                state = row["state"]
                if state in ("settled", "released"):
                    return
                if state == "unresolved" and usage is None:
                    return
                reserve = int(row["reserved_micro_usd"])
                if usage is not None and usage.billing_dimensions_supported:
                    price = Price(
                        provider=row["provider"],
                        model=row["model"],
                        version=row["pricing_version"],
                        effective_at=row["created_at"],
                        input_per_million_usd=Decimal(row["input_rate"]),
                        output_per_million_usd=Decimal(row["output_rate"]),
                    )
                    charge = price.charge_micro_usd(usage.prompt_tokens, usage.completion_tokens)
                    new_state = "settled"
                elif may_have_processed:
                    charge = reserve
                    new_state = "unresolved"
                else:
                    charge = 0
                    new_state = "released"
                prior_spent = reserve if state == "unresolved" else 0
                prior_reserved = 0 if state == "unresolved" else reserve
                links = (
                    await connection.execute(
                        text("""SELECT scope, subject_id, period_kind,
                    period_start FROM attempt_budget_periods WHERE attempt_id=CAST(:id AS uuid)
                    ORDER BY scope, subject_id, period_kind"""),
                        {"id": str(attempt_id)},
                    )
                ).mappings()
                for link in links:
                    await connection.execute(
                        text("""UPDATE budget_periods SET
                        reserved_micro_usd=reserved_micro_usd-:reserved,
                        spent_micro_usd=spent_micro_usd+:delta
                        WHERE scope=:scope AND subject_id=:subject_id AND period_kind=:kind
                        AND period_start=:start"""),
                        {
                            "reserved": prior_reserved,
                            "delta": charge - prior_spent,
                            "scope": link["scope"],
                            "subject_id": link["subject_id"],
                            "kind": link["period_kind"],
                            "start": link["period_start"],
                        },
                    )
                await connection.execute(
                    text("""UPDATE cost_attempts SET state=:state,
                    charge_micro_usd=:charge, input_tokens=:input_tokens,
                    output_tokens=:output_tokens, classification=:classification,
                    settled_at=clock_timestamp() WHERE attempt_id=CAST(:id AS uuid)"""),
                    {
                        "state": new_state,
                        "charge": charge,
                        "input_tokens": usage.prompt_tokens if usage else None,
                        "output_tokens": usage.completion_tokens if usage else None,
                        "classification": classification,
                        "id": str(attempt_id),
                    },
                )
                event_type = "reconciled" if state == "unresolved" else new_state
                await self._event(connection, attempt_id, event_type, charge - prior_spent)
                settled_metric = (new_state, charge)
        except (SQLAlchemyError, OSError) as exc:
            if self.metrics is not None:
                self.metrics.cost_transaction_errors.labels("settle").inc()
                self.metrics.cost_reconciliation_errors.inc()
            logger.error("cost settlement transaction failed: %s", type(exc).__name__)
            raise AdmissionUnavailableError("Cost settlement is unavailable.") from exc
        if self.metrics is not None and settled_metric is not None:
            state, charge = settled_metric
            if state == "settled":
                self.metrics.cost_observed_micro_usd.inc(charge)
            elif state == "unresolved":
                self.metrics.cost_unknown_usage.inc()

    async def recover_expired(self, *, batch_size: int = 50) -> int:
        async with self._transaction() as connection:
            rows = (
                await connection.execute(
                    text("""SELECT attempt_id, state FROM cost_attempts
                WHERE state IN ('reserved', 'dispatched') AND expires_at < clock_timestamp()
                ORDER BY expires_at FOR UPDATE SKIP LOCKED LIMIT :limit"""),
                    {"limit": batch_size},
                )
            ).all()
            for attempt_id, state in rows:
                await self._settle_expired(connection, attempt_id, state)
        if self.metrics is not None:
            for _, state in rows:
                self.metrics.cost_expired.labels(state).inc()
        return len(rows)

    async def _settle_expired(
        self, connection: AsyncConnection, attempt_id: uuid.UUID, state: str
    ) -> None:
        amount = (
            await connection.execute(
                text("""SELECT reserved_micro_usd FROM cost_attempts
            WHERE attempt_id=CAST(:id AS uuid)"""),
                {"id": str(attempt_id)},
            )
        ).scalar_one()
        links = (
            await connection.execute(
                text("""SELECT scope, subject_id, period_kind,
            period_start FROM attempt_budget_periods WHERE attempt_id=CAST(:id AS uuid)
            ORDER BY scope, subject_id, period_kind"""),
                {"id": str(attempt_id)},
            )
        ).mappings()
        charge = amount if state == "dispatched" else 0
        for link in links:
            await connection.execute(
                text("""UPDATE budget_periods SET
                reserved_micro_usd=reserved_micro_usd-:amount,
                spent_micro_usd=spent_micro_usd+:charge
                WHERE scope=:scope AND subject_id=:subject_id AND period_kind=:kind
                AND period_start=:start"""),
                {
                    "amount": amount,
                    "charge": charge,
                    "scope": link["scope"],
                    "subject_id": link["subject_id"],
                    "kind": link["period_kind"],
                    "start": link["period_start"],
                },
            )
        new_state = "unresolved" if state == "dispatched" else "released"
        await connection.execute(
            text("""UPDATE cost_attempts SET state=:state,
            charge_micro_usd=:charge, classification='expired', settled_at=clock_timestamp()
            WHERE attempt_id=CAST(:id AS uuid)"""),
            {
                "state": new_state,
                "charge": charge,
                "id": str(attempt_id),
            },
        )
        await self._event(connection, attempt_id, "expired", charge)

    async def _event(
        self,
        connection: AsyncConnection,
        attempt_id: uuid.UUID,
        event_type: str,
        amount: int,
        at: datetime | None = None,
    ) -> None:
        await connection.execute(
            text("""INSERT INTO cost_events
            (attempt_id, event_type, amount_micro_usd, created_at)
            VALUES (CAST(:id AS uuid), :kind, :amount, :at)"""),
            {
                "id": str(attempt_id),
                "kind": event_type,
                "amount": amount,
                "at": at or datetime.now(UTC),
            },
        )
