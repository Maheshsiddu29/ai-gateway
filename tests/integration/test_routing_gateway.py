"""Routed requests through the gateway with real PostgreSQL and Redis.

Set GATEWAY_TEST_DATABASE_URL; redis-server must be installed. Provider names are
randomized per test so that evidence and ledger rows from other tests in the
shared database never influence a decision.
"""

import asyncio
import logging
import os
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy import text

from gateway.admission.redis_store import KEY_PREFIX
from gateway.admission.tokens import estimate_input_tokens
from gateway.core.config import ProviderConfig
from gateway.core.security import StaticKeyAuthenticator
from gateway.errors import ProviderUnavailableError
from gateway.evaluation.dataset import load_dataset
from gateway.evaluation.runner import run_evaluation
from gateway.evaluation.simulated import SimulatedProvider
from gateway.evaluation.store import record_run
from gateway.main import build_app
from gateway.providers.base import Completion, Provider, Usage
from gateway.providers.factory import build_provider
from gateway.resilience.breaker import CircuitBreaker, Permit
from gateway.schemas.completions import ChatMessage
from tests.fakes import (
    SSE_HEADERS,
    ControlledStream,
    FakeProvider,
    UpstreamStub,
    make_config,
    make_settings,
    openai_chunk,
    serve,
    sse_body,
)
from tests.polling import eventually
from tests.redis_server import RedisServer
from tests.servers import run_server
from tests.tcp_proxy import TcpProxy

TICKETS = (
    Path(__file__).parents[2] / "evaluations" / "datasets" / "support-ticket-category-v1.jsonl"
)
ALLOWANCE = 10
CHEAP_RATE = 1
PREMIUM_RATE = 5
MESSAGES = [{"role": "user", "content": "Classify: my card was charged twice."}]


def database_url() -> str:
    url = os.environ.get("GATEWAY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set GATEWAY_TEST_DATABASE_URL for PostgreSQL tests")
    return url


def principal_id(key: str) -> str:
    principal = StaticKeyAuthenticator([SecretStr(key)]).authenticate(key)
    assert principal is not None
    return principal.id


def reservation(rate: int) -> int:
    """Input is priced at zero in these tests, so a reservation is allowance x rate."""
    return ALLOWANCE * rate


@dataclass
class Names:
    cheap: str
    premium: str

    @classmethod
    def random(cls) -> "Names":
        suffix = uuid.uuid4().hex[:8]
        return cls(cheap=f"cheap{suffix}", premium=f"premium{suffix}")


def price(provider: str, model: str, rate: int | str, version: str = "v1") -> dict[str, str]:
    return {
        "provider": provider,
        "model": model,
        "version": version,
        "effective_at": "2020-01-01T00:00:00Z",
        "input_per_million_usd": "0",
        "output_per_million_usd": str(rate),
    }


