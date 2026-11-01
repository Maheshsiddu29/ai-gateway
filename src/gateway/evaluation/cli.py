"""Command-line entry point: ``python -m gateway.evaluation``.

``run`` evaluates one dataset against one or more configured provider/model
targets and writes one results file per target. Provider calls cost money, so
``run`` refuses to call a real provider unless ``--live`` is given together with a
``--max-cost-micro-usd`` ceiling that covers the worst case for the whole run.
``--simulate`` uses SimulatedProvider instead and makes no network calls.

``record`` re-checks results files and inserts them into PostgreSQL
(``GATEWAY_DATABASE_URL``), where the gateway reads routing evidence.

``check`` verifies that a holdout partition shares its development partition's
task definition and none of its cases.
"""

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine

from gateway.admission.tokens import estimate_input_tokens
from gateway.core.config import GatewayConfig, read_gateway_config, resolve_api_keys
from gateway.cost.pricing import Price, PriceCatalog
from gateway.errors import ConfigurationError
from gateway.evaluation.dataset import Dataset, DatasetError, check_partitions, load_dataset
from gateway.evaluation.runner import MAX_CONCURRENCY, EvaluationRun, run_evaluation
from gateway.evaluation.simulated import SimulatedProvider
from gateway.evaluation.store import EvaluationRecordError, record_run
from gateway.providers.base import Provider
from gateway.providers.factory import build_provider
from gateway.schemas.completions import MAX_OUTPUT_TOKENS


class UsageError(Exception):
    pass


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "run":
            asyncio.run(_run(args))
        elif args.command == "record":
            asyncio.run(_record([Path(path) for path in args.results]))
        else:
            _check(args.development, args.holdout)
    except (UsageError, ConfigurationError, DatasetError, EvaluationRecordError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
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
    run.add_argument(
        "--max-cost-micro-usd",
        type=int,
        help="with --live: refuse to start if the worst-case estimated cost is higher",
    )
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


async def _run(args: argparse.Namespace) -> None:
    if not 1 <= args.max_tokens <= MAX_OUTPUT_TOKENS:
        raise UsageError(f"--max-tokens must be between 1 and {MAX_OUTPUT_TOKENS}")
    if not 1 <= args.concurrency <= MAX_CONCURRENCY:
        raise UsageError(f"--concurrency must be between 1 and {MAX_CONCURRENCY}")
    config = read_gateway_config(args.config)
    dataset = load_dataset(args.dataset)
    targets = [_parse_target(value, config) for value in args.target]
    if len(set(targets)) != len(targets):
        raise UsageError("--target values must not repeat")
    catalog = PriceCatalog(config.cost.pricing)
    now = datetime.now(UTC)
    prices = {target: catalog.find(*target, now) for target in targets}

    if args.live:
        _check_cost_ceiling(args.max_cost_micro_usd, dataset, args.max_tokens, prices)
        keys = resolve_api_keys(config, os.environ, {provider for provider, _ in targets})
        providers: dict[str, Provider] = {
            name: build_provider(name, config.providers[name], keys[name]) for name in keys
        }
    else:
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
                concurrency=args.concurrency,
                call_timeout_seconds=args.timeout_seconds,
            )
            path = args.output / _results_name(run)
            path.write_text(json.dumps(run.to_json(), indent=2) + "\n")
            _print_summary(run, path)
    finally:
        for provider in providers.values():
            await provider.aclose()


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


def _check_cost_ceiling(
    ceiling: int | None,
    dataset: Dataset,
    max_tokens: int,
    prices: dict[tuple[str, str], Price | None],
) -> None:
    if ceiling is None:
        raise UsageError("--live requires --max-cost-micro-usd")
    unpriced = [f"{p}/{m}" for (p, m), price in prices.items() if price is None]
    if unpriced:
        # Without a price the worst case cannot be bounded.
        raise UsageError(f"no effective price for {', '.join(unpriced)} in [[cost.pricing]]")
    worst_case = sum(
        price.charge_micro_usd(estimate_input_tokens(case.messages), max_tokens)
        for price in prices.values()
        if price is not None
        for case in dataset.cases
    )
    if worst_case > ceiling:
        raise UsageError(
            f"worst-case estimated cost {worst_case} micro-USD exceeds --max-cost-micro-usd"
        )


def _results_name(run: EvaluationRun) -> str:
    identity = run.identity
    model = re.sub(r"[^A-Za-z0-9._-]", "_", identity.model)
    return (
        f"{identity.dataset}-v{identity.dataset_version}-{identity.provider}-{model}"
        f"-{str(identity.run_id)[:8]}.json"
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
        f" cost {summary.estimated_cost_micro_usd} micro-USD"
        f"{'' if summary.cost_complete else ' (incomplete)'} -> {path}"
    )
