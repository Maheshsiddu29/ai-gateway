import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from gateway.cost.pricing import Price
from gateway.errors import ProviderUnavailableError
from gateway.evaluation.cli import main
from gateway.evaluation.dataset import Dataset, load_dataset
from gateway.evaluation.runner import (
    CaseResult,
    EvaluationRun,
    case_max_cost,
    mean_completion_tokens,
    run_evaluation,
)
from gateway.evaluation.scoring import EVALUATOR_VERSIONS
from gateway.evaluation.simulated import SimulatedProvider
from gateway.providers.base import Completion, ProviderRequest, Usage
from tests.fakes import FakeProvider

DATASETS = Path(__file__).parents[2] / "evaluations" / "datasets"
TICKETS = DATASETS / "support-ticket-category-v1.jsonl"
INVOICES = DATASETS / "invoice-fields-v1.jsonl"
PRICE = Price(
    provider="sim",
    model="m",
    version="p1",
    effective_at=datetime(2020, 1, 1, tzinfo=UTC),
    input_per_million_usd=Decimal("1"),
    output_per_million_usd=Decimal("2"),
)


def tickets() -> Dataset:
    return load_dataset(TICKETS)


@pytest.mark.parametrize("path", [TICKETS, INVOICES], ids=["classification", "extraction"])
async def test_simulated_evaluation_is_deterministic(path: Path) -> None:
    dataset = load_dataset(path)
    provider = SimulatedProvider("sim", dataset, {"m": 0.75})

    first = await run_evaluation(
        provider, "m", dataset, max_output_tokens=64, price=PRICE, simulated=True
    )
    second = await run_evaluation(
        provider, "m", dataset, max_output_tokens=64, price=PRICE, simulated=True
    )

    def outcomes(run: EvaluationRun) -> list[tuple[str, bool, float]]:
        return [(r.case_id, r.passed, r.score) for r in run.results]

    assert outcomes(first) == outcomes(second)
    summary = first.summary
    assert summary.case_count == len(dataset.cases)
    assert 0 < summary.passed_count < summary.case_count
    assert summary.invalid_count == summary.error_count == 0
    assert summary.success_rate_low < summary.success_rate < summary.success_rate_high
    assert first.identity.simulated
    assert first.identity.evaluator_version == EVALUATOR_VERSIONS[dataset.method]
    assert first.identity.dataset_sha256 == dataset.sha256


async def test_perfect_and_zero_accuracy_bounds() -> None:
    dataset = tickets()
    provider = SimulatedProvider("sim", dataset, {"good": 1.0, "bad": 0.0})

    good = await run_evaluation(
        provider, "good", dataset, max_output_tokens=8, price=None, simulated=True
    )
    bad = await run_evaluation(
        provider, "bad", dataset, max_output_tokens=8, price=None, simulated=True
    )

    assert good.summary.passed_count == len(dataset.cases)
    assert bad.summary.passed_count == 0
    # A perfect score on 44 cases is still uncertain.
    assert good.summary.success_rate_low < 0.93


async def test_costs_use_reported_usage_and_the_given_price() -> None:
    dataset = tickets()
    run = await run_evaluation(
        SimulatedProvider("sim", dataset, {"m": 1.0}),
        "m",
        dataset,
        max_output_tokens=8,
        price=PRICE,
        simulated=True,
    )

    for result in run.results:
        assert result.prompt_tokens is not None
        assert result.completion_tokens is not None
        assert result.cost_micro_usd == PRICE.charge_micro_usd(
            result.prompt_tokens, result.completion_tokens
        )
    assert run.summary.cost_complete
    assert run.summary.estimated_cost_micro_usd == sum(
        result.cost_micro_usd or 0 for result in run.results
    )
    assert run.identity.pricing_version == "p1"
    assert mean_completion_tokens(run.summary) is not None