def raw_config(
    names: Names,
    *,
    cheap_capabilities: list[str] | None = None,
    daily_budget: int = 1_000_000,
    on_insufficient_evidence: str = "primary",
    rollout_percent: int = 100,
    pricing: list[dict[str, str]] | None = None,
    routing_enabled: bool = True,
) -> dict[str, Any]:
    candidates = [{"provider": names.cheap, "model": "small"}]
    return {
        "providers": {
            names.cheap: {"type": "openai_compatible", "base_url": "https://cheap.test/v1"},
            names.premium: {"type": "openai_compatible", "base_url": "https://premium.test/v1"},
        },
        "models": {
            "classify": {
                "provider": names.premium,
                "model": "large",
                "max_output_tokens": ALLOWANCE,
                "routing": {
                    "policy": "lowest_cost",
                    "candidates": candidates,
                    "rollout_percent": rollout_percent,
                },
            },
            "quality": {
                "provider": names.premium,
                "model": "large",
                "max_output_tokens": ALLOWANCE,
                "routing": {
                    "policy": "quality_constrained",
                    "candidates": candidates,
                    "dataset": "support-ticket-category",
                    "dataset_version": "1",
                    "min_success_rate": 0.85,
                    "min_samples": 40,
                    "max_evidence_age_days": 30,
                    "on_insufficient_evidence": on_insufficient_evidence,
                },
            },
            "fixed": {
                "provider": names.premium,
                "model": "large",
                "max_output_tokens": ALLOWANCE,
                "fallbacks": [{"provider": names.cheap, "model": "small"}],
            },
        },
        "resilience": {"max_attempts_per_provider": 1},
        "admission": {"enabled": True, "default_limits": {"max_concurrent_requests": 100}},
        "cost": {
            "enabled": True,
            "default_key_limits": {"daily_micro_usd": daily_budget},
            "pricing": pricing
            or [
                price(names.cheap, "small", CHEAP_RATE),
                price(names.premium, "large", PREMIUM_RATE),
            ],
        },
        "routing": {
            "enabled": routing_enabled,
            "models": [
                {
                    "provider": names.cheap,
                    "model": "small",
                    "context_window_tokens": 4096,
                    "max_output_tokens": 100,
                    "capabilities": ["stop"] if cheap_capabilities is None else cheap_capabilities,
                },
                {
                    "provider": names.premium,
                    "model": "large",
                    "context_window_tokens": 4096,
                    "max_output_tokens": 100,
                    "capabilities": ["streaming", "stream_usage", "temperature", "top_p", "stop"],
                },
            ],
        },
    }


@dataclass
class Gateway:
    app: FastAPI
    client: httpx.AsyncClient
    key: str
    names: Names

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"}

    async def post(self, model: str = "classify", **fields: Any) -> httpx.Response:
        return await self.client.post(
            "/v1/chat/completions",
            json={"model": model, "messages": MESSAGES, **fields},
            headers=self.headers,
        )

    async def ledger(self) -> list[Mapping[str, Any]]:
        async with self.app.state.cost_store.engine.connect() as connection:
            result = await connection.execute(
                text("""SELECT request_id, provider, model, pricing_version, state,
                reserved_micro_usd, charge_micro_usd FROM cost_attempts
                WHERE principal_id=:principal ORDER BY created_at, attempt_id"""),
                {"principal": principal_id(self.key)},
            )
            return list(result.mappings())


def ok_provider(name: str, usage: Usage | None = None) -> FakeProvider:
    return FakeProvider(name, completion=Completion("billing", "stop", usage))


@asynccontextmanager
async def routed_gateway(
    redis_url: str,
    raw: dict[str, Any],
    names: Names,
    providers: Mapping[str, Provider],
    *,
    key: str | None = None,
    db_url: str | None = None,
) -> AsyncIterator[Gateway]:
    key = key or f"routing-test-{uuid.uuid4()}"
    app = build_app(
        make_settings(redis_url=redis_url, database_url=db_url or database_url(), api_keys=(key,)),
        make_config(raw, dict.fromkeys(raw["providers"])),
        lambda name, *_: providers[name],
    )
    async with serve(app) as client:
        yield Gateway(app, client, key, names)


async def record_measured_evidence(provider: str, model: str, accuracy: float) -> None:
    """Record a non-simulated run. SimulatedProvider supplies deterministic answers;
    the run is marked as a measurement because routing ignores simulated runs."""
    from sqlalchemy.ext.asyncio import create_async_engine

    dataset = load_dataset(TICKETS)
    run = await run_evaluation(
        SimulatedProvider(provider, dataset, {model: accuracy}),
        model,
        dataset,
        max_output_tokens=8,
        price=None,
        simulated=False,
    )
    engine = create_async_engine(database_url())
    try:
        await record_run(engine, run)
    finally:
        await engine.dispose()


