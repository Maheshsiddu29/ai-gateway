"""Command-line entry point: ``python -m gateway.evaluation``.

``run`` evaluates one dataset against one or more configured provider/model
targets and writes one results file per target. ``--simulate`` uses
SimulatedProvider and makes no network calls. Provider calls cost money, so
``--live`` needs a cost ceiling and a request limit, prints a preflight plan, and
sends nothing unless ``--approve`` repeats that plan's SHA-256 (see
``gateway.evaluation.plan``). Each run journals its progress beside its results
file; an interrupted run keeps its journal and is never resumed automatically.

``record`` re-checks results files and inserts them into PostgreSQL
(``GATEWAY_DATABASE_URL``), where the gateway reads routing evidence.

``check`` verifies that a holdout partition shares its development partition's
task definition and none of its cases.

Exit codes: 0 success, 2 invalid request or failed check, 3 live plan printed
and awaiting approval, 4 stopped at the cost ceiling, 130 interrupted.
"""

import argparse
import asyncio
import json
import os
import re
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine

from gateway.core.config import GatewayConfig, read_gateway_config, resolve_api_keys
from gateway.cost.pricing import PriceCatalog
from gateway.errors import ConfigurationError
from gateway.evaluation.dataset import Dataset, DatasetError, check_partitions, load_dataset
from gateway.evaluation.journal import RunJournal
from gateway.evaluation.plan import LivePlan, PlanError, build_live_plan
from gateway.evaluation.runner import (
    MAX_CONCURRENCY,
    CostCeiling,
    CostCeilingError,
    EvaluationRun,
    run_evaluation,
)
from gateway.evaluation.simulated import SimulatedProvider
from gateway.evaluation.store import EvaluationRecordError, record_run
from gateway.providers.base import Provider
from gateway.providers.factory import build_provider
from gateway.schemas.completions import MAX_OUTPUT_TOKENS

APPROVAL_REQUIRED = 3
STOPPED_AT_CEILING = 4
INTERRUPTED = 130


class UsageError(Exception):
    pass


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "run":
            return asyncio.run(_run(args))
        if args.command == "record":
            asyncio.run(_record([Path(path) for path in args.results]))
        else:
            _check(args.development, args.holdout)
    except (
        UsageError,
        ConfigurationError,
        DatasetError,
        EvaluationRecordError,
        PlanError,
        CostCeilingError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(
            "interrupted: results so far are in the .partial.jsonl journal in the output"
            " directory; the run will not be resumed",
            file=sys.stderr,
        )
        return INTERRUPTED
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m gateway.evaluation")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="evaluate a dataset against provider/model targets")
    run.add_argument("--config", type=Path, required=True, help="gateway TOML configuration")
    run.add_argument("--dataset", type=Path, required=True, help="JSONL dataset")
    run.add_argument(
        "--target", action="append", required=True, help="provider/model; may be repeated"
    )
    run.add_argument("--max-tokens", type=int, required=True, help="max_tokens for each case")
    run.add_argument("--output", type=Path, required=True, help="directory for results files")
    mode = run.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--simulate",
        action="append",
        metavar="PROVIDER/MODEL=ACCURACY",
        help="answer with SimulatedProvider at this accuracy; makes no provider calls",
    )
    mode.add_argument("--live", action="store_true", help="call the real providers (costs money)")
    live = run.add_argument_group("live runs")
    live.add_argument(
        "--max-cost-micro-usd",
        type=int,
        help="refuse a plan whose worst-case cost is higher, and stop sending cases at it",
    )
    live.add_argument(
        "--max-requests", type=int, help="refuse a plan that needs more provider requests"
    )
    live.add_argument("--approve", metavar="PLAN_SHA256", help="the digest of the approved plan")
    live.add_argument("--plan-output", type=Path, help="also write the plan as JSON")
    run.add_argument(
        "--stream", action="store_true", help="stream each response and record time to first token"
    )
    run.add_argument("--concurrency", type=int, default=4)
    run.add_argument("--timeout-seconds", type=float, default=60.0)
    record = commands.add_parser("record", help="store results files in PostgreSQL")
    record.add_argument("results", nargs="+", help="results files written by run")
    check = commands.add_parser(
        "check", help="verify that a holdout partition is independent of its development data"
    )
    check.add_argument("--development", type=Path, required=True, help="development JSONL")
    check.add_argument("--holdout", type=Path, required=True, help="holdout JSONL")
    return parser