async def test_errors_invalid_outputs_and_missing_usage_are_recorded() -> None:
    dataset = tickets()
    failing = FakeProvider("f", error=ProviderUnavailableError("down", code="provider_unavailable"))
    rambling = FakeProvider(
        "r", completion=Completion(content="I think billing", finish_reason="stop", usage=None)
    )

    failed = await run_evaluation(
        failing, "m", dataset, max_output_tokens=8, price=PRICE, simulated=False
    )
    invalid = await run_evaluation(
        rambling, "m", dataset, max_output_tokens=8, price=PRICE, simulated=False
    )

    assert failed.summary.error_count == failed.summary.case_count
    assert failed.summary.latency_samples == 0
    assert failed.summary.latency_p95_ms is None
    assert {r.error_code for r in failed.results} == {"provider_unavailable"}
    assert invalid.summary.invalid_count == invalid.summary.case_count
    assert invalid.summary.usage_reported_count == 0
    assert not invalid.summary.cost_complete
    assert mean_completion_tokens(invalid.summary) is None


async def test_unbilled_usage_dimensions_leave_cost_unknown() -> None:
    dataset = tickets()
    provider = FakeProvider(
        "f",
        completion=Completion(
            content="billing",
            finish_reason="stop",
            usage=Usage(10, 1, 11, billing_dimensions_supported=False),
        ),
    )

    run = await run_evaluation(
        provider, "m", dataset, max_output_tokens=8, price=PRICE, simulated=False
    )

    assert {r.cost_micro_usd for r in run.results} == {None}
    assert run.summary.usage_reported_count == run.summary.case_count


async def test_slow_calls_time_out_and_concurrency_is_bounded() -> None:
    dataset = tickets()
    active = 0
    peak = 0

    class Slow(FakeProvider):
        async def complete(self, request: ProviderRequest) -> Completion:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(1)
            finally:
                active -= 1
            raise AssertionError("unreachable")

    run = await run_evaluation(
        Slow("slow"),
        "m",
        dataset,
        max_output_tokens=8,
        price=None,
        simulated=False,
        concurrency=3,
        call_timeout_seconds=0.01,
    )

    assert {r.error_code for r in run.results} == {"evaluation_timeout"}
    assert peak == 3


async def test_concurrency_is_validated() -> None:
    with pytest.raises(ValueError, match="concurrency"):
        await run_evaluation(
            FakeProvider(),
            "m",
            tickets(),
            max_output_tokens=8,
            price=None,
            simulated=False,
            concurrency=17,
        )


async def test_results_file_round_trip_and_tamper_detection() -> None:
    dataset = tickets()
    run = await run_evaluation(
        SimulatedProvider("sim", dataset, {"m": 0.5}),
        "m",
        dataset,
        max_output_tokens=8,
        price=PRICE,
        simulated=True,
    )
    data = json.loads(json.dumps(run.to_json()))

    assert EvaluationRun.from_json(data) == run

    data["results"][0]["passed"] = not data["results"][0]["passed"]
    with pytest.raises(ValueError, match="does not match"):
        EvaluationRun.from_json(data)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("latency_ms", -1.0, "negative measurement"),
        ("completion_tokens", -5, "negative measurement"),
        ("score", 1.5, "outside"),
        ("error_code", "provider_unavailable", "passed despite"),
    ],
)
async def test_results_file_rejects_impossible_case_values(
    field: str, value: object, message: str
) -> None:
    dataset = tickets()
    run = await run_evaluation(
        SimulatedProvider("sim", dataset, {"m": 1.0}),
        "m",
        dataset,
        max_output_tokens=8,
        price=PRICE,
        simulated=True,
    )
    data = json.loads(json.dumps(run.to_json()))
    data["results"][0][field] = value

    with pytest.raises(ValueError, match=message):
        EvaluationRun.from_json(data)


def write_config(tmp_path: Path) -> Path:
    path = tmp_path / "gateway.toml"
    path.write_text(
        """
[providers.sim]
type = "openai_compatible"
base_url = "https://sim.test/v1"
api_key_env = "SIM_KEY"

[models.fast]
provider = "sim"
model = "m"
max_output_tokens = 8

[cost]
enabled = true

[[cost.pricing]]
provider = "sim"
model = "m"
version = "p1"
effective_at = 2020-01-01T00:00:00Z
input_per_million_usd = "1"
output_per_million_usd = "2"
"""
    )
    return path


def cli_run(tmp_path: Path, *extra: str, target: str = "sim/m") -> int:
    return main(
        [
            "run",
            *("--config", str(write_config(tmp_path))),
            *("--dataset", str(TICKETS)),
            *("--target", target),
            *("--max-tokens", "8"),
            *("--output", str(tmp_path / "out")),
            *extra,
        ]
    )


