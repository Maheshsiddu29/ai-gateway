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

# Real providers (costs money). The first command prints a plan and sends nothing.
uv run --env-file .env python -m gateway.evaluation run \
  --config config/gateway.toml \
  --dataset evaluations/datasets/support-ticket-category-v1.jsonl \
  --target openai/small-model-id --max-tokens 8 --output results/ \
  --live --max-cost-micro-usd 200000 --max-requests 100
# After reviewing the plan, repeat the same command with its digest.
uv run --env-file .env python -m gateway.evaluation run ... --approve <plan sha256>

# Store results as routing evidence.
GATEWAY_DATABASE_URL=postgresql+asyncpg://... \
  uv run python -m gateway.evaluation record results/*.json

# Show which candidates of each quality-constrained alias qualify now.
GATEWAY_DATABASE_URL=postgresql+asyncpg://... \
  uv run python -m gateway.evaluation status --config config/gateway.toml
```

`status` loads evidence the way the gateway does and applies each alias's rules
at the current time. For every candidate it prints `qualifies` or the exclusion
(`no_evidence`, `stale_evidence`, `insufficient_samples`, `below_quality`, ...),
with the run ID, pass count, lower bound, and age.

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

### Live-run safeguards

A live evaluation spends money, and having provider credentials in the
environment is not permission to spend it. The CLI therefore refuses to send
anything until the operator approves an exact plan:

1. `--live` requires `--max-cost-micro-usd` and `--max-requests`.
2. Before any credential is read, the CLI checks each target. Each target needs
   a price effective now and a `[[routing.models]]` entry. Every case must fit
   the entry's context window, `--max-tokens` must not exceed its output limit,
   and `--stream` needs the `streaming` and `stream_usage` capabilities. The
   provider adapter must accept every case unchanged. A failure in any check
   rejects the whole plan.
3. The CLI prints the plan and its SHA-256, then exits with code 3 without
   sending anything. The plan lists the dataset (name, version, partition,
   hash), each target with its base URL and price version, the request count,
   generation settings, and the worst-case cost per target and in total.
   `--plan-output` also writes it as JSON. The plan is refused if it needs more
   requests than `--max-requests`, or if its worst case (every case at its
   estimated input plus the full `--max-tokens`) exceeds the ceiling.
4. Repeating the command with `--approve <sha256>` runs the plan. Any change to
   the dataset file, targets, base URLs, prices, limits, or settings changes the
   digest, so the old approval no longer matches and nothing is sent.
5. Credentials are read only after approval, and only for the approved targets'
   providers.

While a run executes:

- Each case is sent once. Errors and timeouts are recorded, never retried, so an
  uncertain call is never paid for twice.
- Before each dispatch, the case reserves its worst-case cost against the
  ceiling, shared by all targets of the command. After the call the reservation
  settles to the known cost, or stays at the worst case when usage is unknown.
  When a reservation does not fit, no further case is sent. The run ends with
  `stop_reason = "cost_ceiling"`, later targets are skipped, and the exit code is
  4. A provider can report more input tokens than estimated, so calls already in
  flight can carry spend past the ceiling. The ceiling limits what the CLI sends.
  It does not bound the provider's invoice.
- Every case is journalled to `<results>.partial.jsonl` when it is sent and when
  its result is known, with each line synced to disk. The journal is removed
  once the results file is written. After Ctrl-C (exit code 130) or a crash, the
  journal remains. Cases sent but without a result were possibly charged. The
  CLI never reads journals, so an interrupted run is not resumed; running the
  command again starts a new run from the first case.
- Concurrency is at most 16 (default 4), and every call has a timeout (default
  60 s, at most 600 s).

Other safeguards:

- `--simulate` uses `SimulatedProvider`. It returns the expected answer for a
  deterministic share of cases (by hash of model and case ID) and a well-formed
  wrong answer otherwise. Its token counts are estimates and its latency is an
  in-process function call. It tests the pipeline and nothing else, and routing
  never accepts its runs.
- `record` recomputes each file's summary from its case records. It refuses files
  whose summary does not match, cases with negative measurements, scores outside
  [0, 1], a pass alongside an error, and field counts or first-token times that
  do not fit the run. The database also refuses runs completed in the future,
  counts that exceed the case count, and negative latencies, token counts, or
  costs.

The test suite and CI use only simulated, stubbed, or local mock providers and
need no vendor credentials.
`tests/integration/test_evaluation_live_mock.py` runs the full live path (plan,
approval, journal, results) in a subprocess against the mock provider on
localhost. Real evaluations are run manually by the operator.

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

`tools/routing_experiment.py` measures whether a quality-constrained alias would
have cost less than its fixed primary model while keeping quality, on cases that
played no part in qualifying its models:

```sh
uv run python tools/routing_experiment.py \
  --config config/gateway.toml --alias classify \
  --evidence-dataset evaluations/datasets/support-ticket-category-v1.jsonl \
  --evidence results/dev/*.json \
  --dataset evaluations/datasets/support-ticket-category-v1-holdout.jsonl \
  --results results/holdout/*.json --output report.json
```

It replays the holdout dataset through the router for `fixed` (the primary
model), `lowest_cost` (the same candidates without quality rules), and the
alias's `quality_constrained` policy. Routing evidence comes only from the
development runs, evaluated as of the newest evidence run so the replay is
reproducible. Each policy's choice for a case is paired with that model's
recorded holdout result for the same case. No provider is called and no request
goes through the gateway.

The tool refuses comparisons that would be unfair:

- holdout runs offered as evidence, or a holdout that fails `check` against the
  evidence dataset;
- workload runs from a different dataset file, incomplete runs, or a target
  without workload runs;
- workload runs sent with a `max_tokens` other than the alias's output allowance,
  or with mixed streaming settings;
- simulated runs, unless `--allow-simulated` is given.

For each policy the report gives:

- requests, refusals by error code, and requests the quality-constrained policy
  sent to its primary without qualified evidence (`unverified_fallbacks`);
- success rate over all requests, with refused requests and failed calls counted
  as not passed, and its 95% Wilson interval;
- a threshold verdict against `min_success_rate`: `passes` when the lower bound
  reaches it, `fails` when the observed rate is below it, `inconclusive`
  otherwise;
- the quality difference from `fixed` on the same cases, with a 95% paired
  interval (the adjusted Wald interval of Bonett and Price, 2012) and the counts
  of cases only one of the two passed;
- invalid responses, provider errors, timeouts, and error rate;
- observed cost (recorded token usage at the configuration's current prices, the
  same price snapshot for every policy) and, separately, the number of cases
  with unknown cost and their upper bound;
- savings against `fixed`: the absolute difference in observed cost, and a
  percentage when both policies served every request with known costs. With
  unknown costs the report gives the range of possible percentages instead.
  With refusals it gives no percentage, because the policies served different
  work. Negative savings are reported as negative;
- latency p50/p95/p99 of the recorded calls (`null` without enough samples) and
  the routing distribution.

The report also records both datasets' hashes, the evidence and workload run
IDs, the quality rules, price versions, the router's measured decision time, and
a list of limitations that apply to the inputs. That list always states that the
comparison is a replay, that latency is client-side evaluation latency, and that
costs are not invoices. It adds a warning when inputs are simulated, in-sample,
from a single run per model, or include unknown costs.

Several results files for the same target are repetitions, for example reruns
of a nondeterministic model. Every target needs the same number. Each repetition
is compared separately, the report lists per-repetition results, and
`variability` shows how often each model's case outcomes agreed across runs.
Repetitions reuse the same cases, so they are not pooled into one interval.

Without `--evidence`, the workload doubles as evidence. That is allowed only
for a development dataset and is reported as `in_sample`, because quality is
then optimistic. See
[benchmarks.md](benchmarks.md#phase-5-measurements-2026-11-01) for a simulated
example and its limits.
