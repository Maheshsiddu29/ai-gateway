# Offline evaluation

Quality-constrained routing uses quality that has been measured on a specific
task, never assumed. An evaluation runs a versioned dataset against one upstream
model through the gateway's own provider adapters, scores every answer with a
deterministic rule, and records the result in PostgreSQL. Routing reads only these
records.

A result says how a model did on that dataset, under those conditions. It is not a
general accuracy figure for the model, and passing a threshold does not make any
individual live answer correct.

## Datasets

Datasets are JSONL files under `evaluations/datasets/`. Each line is one case:

```json
{"id": "ticket-001", "dataset": "support-ticket-category", "dataset_version": "1",
 "task": "classification", "method": "exact_label",
 "messages": [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}],
 "expected": {"label": "account", "labels": ["account", "billing", "shipping", "technical"]}}
```

| Field | Rule |
| --- | --- |
| `id` | Unique in the file; letters, digits, `._-`, at most 64 characters. |
| `dataset`, `dataset_version` | The same on every line. Routing evidence is matched on both. |
| `partition` | `development` (the default, for files released before partitions) or `holdout`; the same on every line. |
| `task` | `classification` or `extraction`. |
| `method` | `exact_label` for classification, `field_f1` for extraction. |
| `messages` | 1 to 32 chat messages, the same message schema the API accepts. |
| `expected` | `{"label", "labels"}` for `exact_label`; `{"fields", "min_f1"}` for `field_f1`. |

The loader rejects unknown fields, missing expectations, a label outside its label
set, mixed datasets, versions, or partitions, duplicate IDs, lines over 64 KiB,
and more than 5000 cases. The run records the file's SHA-256, which identifies
exactly which bytes produced a result. A released dataset file is never edited; a
change gets a new `dataset_version`. `evaluations/manifest.toml` pins the hash of
every released file, and a unit test enforces it.

### Development and holdout partitions

A dataset version can have two files:

- the development partition, whose results may be recorded as routing evidence;
- the holdout partition, used only to measure a routing policy after its evidence
  is fixed. The gateway never reads holdout results as evidence, and the routing
  experiment refuses them as evidence.

```sh
uv run python -m gateway.evaluation check \
  --development evaluations/datasets/support-ticket-category-v1.jsonl \
  --holdout evaluations/datasets/support-ticket-category-v1-holdout.jsonl
```

`check` requires the same dataset name, version, task, method, system prompts,
and label sets in both files. It rejects shared case IDs, and holdout cases whose
non-system text shares at least 60% of its distinct words with a development case
(digits ignored). It detects copied cases, not paraphrases.

The repository ships two synthetic datasets, each with both partitions (see
[evaluations/README.md](../evaluations/README.md) for construction and known
biases): support tickets in four categories (44 development, 60 holdout), and
invoice texts with five fields to extract (40 and 40). They contain no customer
data. Do not add real customer prompts to the repository.

## Scoring

Scorers are pure functions of the case and the output text. Each method has a
version string (`EVALUATOR_VERSIONS` in `gateway/evaluation/scoring.py`) that must
change whenever its behaviour changes. Routing ignores runs scored by any other
version. The field counts added in results format 2 are reported alongside the
score and do not change it, so the versions are unchanged.

`exact_label-1`
: The output, after trimming whitespace, ignoring case, collapsing internal
  whitespace, and removing one trailing period, must equal one of the allowed
  labels. A different allowed label is a wrong answer. Anything else, such as a
  sentence around the label, is an invalid response. Both score 0.

`field_f1-1`
: The output must be one JSON object, optionally inside a single Markdown code
  fence. Otherwise the response is invalid. Keys and string values are normalized
  as above and compared as (field, value) pairs. Precision counts every non-null
  predicted field. Recall counts every expected field. The score is F1, and the
  case passes when F1 ≥ `min_f1` (1.0 in the shipped dataset). `null` means "not
  stated" and is not a prediction. Non-string values never match, because expected
  values are text.

A provider error or timeout fails the case and records its error code. It is not
retried, so unreliability lowers the score instead of being hidden.

## Running evaluations

