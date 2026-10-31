"""Closed-loop load generator for the gateway (or, for a baseline, the mock provider).

Sends a fixed number of chat completion requests with bounded concurrency,
round-robin across the given base URLs, and reports status counts, throughput, and
latency percentiles. With --mock-url it also reports the mock provider's peak
concurrency per model; with --metrics it prints the gateways' admission counters.

Example, against two gateways started with `docker compose --profile multi up`:

    uv run python tools/loadtest.py --url http://127.0.0.1:8000 --url http://127.0.0.1:8001 \\
        --api-key "$GATEWAY_API_KEYS" --requests 2000 --concurrency 64 \\
        --mock-url http://127.0.0.1:9000 --metrics

Baseline without the gateway (the mock accepts any bearer token):

    uv run python tools/loadtest.py --url http://127.0.0.1:9000 --model mock-small \\
        --api-key any --requests 2000 --concurrency 64
"""

import argparse
import asyncio
import itertools
import os
import statistics
import time
from collections import Counter
from dataclasses import dataclass

import httpx

_ADMISSION_METRICS = (
    "gateway_admission_decisions_total",
    "gateway_reservations_settled_total",
    "gateway_charged_tokens_total",
    "gateway_redis_errors_total",
)


@dataclass(frozen=True)
class Result:
    status: int
    seconds: float


async def run(args: argparse.Namespace) -> list[Result]:
    semaphore = asyncio.Semaphore(args.concurrency)
    urls = itertools.cycle(args.url)
    body = {
        "model": args.model,
        "messages": [{"role": "user", "content": "Explain database indexing."}],
        "stream": args.stream,
        "max_tokens": args.max_tokens,
    }
    headers = {"Authorization": f"Bearer {args.api_key}"}
    limits = httpx.Limits(
        max_connections=args.concurrency, max_keepalive_connections=args.concurrency
    )

    async with httpx.AsyncClient(limits=limits, timeout=args.timeout) as client:

        async def one(url: str) -> Result:
            async with semaphore:
                started = time.perf_counter()
                try:
                    async with client.stream(
                        "POST", f"{url}/v1/chat/completions", json=body, headers=headers
                    ) as response:
                        await response.aread()
                        status = response.status_code
                except httpx.HTTPError:
                    status = 0
                return Result(status, time.perf_counter() - started)

        return await asyncio.gather(*(one(next(urls)) for _ in range(args.requests)))


def percentiles(values: list[float]) -> str:
    if len(values) < 2:
        return "n/a"
    cuts = statistics.quantiles(values, n=100, method="inclusive")
    return " ".join(f"p{p}={cuts[p - 1] * 1000:.1f}ms" for p in (50, 95, 99))


def report(results: list[Result], elapsed: float) -> None:
    statuses = Counter(result.status for result in results)
    print(f"requests: {len(results)} in {elapsed:.2f}s ({len(results) / elapsed:.1f}/s)")
    print("statuses:", dict(sorted(statuses.items())), "(0 = transport error)")
    print("latency, all:     ", percentiles([r.seconds for r in results]))
    print("latency, 200 only:", percentiles([r.seconds for r in results if r.status == 200]))


async def print_side_channels(args: argparse.Namespace) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        if args.mock_url:
            stats = (await client.get(f"{args.mock_url}/stats")).json()
            print("mock provider:", stats)
        if args.metrics:
            for url in args.url:
                text = (
                    await client.get(
                        f"{url}/metrics", headers={"Authorization": f"Bearer {args.api_key}"}
                    )
                ).text
                lines = [line for line in text.splitlines() if line.startswith(_ADMISSION_METRICS)]
                print(f"{url}/metrics:")
                for line in lines:
                    print("  ", line)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", action="append", required=True, help="base URL; repeatable")
    parser.add_argument("--api-key", default=os.environ.get("LOADTEST_API_KEY", ""))
    parser.add_argument("--model", default="fast")
    parser.add_argument("--requests", type=int, default=1000)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--max-tokens", type=int, default=64)
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--mock-url", help="mock provider URL, to report peak concurrency")
    parser.add_argument("--reset-mock", action="store_true", help="clear mock stats first")
    parser.add_argument("--metrics", action="store_true", help="print gateway admission metrics")
    args = parser.parse_args()

    if args.reset_mock and args.mock_url:
        async with httpx.AsyncClient() as client:
            await client.post(f"{args.mock_url}/stats/reset")
    started = time.perf_counter()
    results = await run(args)
    report(results, time.perf_counter() - started)
    await print_side_channels(args)


if __name__ == "__main__":
    asyncio.run(main())