async def test_lowest_cost_routes_to_the_cheapest_model_and_reserves_its_price(
    redis_server: RedisServer,
) -> None:
    names = Names.random()
    providers = {
        names.cheap: ok_provider(names.cheap),
        names.premium: ok_provider(names.premium),
    }
    async with routed_gateway(redis_server.url, raw_config(names), names, providers) as gateway:
        response = await gateway.post()
        rows = await gateway.ledger()

    assert response.status_code == 200
    assert response.json()["model"] == "classify"
    # The response does not reveal which provider or model served it.
    assert names.cheap not in response.text
    assert all(names.cheap not in value for value in response.headers.values())
    [sent] = providers[names.cheap].requests
    assert (sent.model, sent.max_tokens) == ("small", ALLOWANCE)
    assert providers[names.premium].requests == []
    [row] = rows
    assert (row["provider"], row["model"], row["pricing_version"]) == (names.cheap, "small", "v1")
    assert row["reserved_micro_usd"] == reservation(CHEAP_RATE)
    # No usage reported: the selected model's full reservation is retained.
    assert (row["state"], row["charge_micro_usd"]) == ("unresolved", reservation(CHEAP_RATE))


async def test_reported_usage_settles_at_the_selected_models_price(
    redis_server: RedisServer,
) -> None:
    names = Names.random()
    providers = {
        names.cheap: ok_provider(names.cheap, Usage(30, 7, 37)),
        names.premium: ok_provider(names.premium),
    }
    async with routed_gateway(redis_server.url, raw_config(names), names, providers) as gateway:
        assert (await gateway.post()).status_code == 200
        [row] = await gateway.ledger()

    assert (row["state"], row["charge_micro_usd"]) == ("settled", 7 * CHEAP_RATE)


async def test_fixed_aliases_keep_phase_three_behavior(redis_server: RedisServer) -> None:
    names = Names.random()
    providers = {
        names.cheap: ok_provider(names.cheap),
        names.premium: ok_provider(names.premium),
    }
    async with routed_gateway(redis_server.url, raw_config(names), names, providers) as gateway:
        response = await gateway.post("fixed")
        [row] = await gateway.ledger()

    assert response.status_code == 200
    assert row["provider"] == names.premium
    assert providers[names.cheap].requests == []


async def test_disabled_routing_uses_the_primary(redis_server: RedisServer) -> None:
    names = Names.random()
    providers = {
        names.cheap: ok_provider(names.cheap),
        names.premium: ok_provider(names.premium),
    }
    raw = raw_config(names, routing_enabled=False)
    async with routed_gateway(redis_server.url, raw, names, providers) as gateway:
        assert (await gateway.post()).status_code == 200
        assert (await gateway.post("quality")).status_code == 200

    assert len(providers[names.premium].requests) == 2
    assert providers[names.cheap].requests == []


async def test_capability_requirements_filter_candidates(redis_server: RedisServer) -> None:
    names = Names.random()
    providers = {
        names.cheap: ok_provider(names.cheap),
        names.premium: ok_provider(names.premium),
    }
    raw = raw_config(names)
    raw["routing"]["models"][1]["capabilities"] = ["stop", "temperature"]
    async with routed_gateway(redis_server.url, raw, names, providers) as gateway:
        with_temperature = await gateway.post(temperature=0.2)
        unsupported = await gateway.post(top_p=0.5)
        with_stop = await gateway.post(stop=["\n"])

    assert with_temperature.status_code == 200
    [sent] = providers[names.premium].requests
    assert sent.temperature == 0.2
    assert unsupported.status_code == 400
    assert unsupported.json()["error"]["code"] == "unsupported_by_route"
    assert with_stop.status_code == 200
    assert providers[names.cheap].requests[0].stop == ("\n",)


def completion_fields(caplog: pytest.LogCaptureFixture) -> dict[str, Any]:
    [record] = [r for r in caplog.records if r.getMessage() == "completion finished"]
    return vars(record)