def test_cli_simulated_run_writes_labelled_results(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = cli_run(tmp_path, "--simulate", "sim/m=0.9")

    assert code == 0
    assert "SIMULATED" in capsys.readouterr().out
    [path] = (tmp_path / "out").iterdir()
    run = EvaluationRun.from_json(json.loads(path.read_text()))
    assert run.identity.simulated
    assert run.identity.pricing_version == "p1"


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--live"], "requires --max-cost-micro-usd"),
        (["--live", "--max-cost-micro-usd", "1"], "exceeds --max-cost-micro-usd"),
        (["--simulate", "sim/other=0.5"], "no --simulate accuracy for sim/m"),
        (["--simulate", "sim/m=2"], "in \\[0, 1\\]"),
    ],
)
def test_cli_refuses_unbounded_or_unconfigured_runs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: list[str], message: str
) -> None:
    code = cli_run(tmp_path, *extra)

    assert code == 2
    assert message.replace("\\", "") in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_cli_live_run_requires_provider_credentials(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SIM_KEY", raising=False)

    code = cli_run(tmp_path, "--live", "--max-cost-micro-usd", "1000000")

    assert code == 2
    assert "requires environment variable SIM_KEY" in capsys.readouterr().err


def test_cli_rejects_unknown_targets(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli_run(tmp_path, "--simulate", "other/m=1", target="other/m")

    assert code == 2
    assert "unconfigured provider" in capsys.readouterr().err


async def test_streaming_runs_record_time_to_first_token() -> None:
    dataset = tickets()
    provider = SimulatedProvider("sim", dataset, {"m": 1.0})

    streamed = await run_evaluation(
        provider, "m", dataset, max_output_tokens=8, price=PRICE, simulated=True, stream=True
    )
    whole = await run_evaluation(
        provider, "m", dataset, max_output_tokens=8, price=PRICE, simulated=True
    )

    assert streamed.identity.stream
    assert all(r.first_token_ms is not None for r in streamed.results)
    assert streamed.summary.first_token_samples == streamed.summary.case_count
    assert streamed.summary.first_token_p95_ms is not None
    assert streamed.summary.cost_complete
    assert whole.summary.first_token_samples == 0
    assert {r.first_token_ms for r in whole.results} == {None}


@pytest.mark.parametrize(
    ("finish_reason", "field"), [("content_filter", "refusal_count"), ("length", "truncated_count")]
)
async def test_refusals_and_truncation_are_counted(finish_reason: str, field: str) -> None:
    provider = FakeProvider(
        "f", completion=Completion(content="", finish_reason=finish_reason, usage=Usage(5, 8, 13))
    )

    run = await run_evaluation(
        provider, "m", tickets(), max_output_tokens=8, price=PRICE, simulated=False
    )

    assert getattr(run.summary, field) == run.summary.case_count
    assert run.summary.invalid_count == run.summary.case_count
    assert run.summary.passed_count == 0


async def test_certain_and_uncertain_failures_are_costed_differently() -> None:
    dataset = tickets()
    refused = FakeProvider(
        "f",
        error=ProviderUnavailableError("refused", code="connect_error", may_have_processed=False),
    )
    uncertain = FakeProvider("f", error=ProviderUnavailableError("5xx", code="upstream_error"))

    not_run = await run_evaluation(
        refused, "m", dataset, max_output_tokens=8, price=PRICE, simulated=False
    )
    maybe_run = await run_evaluation(
        uncertain, "m", dataset, max_output_tokens=8, price=PRICE, simulated=False
    )

    assert not_run.summary.cost_complete
    assert not_run.summary.estimated_cost_micro_usd == 0
    assert not_run.summary.unresolved_case_count == 0
    assert not maybe_run.summary.cost_complete
    assert maybe_run.summary.unresolved_case_count == len(dataset.cases)
    assert maybe_run.summary.unresolved_max_micro_usd == sum(
        case_max_cost(PRICE, case, 8) for case in dataset.cases
    )


async def test_unknown_costs_without_a_price_have_no_bound() -> None:
    run = await run_evaluation(
        FakeProvider(), "m", tickets(), max_output_tokens=8, price=None, simulated=False
    )

    assert run.summary.unresolved_case_count == run.summary.case_count
    assert run.summary.unresolved_max_micro_usd is None


async def test_extraction_reports_micro_averaged_field_scores() -> None:
    dataset = load_dataset(INVOICES)
    run = await run_evaluation(
        SimulatedProvider("sim", dataset, {"m": 0.5}),
        "m",
        dataset,
        max_output_tokens=128,
        price=None,
        simulated=True,
    )

    summary = run.summary
    expected = sum(result.fields_expected or 0 for result in run.results)
    wrong = summary.case_count - summary.passed_count
    # Each wrong simulated answer omits exactly one field and invents none.
    assert expected > 0
    assert summary.field_precision == 1.0
    assert summary.field_recall == pytest.approx((expected - wrong) / expected)
    assert summary.field_f1 is not None
    labels = await run_evaluation(
        SimulatedProvider("sim", tickets(), {"m": 0.5}),
        "m",
        tickets(),
        max_output_tokens=8,
        price=None,
        simulated=True,
    )
    assert (labels.summary.field_precision, labels.summary.field_f1) == (None, None)


async def test_latency_percentiles_need_enough_samples() -> None:
    dataset = tickets()
    run = await run_evaluation(
        SimulatedProvider("sim", dataset, {"m": 1.0}),
        "m",
        dataset,
        max_output_tokens=8,
        price=None,
        simulated=True,
    )

    assert run.summary.latency_samples == 44
    assert run.summary.latency_p95_ms is not None
    # 44 samples cannot distinguish p99 from the maximum.
    assert run.summary.latency_p99_ms is None


async def test_cancellation_reports_in_flight_cases_and_propagates() -> None:
    dataset = tickets()
    started = asyncio.Event()

    class Hanging(FakeProvider):
        async def complete(self, request: ProviderRequest) -> Completion:
            started.set()
            await asyncio.sleep(3600)
            raise AssertionError("unreachable")

    dispatched: list[str] = []
    finished: list[CaseResult] = []

    class Observer:
        def dispatched(self, case_id: str) -> None:
            dispatched.append(case_id)

        def finished(self, result: CaseResult) -> None:
            finished.append(result)

    task = asyncio.create_task(
        run_evaluation(
            Hanging("h"),
            "m",
            dataset,
            max_output_tokens=8,
            price=PRICE,
            simulated=False,
            concurrency=3,
            observer=Observer(),
        )
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Only the three calls in flight were sent; queued cases were never dispatched.
    assert len(dispatched) == 3
    assert sorted(r.case_id for r in finished) == sorted(dispatched)
    assert {r.error_code for r in finished} == {"evaluation_interrupted"}
    assert all(r.cost_micro_usd is None for r in finished)
    assert all(r.unresolved_max_micro_usd for r in finished)


async def test_results_digest_identifies_the_exact_file() -> None:
    dataset = tickets()
    provider = SimulatedProvider("sim", dataset, {"m": 0.5})
    run = await run_evaluation(
        provider, "m", dataset, max_output_tokens=8, price=None, simulated=True
    )
    copy = EvaluationRun.from_json(json.loads(json.dumps(run.to_json())))
    other = await run_evaluation(
        provider, "m", dataset, max_output_tokens=8, price=None, simulated=True
    )

    assert copy.results_sha256() == run.results_sha256()
    assert other.results_sha256() != run.results_sha256()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda data: data.pop("format_version"), "format_version"),
        (lambda data: data["results"][0].update(first_token_ms=1.0), "first-token time"),
        (lambda data: data["results"][0].update(fields_expected=3), "field counts"),
    ],
    ids=["format", "first-token", "field-counts"],
)
async def test_results_files_from_other_formats_or_methods_are_rejected(
    change: Callable[[dict[str, Any]], object], message: str
) -> None:
    dataset = tickets()
    run = await run_evaluation(
        SimulatedProvider("sim", dataset, {"m": 1.0}),
        "m",
        dataset,
        max_output_tokens=8,
        price=None,
        simulated=True,
    )
    data = json.loads(json.dumps(run.to_json()))
    change(data)

    with pytest.raises(ValueError, match=message):
        EvaluationRun.from_json(data)
