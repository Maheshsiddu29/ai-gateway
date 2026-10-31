"""Opt-in PostgreSQL process restart test using a caller-owned disposable cluster."""

import asyncio
import os
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from gateway.cost.config import CostConfig, SpendingLimits
from gateway.cost.pricing import Price
from gateway.cost.store import CostStore
from gateway.errors import AdmissionUnavailableError


async def test_budget_admission_fails_closed_and_recovers_after_database_restart() -> None:
    directory = os.environ.get("GATEWAY_MANAGED_POSTGRES_DIR")
    url = os.environ.get("GATEWAY_TEST_DATABASE_URL")
    binary = shutil.which("pg_ctl")
    if not directory or not url or not binary:
        pytest.skip("requires a caller-owned disposable PostgreSQL cluster and pg_ctl")
    port = int(os.environ.get("GATEWAY_MANAGED_POSTGRES_PORT", "55432"))
    config = CostConfig(
        enabled=True,
        pricing=[
            Price(
                provider="mock",
                model="model",
                version="v1",
                effective_at=datetime(2020, 1, 1, tzinfo=UTC),
                input_per_million_usd=Decimal("1"),
                output_per_million_usd=Decimal("0"),
            )
        ],
        default_key_limits=SpendingLimits(daily_micro_usd=100),
    )
    store = CostStore.from_url(url, config)
    stopped = False
    try:
        assert await store.ping()
        await asyncio.to_thread(
            subprocess.run,
            [binary, "-D", directory, "-m", "fast", "stop"],
            check=True,
            capture_output=True,
            text=True,
        )
        stopped = True
        assert not await store.ping()
        with pytest.raises(AdmissionUnavailableError):
            await store.reserve(
                attempt_id=uuid.uuid4(),
                request_id=uuid.uuid4(),
                principal_id=f"test-{uuid.uuid4()}",
                provider="mock",
                model="model",
                estimated_input_tokens=1,
                maximum_output_tokens=0,
            )
    finally:
        if stopped:
            await asyncio.to_thread(
                subprocess.run,
                [
                    binary,
                    "-D",
                    directory,
                    "-o",
                    f"-h 127.0.0.1 -p {port} -k /private/tmp",
                    "-l",
                    "/private/tmp/ai-gateway-pg-phase3.log",
                    "start",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        assert await store.ping()
        await store.close()
