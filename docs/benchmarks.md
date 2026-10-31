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

## Audit run at commit 330234a

[Raw measurements and memory samples](benchmark-audit.json) record the final audit
run. The environment was an Apple M3 Pro (12 cores, 18 GiB RAM), macOS 26.2,
Docker Engine 29.6.2, and a Docker VM with 12 CPUs and 7.75 GiB RAM. Python was
3.13.18. Compose ran two gateways, Redis 8.10.3, and the mock provider with a
50 ms non-streaming response delay. No test suite ran concurrently. These are
single runs on a laptop, including Docker networking and memory-sampling overhead;
they are not capacity targets or production guarantees.

All calls used the same three-word prompt and `max_tokens=64`. The key had a
concurrency limit of 64, request burst 1000/refill 6000 per minute, and token
burst 1000000/refill 6000000 per minute. Redis and mock statistics were reset
before each scenario. Percentiles below include all responses, including 429s.

- **baseline**: 500 requests at client concurrency 4; 68.6 requests/s; p50/p95/p99 57.6/62.0/64.0 ms. Statuses: {'200': 500}. Provider peak concurrency 4.
- **gateway**: 500 requests at client concurrency 4; 62.1 requests/s; p50/p95/p99 62.9/73.0/79.4 ms. Statuses: {'200': 500}. Provider peak concurrency 4.
- **saturation**: 1000 requests at client concurrency 128; 100.3 requests/s; p50/p95/p99 709.6/3574.1/5453.7 ms. Statuses: {'200': 936, '429': 64}. Provider peak concurrency 62.

The low-concurrency median difference between gateway and direct provider was
5.3 ms in this run. The stress run admitted 936 requests and rejected 64 with
429; the mock saw exactly 936 requests and peak concurrency 62. There were no
remaining leases or unsettled reservation records after either gateway scenario.
No transport errors occurred.

Memory was sampled with `docker stats --no-stream`, approximately every two seconds
(the CLI itself waits for a measurement). This is Docker's displayed working-set
usage, not an absolute high-water mark or a leak test. In the stress run, sampled
peaks were:

- `gateway-audit-gateway-1`: 64.50 MiB.
- `gateway-audit-gateway-2-1`: 65.46 MiB.
- `gateway-audit-mock-upstream-1`: 33.00 MiB.
- `gateway-audit-redis-1`: 13.38 MiB.

Reproduce the workload from a clean local stack:

```sh
export GATEWAY_API_KEYS=$(openssl rand -hex 32)
export LOADTEST_API_KEY="$GATEWAY_API_KEYS"
MOCK_RESPONSE_DELAY_SECONDS=0.05 docker compose -p gateway-audit --profile multi up -d --build --wait

# Before each scenario:
docker compose -p gateway-audit exec -T redis redis-cli FLUSHDB
curl -fsS -X POST http://127.0.0.1:9000/stats/reset

# Baseline:
LOADTEST_API_KEY=any uv run python tools/loadtest.py --url http://127.0.0.1:9000 --model mock-small --requests 500 --concurrency 4

# Two gateways, low concurrency:
uv run python tools/loadtest.py --url http://127.0.0.1:8000 --url http://127.0.0.1:8001 --requests 500 --concurrency 4 --mock-url http://127.0.0.1:9000 --metrics

# Two gateways, saturation:
uv run python tools/loadtest.py --url http://127.0.0.1:8000 --url http://127.0.0.1:8001 --requests 1000 --concurrency 128 --mock-url http://127.0.0.1:9000 --metrics

# Repeat in another terminal during each run to sample memory:
docker stats --no-stream gateway-audit-gateway-1 gateway-audit-gateway-2-1 gateway-audit-mock-upstream-1 gateway-audit-redis-1

docker compose -p gateway-audit --profile multi down
```

`FLUSHDB` above targets only this disposable, unpersisted Compose Redis. Do not run
that command against an existing admission database.

## Phase 3 cost-accounting measurements (2026-10-31)

These are local measurements, not service-level targets or vendor API results. Host: macOS 26.2 arm64, PostgreSQL 16.2 on loopback for the store benchmark, Docker Desktop 29.6.2 for the gateway stack. The mock provider is deterministic and charges no real tokens. Results depend on the host, connection pool, and workload.

The store benchmark used `tools/bench_cost.py` with 200 attempts, concurrency 20, 50 estimated input tokens, 50 maximum output tokens, 60 observed tokens, and an illustrative $1 per million input/output token price:

```sh
uv run python tools/bench_cost.py \
  --database-url "$GATEWAY_TEST_DATABASE_URL" \
  --requests 200 --concurrency 20
```

| Measurement | Observed |
| --- | ---: |
| Reservation latency p50 / p95 / p99 | 10.088 / 44.004 / 49.920 ms |
| Concurrent reserve-dispatch-settle throughput | 580.09 attempts/s |
| PostgreSQL `BEGIN` + `SELECT 1` + `COMMIT` p50 / p95 / p99 | 0.474 / 0.622 / 0.622 ms |
| `EXPLAIN ANALYZE` provider/model summary query on 200 attempts | 0.170 ms execution time |
| Four independent processes, 40 simultaneous reservations, 10-unit shared budget | 10 accepted, 30 rejected (`test_multiple_processes_share_one_budget`) |
| Two gateway instances, 20 concurrent requests, one-attempt shared budget | 1 accepted, 19 rejected (`test_two_gateway_instances_do_not_over_admit_shared_budget`) |