async def test_quality_constrained_routing_uses_recorded_evidence(
    redis_server: RedisServer, caplog: pytest.LogCaptureFixture
) -> None:
    names = Names.random()
    await record_measured_evidence(names.cheap, "small", 1.0)
    providers = {
        names.cheap: ok_provider(names.cheap),
        names.premium: ok_provider(names.premium),
    }
    caplog.set_level(logging.INFO, logger="gateway.services.completion")
    async with routed_gateway(redis_server.url, raw_config(names), names, providers) as gateway:
        response = await gateway.post("quality")

    assert response.status_code == 200
    assert len(providers[names.cheap].requests) == 1
    fields = completion_fields(caplog)
    assert fields["routing_reason"] == "qualified_lowest_cost"
    assert fields["routing_quality_verified"] is True
    assert "routing_evidence_run_id" in fields
    assert "routing_primary_exclusion" not in fields


async def test_failing_evidence_keeps_requests_on_the_primary(
    redis_server: RedisServer, caplog: pytest.LogCaptureFixture
) -> None:
    names = Names.random()
    # About 0.7 accuracy is below the 0.85 lower-bound threshold.
    await record_measured_evidence(names.cheap, "small", 0.7)
    await record_measured_evidence(names.premium, "large", 0.7)
    providers = {
        names.cheap: ok_provider(names.cheap),
        names.premium: ok_provider(names.premium),
    }
    caplog.set_level(logging.INFO, logger="gateway.services.completion")
    async with routed_gateway(redis_server.url, raw_config(names), names, providers) as gateway:
        response = await gateway.post("quality")
        [row] = await gateway.ledger()

    assert response.status_code == 200
    assert row["provider"] == names.premium
    assert providers[names.cheap].requests == []
    # The primary serves the request without being presented as qualified: its own
    # failing run is reported as the reason, not as supporting evidence.
    fields = completion_fields(caplog)
    assert fields["routing_reason"] == "no_qualified_candidate"
    assert fields["routing_quality_verified"] is False
    assert fields["routing_primary_exclusion"] == "below_quality"
    assert "routing_evidence_run_id" not in fields


async def test_insufficient_evidence_can_reject_before_any_reservation(
    redis_server: RedisServer,
) -> None:
    names = Names.random()
    providers = {
        names.cheap: ok_provider(names.cheap),
        names.premium: ok_provider(names.premium),
    }
    raw = raw_config(names, on_insufficient_evidence="reject")
    async with routed_gateway(redis_server.url, raw, names, providers) as gateway:
        response = await gateway.post("quality")
        rows = await gateway.ledger()

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "no_eligible_route"
    assert rows == []
    assert not providers[names.cheap].requests
    assert not providers[names.premium].requests


async def test_budget_is_charged_at_the_selected_price(redis_server: RedisServer) -> None:
    names = Names.random()
    providers = {
        names.cheap: ok_provider(names.cheap),
        names.premium: ok_provider(names.premium),
    }
    # Room for the cheap reservation, not for the premium one.
    raw = raw_config(names, daily_budget=reservation(PREMIUM_RATE) - 1)
    async with routed_gateway(redis_server.url, raw, names, providers) as gateway:
        routed = await gateway.post()
        fixed = await gateway.post("fixed")

    assert routed.status_code == 200
    assert fixed.status_code == 429
    assert fixed.json()["error"]["code"] == "key_daily_budget_exceeded"
    assert len(providers[names.cheap].requests) == 1
    assert providers[names.premium].requests == []


async def test_concurrent_routed_requests_never_overspend(redis_server: RedisServer) -> None:
    names = Names.random()
    providers = {
        names.cheap: ok_provider(names.cheap),
        names.premium: ok_provider(names.premium),
    }
    admitted = 4
    raw = raw_config(names, daily_budget=admitted * reservation(CHEAP_RATE))
    async with routed_gateway(redis_server.url, raw, names, providers) as gateway:
        responses = await asyncio.gather(*(gateway.post() for _ in range(20)))
        rows = await gateway.ledger()

    statuses = [response.status_code for response in responses]
    assert statuses.count(200) == admitted
    assert statuses.count(429) == 20 - admitted
    assert len(providers[names.cheap].requests) == admitted
    assert sum(row["charge_micro_usd"] for row in rows) == admitted * reservation(CHEAP_RATE)


