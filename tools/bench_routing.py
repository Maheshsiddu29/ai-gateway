"""In-process benchmark of the routing decision and its share of request handling.

Measures, on one event loop with an instant in-memory provider and no Redis or
PostgreSQL:

* ``Router.decide`` latency and decisions per second for each policy and number
  of candidates;
* ``CompletionService.complete`` latency for a fixed alias and for routed aliases,
  whose difference is the planning overhead added by routing;
* memory allocated while making one decision (tracemalloc peak).

Routing performs no I/O on the request path, so database and Redis costs are
unchanged by it and are measured separately (tools/bench_cost.py, tools/loadtest.py).

    uv run python tools/bench_routing.py --iterations 20000
"""

import argparse
import asyncio
import json
import platform
import time
import tracemalloc
import uuid
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

from gateway.core.config import GatewayConfig, ResilienceConfig
from gateway.core.metrics import Metrics
from gateway.core.security import Principal
from gateway.evaluation.stats import percentile
from gateway.providers.base import (
    Completion,
    ProviderRequest,
    StreamEnd,
    StreamEvent,
    TextDelta,
    Usage,
)
from gateway.resilience.deadline import Deadline
from gateway.routing.evidence import Evidence, EvidenceSnapshot
from gateway.routing.router import RequestProfile, Router
from gateway.schemas.completions import ChatCompletionRequest
from gateway.services.completion import CompletionService, routes_from_config
from gateway.services.execution import build_executor

PROMPT = "Classify this support ticket: I was charged twice for the same order."


class InstantProvider:
    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def validate(self, request: ProviderRequest) -> None:
        return

    async def complete(self, request: ProviderRequest) -> Completion:
        return Completion("billing", "stop", Usage(20, 1, 21))

    async def stream(self, request: ProviderRequest) -> AsyncGenerator[StreamEvent]:
        yield TextDelta("billing")
        yield StreamEnd("stop", Usage(20, 1, 21))

    async def aclose(self) -> None:
        return


def build_config(candidates: int) -> GatewayConfig:
    models = [f"m{index}" for index in range(candidates)]
    targets = [{"provider": "bench", "model": model} for model in models[1:]]
    quality = {
        "dataset": "tickets",
        "dataset_version": "1",
        "min_success_rate": 0.5,
        "min_samples": 40,
        "max_evidence_age_days": 30,
    }
    return GatewayConfig.model_validate(
        {
            "providers": {"bench": {"type": "openai_compatible", "base_url": "https://b.test/v1"}},
            "models": {
                "fixed": {"provider": "bench", "model": "m0", "max_output_tokens": 16},
                "lowest": {
                    "provider": "bench",
                    "model": "m0",
                    "max_output_tokens": 16,
                    "routing": {"policy": "lowest_cost", "candidates": targets},
                },
                "quality": {
                    "provider": "bench",
                    "model": "m0",
                    "max_output_tokens": 16,
                    "routing": {"policy": "quality_constrained", "candidates": targets, **quality},
                },
            },
            "cost": {
                "enabled": True,
                "pricing": [
                    {
                        "provider": "bench",
                        "model": model,
                        "version": "v1",
                        "effective_at": "2020-01-01T00:00:00Z",
                        # Descending prices: the last candidate is the cheapest.
                        "input_per_million_usd": str(candidates - index),
                        "output_per_million_usd": str(candidates - index),
                    }
                    for index, model in enumerate(models)
                ],
            },
            "routing": {
                "enabled": True,
                "models": [
                    {
                        "provider": "bench",
                        "model": model,
                        "context_window_tokens": 8192,
                        "max_output_tokens": 1024,
                        "capabilities": ["stop"],
                    }
                    for model in models
                ],
            },
        }
    )


def evidence_for(candidates: int) -> EvidenceSnapshot:
    now = datetime.now(UTC)
    return EvidenceSnapshot.of(
        {
            ("bench", f"m{index}", "tickets", "1"): Evidence(
                run_id=uuid.uuid4(),
                completed_at=now - timedelta(days=1),
                case_count=100,
                passed_count=95,
                latency_samples=100,
                latency_p95_ms=500.0,
                expected_output_tokens=2,
            )
            for index in range(candidates)
        }
    )


def _same(snapshot: EvidenceSnapshot) -> EvidenceSnapshot:
    return snapshot


def summarize(samples_ns: list[int]) -> dict[str, float]:
    micros = [sample / 1000 for sample in samples_ns]
    return {
        "p50_us": round(percentile(micros, 0.5), 2),
        "p95_us": round(percentile(micros, 0.95), 2),
        "p99_us": round(percentile(micros, 0.99), 2),
        "per_second": round(len(micros) / (sum(micros) / 1e6)),
    }


def time_sync(call: Callable[[], object], iterations: int) -> list[int]:
    for _ in range(min(1000, iterations)):
        call()
    samples = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        call()
        samples.append(time.perf_counter_ns() - started)
    return samples


def allocated_bytes(call: Callable[[], object]) -> int:
    call()
    tracemalloc.start()
    try:
        before, _ = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        call()
        _, peak = tracemalloc.get_traced_memory()
        return peak - before
    finally:
        tracemalloc.stop()


async def time_requests(service: CompletionService, alias: str, iterations: int) -> list[int]:
    request = ChatCompletionRequest.model_validate(
        {"model": alias, "messages": [{"role": "user", "content": PROMPT}]}
    )
    principal = Principal(id="key-000000000000")
    for _ in range(min(1000, iterations)):
        await service.complete(request, principal, Deadline(60))
    samples = []
    for _ in range(iterations):
        started = time.perf_counter_ns()
        await service.complete(request, principal, Deadline(60))
        samples.append(time.perf_counter_ns() - started)
    return samples


async def run(iterations: int, candidate_counts: list[int]) -> dict[str, Any]:
    results: dict[str, Any] = {
        "python": platform.python_version(),
        "machine": platform.machine(),
        "platform": platform.platform(),
        "iterations": iterations,
        "decide": {},
        "complete": {},
        "decision_allocated_bytes": {},
    }
    for count in candidate_counts:
        config = build_config(count)
        snapshot = evidence_for(count)
        provider = InstantProvider("bench")
        metrics = Metrics()
        executor = build_executor(
            {"bench": provider}, ResilienceConfig(max_attempts_per_provider=1), metrics
        )
        router = Router(config, executor.available, partial(_same, snapshot))
        profile = RequestProfile(frozenset(), 30, 16)
        for alias in ("lowest", "quality"):
            decide = partial(router.decide, alias, profile, {})
            label = f"{alias}/{count}"
            results["decide"][label] = summarize(time_sync(decide, iterations))
            results["decision_allocated_bytes"][label] = allocated_bytes(decide)
        service = CompletionService(
            routes_from_config(config), {"bench": provider}, executor, metrics, router=router
        )
        for alias in ("fixed", "lowest", "quality"):
            label = alias if alias == "fixed" else f"{alias}/{count}"
            if label not in results["complete"]:
                results["complete"][label] = summarize(
                    await time_requests(service, alias, iterations)
                )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--iterations", type=int, default=20_000)
    parser.add_argument("--candidates", type=int, nargs="+", default=[2, 4, 8])
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.iterations, args.candidates)), indent=2))


if __name__ == "__main__":
    main()
