"""Deterministic local PostgreSQL cost-admission benchmark."""

import argparse
import asyncio
import json
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import text

from gateway.cost.config import CostConfig, SpendingLimits
from gateway.cost.pricing import Price
from gateway.cost.store import CostStore
from gateway.providers.base import Usage


def percentiles(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    return {
        name: round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * quantile))], 3)
        for name, quantile in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99))
    }


async def run(url: str, requests: int, concurrency: int) -> dict[str, object]:
    config = CostConfig(
        enabled=True,
        pricing=[
            Price(
                provider="bench",
                model="mock",
                version="v1",
                effective_at=datetime(2020, 1, 1, tzinfo=UTC),
                input_per_million_usd=Decimal("1"),
                output_per_million_usd=Decimal("1"),
            )
        ],
        default_key_limits=SpendingLimits(daily_micro_usd=1_000_000_000),
    )
    store = CostStore.from_url(url, config)
    principal = f"bench-{uuid.uuid4()}"
    semaphore = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    transaction_latencies: list[float] = []

    async def one() -> None:
        async with semaphore:
            attempt = uuid.uuid4()
            started = time.perf_counter()
            await store.reserve(
                attempt_id=attempt,
                request_id=uuid.uuid4(),
                principal_id=principal,
                provider="bench",
                model="mock",
                estimated_input_tokens=50,
                maximum_output_tokens=50,
            )
            latencies.append((time.perf_counter() - started) * 1000)
            await store.dispatch(attempt)
            await store.settle(
                attempt, Usage(50, 10, 60), may_have_processed=True, classification="success"
            )

    try:
        for _ in range(min(20, requests)):
            started = time.perf_counter()
            async with store.engine.begin() as connection:
                await connection.execute(text("SELECT 1"))
            transaction_latencies.append((time.perf_counter() - started) * 1000)
        wall = time.perf_counter()
        await asyncio.gather(*(one() for _ in range(requests)))
        elapsed = time.perf_counter() - wall
        async with store.engine.connect() as connection:
            plan = (
                await connection.execute(
                    text("""EXPLAIN (ANALYZE, FORMAT JSON)
                SELECT provider, model, SUM(charge_micro_usd) FROM cost_attempts
                WHERE principal_id=:principal AND created_at >= clock_timestamp() - interval '1 day'
                GROUP BY provider, model"""),
                    {"principal": principal},
                )
            ).scalar_one()
        execution_ms = plan[0]["Execution Time"]
        return {
            "requests": requests,
            "concurrency": concurrency,
            "elapsed_seconds": round(elapsed, 3),
            "attempts_per_second": round(requests / elapsed, 2),
            "reservation_latency_ms": percentiles(latencies),
            "postgres_transaction_latency_ms": percentiles(transaction_latencies),
            "summary_query_execution_ms": round(execution_ms, 3),
            "database": "local PostgreSQL",
            "price": "illustrative mock price",
        }
    finally:
        await store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=20)
    args = parser.parse_args()
    if args.requests < 1 or args.concurrency < 1:
        parser.error("requests and concurrency must be positive")
    print(
        json.dumps(
            asyncio.run(run(args.database_url, args.requests, args.concurrency)),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