async def test_fallback_candidate_gets_its_own_reservation(redis_server: RedisServer) -> None:
    names = Names.random()
    refused = ProviderUnavailableError("connect failed", may_have_processed=False)
    providers = {
        names.cheap: FakeProvider(names.cheap, error=refused),
        names.premium: ok_provider(names.premium, Usage(5, 3, 8)),
    }
    async with routed_gateway(redis_server.url, raw_config(names), names, providers) as gateway:
        response = await gateway.post()
        rows = await gateway.ledger()

    assert response.status_code == 200
    assert [(r["provider"], r["state"]) for r in rows] == [
        (names.cheap, "released"),
        (names.premium, "settled"),
    ]
    assert len({r["request_id"] for r in rows}) == 1
    assert [r["reserved_micro_usd"] for r in rows] == [
        reservation(CHEAP_RATE),
        reservation(PREMIUM_RATE),
    ]
    assert [r["charge_micro_usd"] for r in rows] == [0, 3 * PREMIUM_RATE]


async def test_uncertain_failure_is_not_redirected(redis_server: RedisServer) -> None:
    names = Names.random()
    uncertain = ProviderUnavailableError("server error", may_have_processed=True)
    providers = {
        names.cheap: FakeProvider(names.cheap, error=uncertain),
        names.premium: ok_provider(names.premium),
    }
    async with routed_gateway(redis_server.url, raw_config(names), names, providers) as gateway:
        response = await gateway.post()
        rows = await gateway.ledger()

    assert response.status_code == 502
    assert providers[names.premium].requests == []
    assert [(r["provider"], r["state"]) for r in rows] == [(names.cheap, "unresolved")]


class _TrippedBreaker(CircuitBreaker):
    """Reports itself available to the router, then refuses at dispatch, as when
    another request opens the breaker between routing and dispatch."""

    def available(self) -> bool:
        return True

    def try_acquire(self) -> Permit | None:
        return None