```sh
# Simulated: no provider calls, results marked simulated.
uv run python -m gateway.evaluation run \
  --config config/gateway.toml \
  --dataset evaluations/datasets/support-ticket-category-v1.jsonl \
  --target openai/small-model-id --target anthropic/large-model-id \
  --max-tokens 8 --output results/ \
  --simulate openai/small-model-id=0.8 --simulate anthropic/large-model-id=0.95

# Real providers. This costs money.
uv run --env-file .env python -m gateway.evaluation run \
  --config config/gateway.toml \
  --dataset evaluations/datasets/support-ticket-category-v1.jsonl \
  --target openai/small-model-id --max-tokens 8 --output results/ \
  --live --max-cost-micro-usd 200000

# Store results as routing evidence.
GATEWAY_DATABASE_URL=postgresql+asyncpg://... \
  uv run python -m gateway.evaluation record results/*.json
```

Use a `--max-tokens` no larger than the output allowance of the aliases that
will rely on the run. Routing ignores a run made with a larger allowance than a
request's, because that run did not measure the truncation the request may
suffer.

`run` writes one results file per target (`format_version` 2; files from
earlier versions must be regenerated). Model output text is not stored. Each file
has:

- the run identity: run ID, provider, model, the provider's base URL (live runs),
  dataset, version, partition, SHA-256, task, method, evaluator version,
  simulated flag, streaming flag, `max_tokens`, pricing version, start and
  completion times, and `stop_reason` (set only when the run stopped before every
  case was sent);
- one record per case: passed, score, invalid response, latency, time to first
  token (streaming runs), finish reason, reported token usage, cost, the
  unresolved cost bound, error code, and for extraction the field counts behind
  the F1 score;
- a summary computed only from the case records.

| Summary field | Meaning |
| --- | --- |
| `case_count`, `passed_count`, `success_rate`, `success_rate_low/high` | Pass rate over every case, including failed calls, with its 95% Wilson interval. |
| `invalid_count` | Responses without the required form: not one allowed label, or not a JSON object. A refusal written as text lands here. |
| `error_count`, `timeout_count` | Calls that returned no response; timeouts are the `evaluation_timeout` subset. |
| `refusal_count`, `truncated_count` | Finish reason `content_filter` (provider-signalled refusal) or `length` (output allowance reached). The response is still scored. |
| `mean_score` | Mean of per-case scores (1/0 for labels, F1 for fields). |
| `field_precision`, `field_recall`, `field_f1` | Extraction only, micro-averaged over all (field, value) pairs. Failed and invalid responses predict nothing, so their fields count against recall. |
| `latency_p50/p95/p99_ms`, `first_token_p50/p95_ms` | Nearest-rank percentiles of successful calls, or `null` with fewer than 1/(1−q) samples (2, 20, 100), where the percentile would just be the slowest call. |
| `usage_reported_count`, `prompt_tokens`, `completion_tokens` | Provider-reported token counts. Missing usage is never filled in. |
| `estimated_cost_micro_usd` | Sum of known per-case costs: reported usage at the configured price, or 0 for calls the provider certainly did not run. |
| `cost_complete`, `unresolved_case_count`, `unresolved_max_micro_usd` | Cases whose cost is unknown (no usage, or a timeout or error after the request may have been sent) and the most they could cost at full output. |

Costs are estimates from the configured prices. Providers do not report charges
per call, so there is no observed charge to compare them with, and they are not
invoice amounts.

Safeguards:

- Real provider calls require `--live`. They also require `--max-cost-micro-usd`
  covering the worst case: every case at its estimated input and the full
  `--max-tokens`, at the configured prices. A target without a price cannot run
  live. Credentials are read only for the providers named in `--target`.
- Concurrency is at most 16 (default 4) and every call has a timeout (default
  60 s).
- `--simulate` uses `SimulatedProvider`. It returns the expected answer for a
  deterministic share of cases (by hash of model and case ID) and a well-formed
  wrong answer otherwise. Its token counts are estimates and its latency is an
  in-process function call. It tests the pipeline and nothing else, and routing
  never accepts its runs.