For the gateway measurement, Compose ran one gateway, Redis, PostgreSQL, and the local mock provider. The mock used a fixed 50 ms response delay. The command below issued 500 requests with concurrency 20 and `max_tokens=64`; container memory was sampled with `docker stats --no-stream` during the run:

```sh
MOCK_RESPONSE_DELAY_SECONDS=0.05 GATEWAY_API_KEYS=local-test-key-0123456789abcdef docker compose up -d --build
uv run python tools/loadtest.py --url http://127.0.0.1:8000 \
  --api-key local-test-key-0123456789abcdef --model fast --requests 500 \
  --concurrency 20 --max-tokens 64 --mock-url http://127.0.0.1:9000 --reset-mock
```

| Measurement | Observed |
| --- | ---: |
| HTTP statuses | 500 × 200 |
| Gateway latency p50 / p95 / p99 | 103.6 / 185.7 / 354.9 ms |
| Throughput | 170.6 requests/s |
| Mock peak concurrent requests | 20 |
| Gateway container memory samples | 75.95 and 77.39 MiB; sampled peak 77.39 MiB |

The memory figure is the highest of two sampled container readings, not a continuous high-water mark. The store throughput includes three financial transactions per attempt; reservation latency covers the first transaction only. The query timing is PostgreSQL's execution time and excludes HTTP serialization and client network time.

## Phase 4 routing measurements (2026-10-31)

Local measurements on the same laptop (Apple M3 Pro, macOS 26.2, Python 3.13.18,
Docker Desktop 29.6.2). They are not capacity figures, and no real provider was
called.

### Routing decision and planning overhead

`tools/bench_routing.py` runs on one event loop, in process, with an instant
in-memory provider and no Redis or PostgreSQL. Candidates use the `stop`
capability only, all are eligible, and the quality policy has evidence for each.
20,000 iterations after 1,000 warm-up calls; two runs, both shown.

```sh
uv run python tools/bench_routing.py --iterations 20000
```

`Router.decide`, the decision alone:

| Policy / candidates | p50 | p95 | p99 | Decisions/s |
| --- | --- | --- | --- | --- |
| lowest_cost / 2 | 5.9, 5.8 µs | 6.0, 6.0 µs | 6.3, 6.2 µs | 169k, 171k |
| quality_constrained / 2 | 9.4, 9.4 µs | 10.0, 9.8 µs | 12.3, 12.0 µs | 103k, 105k |
| lowest_cost / 4 | 10.0, 10.1 µs | 11.4, 10.3 µs | 13.0, 10.5 µs | 98k, 99k |
| quality_constrained / 4 | 15.3, 15.5 µs | 16.0, 15.8 µs | 18.4, 17.0 µs | 65k, 64k |
| lowest_cost / 8 | 18.1, 18.2 µs | 18.4, 19.4 µs | 19.6, 23.0 µs | 55k, 54k |
| quality_constrained / 8 | 27.1, 27.3 µs | 27.6, 28.3 µs | 28.6, 32.6 µs | 37k, 36k |

`CompletionService.complete` with the instant provider. The difference from the
fixed alias is the planning overhead added by routing. That overhead covers
building and validating the request for every target, the decision itself,
metrics, and assembling log fields. About 1 µs of it is each
`dataclasses.replace` call for a per-candidate request.

| Alias | p50 | p95 | p99 |
| --- | --- | --- | --- |
| fixed | 10.6, 10.7 µs | 11.0, 11.3 µs | 12.2, 13.4 µs |
| lowest_cost / 2 | 26.6, 26.7 µs | 30.9, 29.1 µs | 36.1, 34.2 µs |
| quality_constrained / 2 | 32.2, 31.3 µs | 39.0, 33.0 µs | 56.4, 37.4 µs |
| lowest_cost / 8 | 49.6, 49.6 µs | 53.1, 53.5 µs | 60.8, 62.0 µs |
| quality_constrained / 8 | 62.4, 62.5 µs | 67.1, 66.8 µs | 74.5, 73.5 µs |

These runs were taken before the evidence output-limit rule was added. One
re-run afterwards measured `decide` p50 between 6.2 µs (lowest_cost / 2) and
27.9 µs (quality_constrained / 8), and `complete` p50 between 27.5 µs and 65.9 µs.
Those are within 6% of the figures above, comparable to the run-to-run variation.

Memory allocated during one decision (tracemalloc peak) was 1,168 bytes for
lowest_cost with 2 candidates and 3,152 bytes for quality_constrained with 8.
Decisions retain nothing after the request.

For the fixed path, the same script was run against the Phase 3 tree (commit
a2eb59e) and this tree, three times each, 20,000 iterations:

| Tree | p50 | p95 | p99 |
| --- | --- | --- | --- |
| Phase 3 | 9.12, 9.08, 8.96 µs | 10.67, 9.38, 9.29 µs | 11.96, 10.75, 10.54 µs |
| Phase 4 | 10.67, 10.62, 10.58 µs | 11.08, 10.92, 10.92 µs | 12.46, 12.25, 12.25 µs |

The fixed path gained about 1.5 µs, mostly the planning-duration histogram and the
extra log fields.

### Database work

Routing makes no database query per request. The evidence snapshot is one query
per refresh interval (default 60 s). With 2,032 runs in `evaluation_runs`, 50
consecutive loads took p50 7.39 ms, p95 8.47 ms, max 8.85 ms, including
connection checkout. `EXPLAIN ANALYZE` reported 4.2 ms execution time for the
query. Per-attempt PostgreSQL reservations are unchanged from Phase 3.

### End-to-end through Compose

Compose ran two gateways, Redis, PostgreSQL, and the mock provider without delay.
`fast` is a fixed alias to `mock-openai/mock-small`. `routed` is a `lowest_cost`
alias that selects `mock-openai/mock-routed-small`, so both reach the same mock
endpoint. Redis was restarted before each scenario. The runs alternated, using
`max_tokens = 64`, round-robin across both gateways.

```sh
uv run python tools/loadtest.py --url http://127.0.0.1:8000 --url http://127.0.0.1:8001 \
    --api-key "$GATEWAY_API_KEYS" --model routed --requests 500 --concurrency 4
```

| Alias | Concurrency, requests | Throughput | p50 | p95 | p99 |
| --- | --- | --- | --- | --- | --- |
| fast | 4, 500 (two runs) | 183, 205 req/s | 19.9, 18.6 ms | 27.9, 24.8 ms | 38.5, 32.9 ms |
| routed | 4, 500 (two runs) | 185, 183 req/s | 20.0, 20.3 ms | 29.5, 31.2 ms | 45.5, 46.9 ms |
| fast | 20, 1000 | 188 req/s | 88.6 ms | 228.9 ms | 308.9 ms |
| routed | 20, 1000 | 177 req/s | 91.1 ms | 241.0 ms | 498.5 ms |

All requests returned 200. The differences are within the run-to-run variation of
this setup: the two fixed runs differ by 1.3 ms at p50. The tail at concurrency 20
is dominated by the client, port forwarding, and single-process mock, as in
earlier phases. Sampled gateway container memory (`docker stats --no-stream`, five
samples per run) peaked at 78.0 MiB in the fixed run and 79.2 MiB in the routed
run.

### Simulated policy comparison

The commands below exercise the evaluation and experiment tooling with
`SimulatedProvider`. The prices in `evaluations/experiments/ticket-routing.toml`
are fictional. The accuracies are inputs to the simulation, not measurements.
**These numbers show what the tools report. They are not evidence of savings or
quality for any real model.**

```sh
uv run python -m gateway.evaluation run --config evaluations/experiments/ticket-routing.toml \
  --dataset evaluations/datasets/support-ticket-category-v1.jsonl \
  --target sim/small --target sim/medium --target sim/large --max-tokens 8 \
  --output results/tickets \
  --simulate sim/small=0.80 --simulate sim/medium=0.93 --simulate sim/large=0.98
uv run python tools/routing_experiment.py --config evaluations/experiments/ticket-routing.toml \
  --alias classify --dataset evaluations/datasets/support-ticket-category-v1.jsonl \
  --results results/tickets/*.json --allow-simulated
```

The simulation is deterministic, so these commands reproduce the table below.
Thresholds: `min_success_rate = 0.75` at the Wilson lower bound, `min_samples =
40`. Evidence and workload are the same runs (in-sample).

| Dataset (cases) | Policy | Routed to | Success (95% CI) | Total cost | Savings vs fixed |
| --- | --- | --- | --- | --- | --- |
| support-ticket-category (44) | fixed | sim/large | 1.000 (0.920–1.000) | 34,440 µUSD | baseline |
| | lowest_cost | sim/small | 0.818 (0.680–0.905) | 1,142 µUSD | 96.68% |
| | quality_constrained | sim/medium | 0.977 (0.882–0.996) | 5,689 µUSD | 83.48% |
| invoice-fields (40) | fixed | sim/large | 0.975 (0.871–0.996) | 47,775 µUSD | baseline |
| | lowest_cost | sim/small | 0.750 (0.598–0.858) | 1,419 µUSD | 97.03% |
| | quality_constrained | sim/large | 0.975 (0.871–0.996) | 47,775 µUSD | 0.0% |

The invoice rows show the threshold working. `sim/medium` passed 35/40, whose
lower bound of 0.739 is below 0.75. With only 40 cases, quality-constrained
routing therefore kept every request on the expensive model. `lowest_cost` has no
quality rule and accepted a 0.75 pass rate. Latency in simulated runs is an
in-process function call and is not reported.

No real-provider evaluation has been run for this repository, so it contains no
measured cost or quality comparison for any vendor model. To produce one, run
`python -m gateway.evaluation run --live` for each candidate, preferably on two
dataset versions (one as evidence, one as workload). Then publish the dataset,
sample size, model IDs, prices and their dates, and the run dates with the
results.
