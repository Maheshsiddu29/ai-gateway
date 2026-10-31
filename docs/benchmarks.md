# Benchmarks

Measurements of the Compose stack with `tools/loadtest.py`. They describe one laptop
running Docker Desktop and are not capacity figures for any other deployment.
Correctness under concurrency is established by the test suite
(`tests/integration/test_multi_instance_load.py` and the Redis integration tests),
not by these runs.

## Environment

| | |
| --- | --- |
| Machine | Apple M3 Pro, 12 cores, 18 GiB RAM, macOS 26.2 |
| Docker | Docker Desktop, engine 29.6.2, VM with 12 CPUs and 7.75 GiB |
| Stack | `docker compose --profile multi up --build`: two gateway containers, the mock provider, Redis 8.10.3 |
| Gateway | one uvicorn process per container, admission control enabled, JSON logs to stdout |
| Client | `tools/loadtest.py` on the host, through Docker Desktop port forwarding |

The mock provider is a single Python process. In these runs it, the client, and
Docker Desktop's port forwarding are all on the request path, and they limit
throughput at higher concurrency more than the gateway does.

## Commands

```sh
export GATEWAY_API_KEYS=$(openssl rand -hex 32)
docker compose --profile multi up --build --wait

# Baseline: the mock provider directly, without the gateway.
uv run python tools/loadtest.py --url http://127.0.0.1:9000 --model mock-small \
    --api-key any --requests 500 --concurrency 4

# Both gateways, round-robin.
uv run python tools/loadtest.py --url http://127.0.0.1:8000 --url http://127.0.0.1:8001 \
    --api-key "$GATEWAY_API_KEYS" --requests 500 --concurrency 4 --metrics

# Limit enforcement: slow provider, client concurrency above the per-key limit.
MOCK_RESPONSE_DELAY_SECONDS=0.2 docker compose --profile multi up -d --wait mock-upstream
uv run python tools/loadtest.py --url http://127.0.0.1:8000 --url http://127.0.0.1:8001 \
    --api-key "$GATEWAY_API_KEYS" --requests 1000 --concurrency 128 \
    --mock-url http://127.0.0.1:9000 --reset-mock --metrics
```

`config/gateway.compose.toml` sets 6000 requests per minute (burst 1000),
6,000,000 tokens per minute (burst 1,000,000), and 64 concurrent requests per key.
Redis was restarted (`docker compose restart redis`) before each scenario below so
that it started with full buckets. Each request is non-streaming with
`max_tokens = 64`.

## Results (2026-10-31)

### Gateway overhead at low concurrency

Client concurrency 4, 500 requests, mock provider without delay. Two runs each.

| Path | Throughput | p50 | p95 | p99 |
| --- | --- | --- | --- | --- |
| Mock directly | 1473, 1531 req/s | 2.0, 2.1 ms | 2.9, 3.0 ms | 8.3, 4.8 ms |
| Two gateways | 373, 392 req/s | 9.8, 9.3 ms | 15.3, 15.0 ms | 21.5, 21.6 ms |

The gateway path added about 7.5 ms at the median. That includes a second port
forward and container network hop, two Redis script calls (admit and settle), JSON
parsing and encoding, and three log lines per request.

### Higher concurrency

Client concurrency 32, 900 requests, mock provider without delay, after a Redis
restart.

| Path | Throughput | Statuses | p50 | p95 | p99 |
| --- | --- | --- | --- | --- | --- |
| Mock directly | 287 req/s | 900 × 200 | 73.9 ms | 328.2 ms | 455.1 ms |
| Two gateways | 252 req/s | 900 × 200 | 95.0 ms | 285.0 ms | 444.3 ms |

The direct path is slower here than at concurrency 4, so these figures are dominated
by the client, port forwarding, and mock rather than by the gateway.

A first run of this test, before commit 99731ad, refused 9 of 900 requests with
`admission_unavailable` after the Redis restart; see that commit for the cause and
fix. The run above was taken after the fix, with no Redis errors.

### Limit enforcement

Client concurrency 128, 1000 requests, mock provider delay 200 ms, limit 64
concurrent requests per key.

| Measurement | Value |
| --- | --- |
| Statuses | 786 × 200, 213 × 429, 1 transport error |
| Concurrency rejections in gateway metrics | 112 + 101 = 213 |
| Peak concurrent requests seen by the provider | 64 |
| Requests that reached the provider | 786 (equal to admissions) |
| Leases left in Redis afterwards | 0 |
| Reservation records afterwards | 786, all `settled` |
| Gateway log errors | 0 |

The transport error did not reach admission (admission counters account for every
other request) and was not investigated further; it occurred between the client
and Docker Desktop's port forwarding. Latencies in this run (p50 1.3 s) reflect
queueing at the client and the single-process mock, not admission control.