- `record` recomputes each file's summary from its case records. It refuses files
  whose summary does not match, and cases with negative measurements, scores
  outside [0, 1], or a pass alongside an error. The database also refuses runs
  completed in the future, counts that exceed the case count, and negative
  latencies, token counts, or costs.

The default test suite uses only simulated or fake providers and needs no vendor
credentials. Real evaluations are run manually by the operator.

## Storage

Migration `0003_evaluation_runs` adds two tables, separate from the cost ledger:

- `evaluation_runs`: one row per run with its identity and summary. Columns
  include `case_count`, `passed_count`, `invalid_count`, `error_count`,
  `mean_score`, latency p50/p95 and sample count, token totals, estimated cost,
  and `recorded_at`.
- `evaluation_case_results`: one row per case.

Migration `0004_evaluation_partitions` adds the partition, base URL, streaming
flag, `stop_reason`, and a `results_sha256` digest to runs, and the per-case
detail above to case rows. Runs recorded before it keep their meaning: they used
development data, ran every case, and did not stream. Their new per-case columns
are `NULL`. New runs must have a digest (a `NOT VALID` check that applies only to
new rows). Downgrading below 0004 is refused while holdout or incomplete runs
exist, because revision 0003 would treat them as evidence.

A run and its cases are inserted in one transaction. A trigger rejects `UPDATE`
and `DELETE` on both tables. Recording is idempotent: recording the same results
file again reports `already recorded` and changes nothing. A different file with
an already recorded run ID is refused. Concurrent `record` commands for one run
store one copy. To retire evidence, record a newer run or change
`dataset_version`, `max_evidence_age_days`, or the model's `routable` flag.

Holdout runs and incomplete runs are stored for audit and comparison, but the
gateway never loads them as evidence.

## Uncertainty and evidence rules

Each summary reports the pass rate and its 95% Wilson interval. Routing compares
`min_success_rate` with the lower bound, so small samples need a higher observed
rate to qualify:

| Passed / cases | Pass rate | 95% lower bound |
| --- | --- | --- |
| 10 / 10 | 1.000 | 0.722 |
| 43 / 44 | 0.977 | 0.882 |
| 44 / 44 | 1.000 | 0.920 |
| 950 / 1000 | 0.950 | 0.935 |

`min_samples` also sets a hard floor. Evidence older than
`max_evidence_age_days` stops counting. Only complete, non-simulated runs on a
development partition, scored by the current evaluator version, are evidence. A
`max_p95_latency_ms` rule needs at least 20 successful calls, because p95 is not
reported with fewer. Choose these per task. A small dataset
with a high threshold will often leave a cheaper model unqualified, which is
intended.

The interval treats cases as independent samples from the population the dataset
represents. A hand-written dataset of 40 cases represents only what its author
thought to include. It says nothing about inputs unlike those cases, and model
behaviour can change without the model ID changing. Re-run evaluations after a
provider announces changes, and periodically within `max_evidence_age_days`.

## Comparing policies

`tools/routing_experiment.py` replays a dataset through the router for `fixed`,
`lowest_cost`, and `quality_constrained`, using one results file per target. Each
policy's choice is paired with that model's recorded result for the same case. It
reports:

- total cost and the per-request distribution (p50, p95, max), priced from
  recorded token usage at the configuration's current prices;
- success rate with its interval, and whether it fell below `min_success_rate`;
- latency p50/p95/p99 from the recorded calls;
- routing distribution, refused requests by error code, invalid responses, and
  provider errors;
- savings against `fixed`, as (baseline − policy) / baseline × 100. Savings are
  shown only when both policies served every case with recorded usage. Otherwise
  the report states why they are not comparable.

When the same runs serve as evidence and as workload (the default), the report
says `in_sample`, and the quality it shows is optimistic. Pass separate
`--evidence` files from another run, or a different dataset version, for an
out-of-sample comparison. Simulated inputs need `--allow-simulated`, and the
report then begins with a SIMULATED warning. See
[benchmarks.md](benchmarks.md#phase-4-routing-measurements-2026-10-31) for a
simulated example and its limits.