async def _run(args: argparse.Namespace) -> int:
    if not 1 <= args.max_tokens <= MAX_OUTPUT_TOKENS:
        raise UsageError(f"--max-tokens must be between 1 and {MAX_OUTPUT_TOKENS}")
    if not 1 <= args.concurrency <= MAX_CONCURRENCY:
        raise UsageError(f"--concurrency must be between 1 and {MAX_CONCURRENCY}")
    if not 0 < args.timeout_seconds <= 600:
        raise UsageError("--timeout-seconds must be above 0 and at most 600")
    config = read_gateway_config(args.config)
    dataset = load_dataset(args.dataset)
    targets = [_parse_target(value, config) for value in args.target]
    if len(set(targets)) != len(targets):
        raise UsageError("--target values must not repeat")
    now = datetime.now(UTC)
    catalog = PriceCatalog(config.cost.pricing)
    prices = {target: catalog.find(*target, now) for target in targets}

    plan: LivePlan | None = None
    ceiling: CostCeiling | None = None
    if args.live:
        plan = _approved_plan(args, config, dataset, targets, now)
        if plan is None:
            return APPROVAL_REQUIRED
        ceiling = CostCeiling(plan.max_cost_micro_usd)
        # Credentials are read only now, and only for the approved targets.
        keys = resolve_api_keys(config, os.environ, {provider for provider, _ in targets})
        providers: dict[str, Provider] = {
            name: build_provider(name, config.providers[name], keys[name]) for name in keys
        }
    else:
        if any(
            value is not None
            for value in (
                args.max_cost_micro_usd,
                args.max_requests,
                args.approve,
                args.plan_output,
            )
        ):
            raise UsageError(
                "--max-cost-micro-usd, --max-requests, --approve, and --plan-output"
                " apply only to --live"
            )
        accuracy = _parse_simulation(args.simulate, targets)
        providers = {
            name: SimulatedProvider(
                name, dataset, {model: value for (p, model), value in accuracy.items() if p == name}
            )
            for name in {provider for provider, _ in targets}
        }

    args.output.mkdir(parents=True, exist_ok=True)
    try:
        for provider_name, model in targets:
            if ceiling is not None and ceiling.reached:
                print(f"skipped {provider_name}/{model}: the cost ceiling was reached")
                continue
            run_id = uuid.uuid4()
            stem = _results_stem(dataset, provider_name, model, run_id)
            journal = RunJournal(
                args.output / f"{stem}.partial.jsonl",
                {
                    "run_id": str(run_id),
                    "provider": provider_name,
                    "model": model,
                    "dataset_sha256": dataset.sha256,
                    "plan_sha256": plan.sha256() if plan is not None else None,
                    "started_at": datetime.now(UTC).isoformat(),
                },
            )
            print(f"running {provider_name}/{model}; journal {journal.path}")
            try:
                run = await run_evaluation(
                    providers[provider_name],
                    model,
                    dataset,
                    max_output_tokens=args.max_tokens,
                    price=prices[(provider_name, model)],
                    simulated=not args.live,
                    stream=args.stream,
                    provider_base_url=(
                        str(config.providers[provider_name].base_url) if args.live else None
                    ),
                    run_id=run_id,
                    observer=journal,
                    ceiling=ceiling,
                    concurrency=args.concurrency,
                    call_timeout_seconds=args.timeout_seconds,
                )
            except BaseException:
                journal.close()
                raise
            path = args.output / f"{stem}.json"
            _write_atomically(path, json.dumps(run.to_json(), indent=2) + "\n")
            journal.discard()
            _print_summary(run, path)
    finally:
        for provider in providers.values():
            await provider.aclose()
    if ceiling is not None and ceiling.reached:
        print(
            f"stopped: the cost ceiling of {ceiling.limit_micro_usd} micro-USD was reached;"
            " results are incomplete and will not be routing evidence",
            file=sys.stderr,
        )
        return STOPPED_AT_CEILING
    return 0


def _approved_plan(
    args: argparse.Namespace,
    config: GatewayConfig,
    dataset: Dataset,
    targets: list[tuple[str, str]],
    now: datetime,
) -> LivePlan | None:
    """Return the plan if ``--approve`` matches it; otherwise print it and return None."""
    if args.max_cost_micro_usd is None or args.max_cost_micro_usd < 0:
        raise UsageError("--live requires --max-cost-micro-usd")
    if args.max_requests is None or args.max_requests < 1:
        raise UsageError("--live requires --max-requests")
    plan = build_live_plan(
        config,
        dataset,
        targets,
        max_tokens=args.max_tokens,
        stream=args.stream,
        timeout_seconds=args.timeout_seconds,
        concurrency=args.concurrency,
        max_requests=args.max_requests,
        max_cost_micro_usd=args.max_cost_micro_usd,
        now=now,
    )
    digest = plan.sha256()
    print(plan.describe())
    print(f"plan sha256 {digest}")
    if args.plan_output is not None:
        args.plan_output.write_text(
            json.dumps({**plan.to_json(), "sha256": digest}, indent=2) + "\n"
        )
    if args.approve is None:
        print(
            "Nothing was sent. To run this plan, repeat the command with --approve " + digest,
            file=sys.stderr,
        )
        return None
    if args.approve != digest:
        raise UsageError(
            "--approve does not match this plan; review the plan above and approve its digest"
        )
    return plan


