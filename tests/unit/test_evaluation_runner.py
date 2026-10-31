import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from gateway.cost.pricing import Price
from gateway.errors import ProviderUnavailableError
from gateway.evaluation.cli import main
from gateway.evaluation.dataset import Dataset, load_dataset
from gateway.evaluation.runner import EvaluationRun, mean_completion_tokens, run_evaluation
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