async def test_outage_between_routing_and_dispatch_moves_to_the_next_candidate(
    redis_server: RedisServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = Names.random()
    providers = {
        names.cheap: ok_provider(names.cheap),
        names.premium: ok_provider(names.premium),
    }
    original = CircuitBreaker.__init__

    def breakers(self: CircuitBreaker, provider: str, *args: Any, **kwargs: Any) -> None:
        original(self, provider, *args, **kwargs)
        if provider == names.cheap:
            self.__class__ = _TrippedBreaker

    monkeypatch.setattr(CircuitBreaker, "__init__", breakers)
    async with routed_gateway(redis_server.url, raw_config(names), names, providers) as gateway:
        response = await gateway.post()
        rows = await gateway.ledger()
        metrics = gateway.app.state.metrics

    assert response.status_code == 200
    assert providers[names.cheap].requests == []
    # The refused candidate never reached budget reservation.
    assert [r["provider"] for r in rows] == [names.premium]
    assert (
        metrics.registry.get_sample_value(
            "gateway_provider_attempts_total",
            {"provider": names.cheap, "outcome": "circuit_open"},
        )
        == 1
    )


async def test_pricing_change_reroutes_without_rewriting_history(
    redis_server: RedisServer,
) -> None:
    names = Names.random()
    key = f"routing-test-{uuid.uuid4()}"
    providers = {
        names.cheap: ok_provider(names.cheap),
        names.premium: ok_provider(names.premium),
    }
    async with routed_gateway(
        redis_server.url, raw_config(names), names, providers, key=key
    ) as gateway:
        assert (await gateway.post()).status_code == 200
    repriced = raw_config(
        names,
        pricing=[
            price(names.cheap, "small", CHEAP_RATE),
            {**price(names.cheap, "small", 9, "v2"), "effective_at": "2024-01-01T00:00:00Z"},
            price(names.premium, "large", PREMIUM_RATE),
        ],
    )
    async with routed_gateway(redis_server.url, repriced, names, providers, key=key) as gateway:
        assert (await gateway.post()).status_code == 200
        rows = await gateway.ledger()

    assert [(r["provider"], r["pricing_version"], r["reserved_micro_usd"]) for r in rows] == [
        (names.cheap, "v1", reservation(CHEAP_RATE)),
        (names.premium, "v1", reservation(PREMIUM_RATE)),
    ]


async def test_routed_stream_cancellation_settles_the_selected_attempt(
    redis_server: RedisServer, redis_client: Redis
) -> None:
    names = Names.random()
    key = f"routing-test-{uuid.uuid4()}"
    body = ControlledStream(sse_body(openai_chunk("partial"), done=False))
    stub = UpstreamStub(lambda _: httpx.Response(200, headers=SSE_HEADERS, stream=body))
    raw = raw_config(names, cheap_capabilities=["streaming", "stop"])

    def factory(name: str, config: ProviderConfig, api_key: SecretStr | None) -> Provider:
        return build_provider(name, config, api_key, transport=stub)

    app = build_app(
        make_settings(redis_url=redis_server.url, database_url=database_url(), api_keys=(key,)),
        make_config(raw, dict.fromkeys(raw["providers"])),
        factory,
    )
    async with (
        run_server(app) as url,
        httpx.AsyncClient(base_url=url, headers={"Authorization": f"Bearer {key}"}) as client,
        asyncio.timeout(5),
    ):
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={"model": "classify", "messages": MESSAGES, "stream": True},
        ) as response:
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if '"partial"' in line:
                    break
        await body.closed.wait()

        async def settled() -> bool:
            async with app.state.cost_store.engine.connect() as connection:
                row = (
                    await connection.execute(
                        text("""SELECT provider, state, charge_micro_usd FROM cost_attempts
                        WHERE principal_id=:principal"""),
                        {"principal": principal_id(key)},
                    )
                ).one_or_none()
            return row is not None and tuple(row) == (
                names.cheap,
                "unresolved",
                reservation(CHEAP_RATE),
            )

        async def lease_released() -> bool:
            leases = f"{KEY_PREFIX}:{{{principal_id(key)}}}:leases"
            return bool(await redis_client.zcard(leases) == 0)

        await eventually(settled)
        await eventually(lease_released)
    assert stub.requests[0].url.host == "cheap.test"


async def test_database_outage_after_routing_fails_closed(redis_server: RedisServer) -> None:
    url = urlsplit(database_url())
    assert url.hostname is not None
    proxy = TcpProxy(url.hostname, url.port or 5432)
    await proxy.start()
    proxied = urlunsplit(
        url._replace(netloc=f"{url.netloc.rpartition('@')[0]}@127.0.0.1:{proxy.port}")
    )
    names = Names.random()
    providers = {
        names.cheap: ok_provider(names.cheap),
        names.premium: ok_provider(names.premium),
    }
    try:
        async with routed_gateway(
            redis_server.url, raw_config(names), names, providers, db_url=proxied
        ) as gateway:
            await proxy.stop()
            response = await gateway.post()
    finally:
        await proxy.stop()

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "admission_unavailable"
    assert providers[names.cheap].requests == []
    assert providers[names.premium].requests == []


async def test_redis_outage_fails_closed_before_reservation(redis_server: RedisServer) -> None:
    names = Names.random()
    providers = {
        names.cheap: ok_provider(names.cheap),
        names.premium: ok_provider(names.premium),
    }
    async with routed_gateway(redis_server.url, raw_config(names), names, providers) as gateway:
        await redis_server.stop()
        response = await gateway.post()
        rows = await gateway.ledger()

    assert response.status_code == 503
    assert rows == []
    assert providers[names.cheap].requests == []