def _write_atomically(path: Path, content: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content)
    temporary.replace(path)


async def _record(paths: list[Path]) -> None:
    url = os.environ.get("GATEWAY_DATABASE_URL")
    if not url:
        raise UsageError("set GATEWAY_DATABASE_URL to record results")
    runs: list[EvaluationRun] = []
    for path in paths:
        try:
            runs.append(EvaluationRun.from_json(json.loads(path.read_text())))
        except (ValueError, KeyError, TypeError) as exc:
            raise UsageError(f"{path}: not a valid results file: {exc}") from None
    engine = create_async_engine(url)
    try:
        for run in runs:
            action = "recorded" if await record_run(engine, run) else "already recorded"
            identity = run.identity
            print(f"{action} {identity.run_id} ({identity.provider}/{identity.model})")
    finally:
        await engine.dispose()


def _check(development_path: Path, holdout_path: Path) -> None:
    development, holdout = load_dataset(development_path), load_dataset(holdout_path)
    check_partitions(development, holdout)
    print(
        f"{development.name} v{development.version}: {len(development.cases)} development and"
        f" {len(holdout.cases)} holdout cases, no shared or near-duplicate cases"
    )


def _parse_target(value: str, config: GatewayConfig) -> tuple[str, str]:
    provider, separator, model = value.partition("/")
    if not separator or not provider or not model:
        raise UsageError(f"target {value!r} must be provider/model")
    if provider not in config.providers:
        raise UsageError(f"target {value!r} names an unconfigured provider")
    return provider, model


def _parse_simulation(
    values: list[str], targets: list[tuple[str, str]]
) -> dict[tuple[str, str], float]:
    accuracy: dict[tuple[str, str], float] = {}
    for value in values:
        target, separator, number = value.rpartition("=")
        provider, _, model = target.partition("/")
        try:
            accuracy[(provider, model)] = float(number)
        except ValueError:
            raise UsageError(f"--simulate {value!r} must be provider/model=accuracy") from None
        if not separator or not 0 <= accuracy[(provider, model)] <= 1:
            raise UsageError(f"--simulate {value!r} must be provider/model=accuracy in [0, 1]")
    missing = [f"{p}/{m}" for p, m in targets if (p, m) not in accuracy]
    if missing:
        raise UsageError(f"no --simulate accuracy for {', '.join(missing)}")
    return accuracy


def _results_stem(dataset: Dataset, provider: str, model: str, run_id: uuid.UUID) -> str:
    safe_model = re.sub(r"[^A-Za-z0-9._-]", "_", model)
    return (
        f"{dataset.name}-v{dataset.version}-{dataset.partition.value}-{provider}-{safe_model}"
        f"-{str(run_id)[:8]}"
    )


def _print_summary(run: EvaluationRun, path: Path) -> None:
    identity, summary = run.identity, run.summary
    label = " (SIMULATED: not a measurement of any model)" if identity.simulated else ""
    p95 = "n/a" if summary.latency_p95_ms is None else f"{summary.latency_p95_ms:.1f} ms"
    print(
        f"{identity.provider}/{identity.model}{label}: passed {summary.passed_count}"
        f"/{summary.case_count} ({summary.success_rate:.3f}, 95% CI"
        f" {summary.success_rate_low:.3f}-{summary.success_rate_high:.3f}),"
        f" invalid {summary.invalid_count}, errors {summary.error_count}, p95 {p95},"
        f" estimated cost {summary.estimated_cost_micro_usd} micro-USD{_unresolved(run)}"
        f"{'' if run.complete else f', STOPPED ({identity.stop_reason})'} -> {path}"
    )


def _unresolved(run: EvaluationRun) -> str:
    summary = run.summary
    if summary.cost_complete:
        return ""
    bound = summary.unresolved_max_micro_usd
    limit = "unknown" if bound is None else f"at most {bound} micro-USD"
    return f" plus {summary.unresolved_case_count} cases of unknown cost ({limit})"