async def test_clients_cannot_choose_providers_or_read_routing(redis_server: RedisServer) -> None:
    names = Names.random()
    providers = {
        names.cheap: ok_provider(names.cheap),
        names.premium: ok_provider(names.premium),
    }
    async with routed_gateway(redis_server.url, raw_config(names), names, providers) as gateway:
        anonymous = await gateway.client.post(
            "/v1/chat/completions", json={"model": "classify", "messages": MESSAGES}
        )
        metrics = await gateway.client.get("/metrics")
        provider_field = await gateway.post(provider=names.premium)
        metadata = await gateway.post(metadata={"task": "extraction"})
        header = await gateway.client.post(
            "/v1/chat/completions",
            json={"model": "classify", "messages": MESSAGES},
            headers={**gateway.headers, "X-Gateway-Provider": names.premium},
        )

    assert anonymous.status_code == 401
    assert metrics.status_code == 401
    assert provider_field.json()["error"]["code"] == "unknown_parameter"
    assert metadata.json()["error"]["code"] == "unsupported_parameter"
    # Headers are not routing inputs.
    assert header.status_code == 200
    assert providers[names.premium].requests == []
    assert len(providers[names.cheap].requests) == 1


async def test_rollout_control_group_uses_the_primary(redis_server: RedisServer) -> None:
    names = Names.random()
    providers = {
        names.cheap: ok_provider(names.cheap),
        names.premium: ok_provider(names.premium),
    }
    raw = raw_config(names, rollout_percent=0)
    async with routed_gateway(redis_server.url, raw, names, providers) as gateway:
        assert (await gateway.post()).status_code == 200

    assert len(providers[names.premium].requests) == 1


async def test_two_instances_make_identical_decisions_against_one_budget(
    redis_server: RedisServer,
) -> None:
    names = Names.random()
    key = f"routing-test-{uuid.uuid4()}"
    providers = {
        names.cheap: ok_provider(names.cheap),
        names.premium: ok_provider(names.premium),
    }
    admitted = 3
    raw = raw_config(names, daily_budget=admitted * reservation(CHEAP_RATE))
    async with (
        routed_gateway(redis_server.url, raw, names, providers, key=key) as first,
        routed_gateway(redis_server.url, raw, names, providers, key=key) as second,
    ):
        responses = await asyncio.gather(
            *((first if index % 2 else second).post() for index in range(12))
        )
        rows = await first.ledger()

    assert [r.status_code for r in responses].count(200) == admitted
    assert {r["provider"] for r in rows} == {names.cheap}
    assert providers[names.premium].requests == []


async def test_completion_log_records_the_decision_without_content(
    redis_server: RedisServer, caplog: pytest.LogCaptureFixture
) -> None:
    names = Names.random()
    providers = {
        names.cheap: ok_provider(names.cheap),
        names.premium: ok_provider(names.premium),
    }
    caplog.set_level(logging.INFO, logger="gateway.services.completion")
    async with routed_gateway(redis_server.url, raw_config(names), names, providers) as gateway:
        assert (await gateway.post()).status_code == 200
        [row] = await gateway.ledger()

    fields = completion_fields(caplog)
    assert fields["routing_policy"] == "lowest_cost"
    assert fields["routing_reason"] == "lowest_cost"
    assert fields["routing_quality_verified"] is False
    assert fields["provider"] == names.cheap
    assert fields["upstream_model"] == "small"
    assert fields["routing_chain"] == [f"{names.cheap}/small", f"{names.premium}/large"]
    assert fields["routing_maximum_micro_usd"] == row["reserved_micro_usd"]
    assert fields["routing_pricing_version"] == "v1"
    assert fields["routing_estimated_input_tokens"] == estimate_input_tokens(
        [ChatMessage.model_validate(message) for message in MESSAGES]
    )
    assert fields["cost_request_id"] == str(row["request_id"])
    assert len(fields["routing_policy_version"]) == 16
    assert "charged twice" not in str(fields)
