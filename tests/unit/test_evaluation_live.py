"""Safeguards for evaluations that call real providers. No test here makes a network call."""

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from gateway.core.config import ProviderConfig
from gateway.evaluation import cli
from gateway.evaluation.cli import main
from gateway.evaluation.dataset import load_dataset
from gateway.evaluation.journal import RunJournal
from gateway.evaluation.runner import EvaluationRun
from gateway.providers.base import Provider
from gateway.providers.factory import build_provider
from tests.fakes import UpstreamStub

TICKETS = (
    Path(__file__).parents[2] / "evaluations" / "datasets" / "support-ticket-category-v1.jsonl"
)
CASES = len(load_dataset(TICKETS).cases)

CONFIG = """
[providers.sim]
type = "openai_compatible"
base_url = "https://sim.test/v1"
api_key_env = "SIM_KEY"

[providers.anth]
type = "anthropic"
base_url = "https://anth.test"
api_key_env = "ANTH_KEY"

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
input_per_million_usd = "{input_price}"
output_per_million_usd = "2"

[[cost.pricing]]
provider = "sim"
model = "unregistered"
version = "p1"
effective_at = 2020-01-01T00:00:00Z
input_per_million_usd = "1"
output_per_million_usd = "2"

[[cost.pricing]]
provider = "anth"
model = "a"
version = "p1"
effective_at = 2020-01-01T00:00:00Z
input_per_million_usd = "1"
output_per_million_usd = "2"

[[routing.models]]
provider = "sim"
model = "m"
context_window_tokens = {context}
max_output_tokens = 16
capabilities = ["stop"]

[[routing.models]]
provider = "anth"
model = "a"
context_window_tokens = 100000
max_output_tokens = 16
capabilities = ["streaming", "stream_usage"]
"""


def write_config(tmp_path: Path, *, input_price: str = "1", context: int = 100000) -> Path:
    path = tmp_path / "gateway.toml"
    path.write_text(CONFIG.format(input_price=input_price, context=context))
    return path


def live(tmp_path: Path, *extra: str, target: str = "sim/m", config: Path | None = None) -> int:
    return main(
        [
            "run",
            *("--config", str(config or write_config(tmp_path))),
            *("--dataset", str(TICKETS)),
            *("--target", target),
            *("--max-tokens", "8"),
            *("--output", str(tmp_path / "out")),
            "--live",
            *extra,
        ]
    )


BOUNDS = ("--max-cost-micro-usd", "1000000", "--max-requests", "1000")


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fail the test if the CLI builds a real provider; record attempts."""
    built: list[str] = []

    def refuse(name: str, *_: object) -> Provider:
        built.append(name)
        raise AssertionError("a provider was built")

    monkeypatch.setattr(cli, "build_provider", refuse)
    return built


def digest_from(output: str) -> str:
    match = re.search(r"plan sha256 ([0-9a-f]{64})", output)
    assert match is not None
    return match[1]


def test_live_mode_prints_a_plan_and_sends_nothing_without_approval(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    no_network: list[str],
) -> None:
    # Credentials being present is not approval.
    monkeypatch.setenv("SIM_KEY", "sk-present")

    code = live(tmp_path, *BOUNDS, "--plan-output", str(tmp_path / "plan.json"))

    out = capsys.readouterr()
    assert code == 3
    assert no_network == []
    assert not (tmp_path / "out").exists()
    assert "No provider has been called" in out.out
    assert f"{CASES} requests (limit 1000)" in out.out
    assert "sim/m at https://sim.test/v1, price p1" in out.out
    assert "--approve" in out.err
    plan = json.loads((tmp_path / "plan.json").read_text())
    assert plan["sha256"] == digest_from(out.out)
    assert plan["targets"][0]["worst_case_micro_usd"] > 0
    assert "sk-present" not in out.out + out.err + (tmp_path / "plan.json").read_text()


def test_the_plan_digest_is_stable_and_covers_prices(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_network: list[str]
) -> None:
    live(tmp_path, *BOUNDS)
    first = digest_from(capsys.readouterr().out)
    live(tmp_path, *BOUNDS)
    second = digest_from(capsys.readouterr().out)

    live(tmp_path, *BOUNDS, config=write_config(tmp_path, input_price="1.5"))
    repriced = digest_from(capsys.readouterr().out)

    assert first == second
    assert repriced != first


def test_a_stale_or_wrong_approval_sends_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_network: list[str]
) -> None:
    live(tmp_path, *BOUNDS)
    approved = digest_from(capsys.readouterr().out)

    # The operator approved one plan, then the prices changed.
    code = live(
        tmp_path, *BOUNDS, "--approve", approved, config=write_config(tmp_path, input_price="9")
    )

    assert code == 2
    assert "does not match this plan" in capsys.readouterr().err
    assert no_network == []
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize(
    ("extra", "target", "config", "message"),
    [
        ((), "sim/m", {}, "requires --max-cost-micro-usd"),
        (("--max-cost-micro-usd", "100"), "sim/m", {}, "requires --max-requests"),
        (("--max-cost-micro-usd", "1", "--max-requests", "1000"), "sim/m", {}, "exceeds"),
        (("--max-cost-micro-usd", "10**9", "--max-requests", "10"), "sim/m", {}, "above"),
        (BOUNDS, "sim/unpriced", {}, "no effective price"),
        (BOUNDS, "sim/unregistered", {}, "no [[routing.models]] entry"),
        ((*BOUNDS, "--stream"), "sim/m", {}, "does not list"),
        (BOUNDS, "sim/m", {"context": 20}, "context window"),
    ],
    ids=[
        "no-ceiling",
        "no-request-limit",
        "over-ceiling",
        "over-request-limit",
        "unpriced",
        "unregistered",
        "streaming-unsupported",
        "context-window",
    ],
)
def test_unbounded_or_unsupported_live_runs_are_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    no_network: list[str],
    extra: tuple[str, ...],
    target: str,
    config: dict[str, Any],
    message: str,
) -> None:
    code = live(
        tmp_path,
        *(str(10**9) if value == "10**9" else value for value in extra),
        target=target,
        config=write_config(tmp_path, **config),
    )

    assert code == 2
    assert message in capsys.readouterr().err
    assert no_network == []


def test_the_output_limit_comes_from_the_registry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_network: list[str]
) -> None:
    code = main(
        [
            "run",
            *("--config", str(write_config(tmp_path))),
            *("--dataset", str(TICKETS)),
            *("--target", "sim/m"),
            *("--max-tokens", "17"),
            *("--output", str(tmp_path / "out")),
            "--live",
            *BOUNDS,
        ]
    )

    assert code == 2
    assert "exceeds the output limit of 16" in capsys.readouterr().err


def test_an_adapter_that_rejects_a_case_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], no_network: list[str]
) -> None:
    dataset = tmp_path / "late-system.jsonl"
    case = json.loads(TICKETS.read_text().splitlines()[0])
    # The Anthropic adapter cannot send a system message after a user message.
    case["messages"] = [case["messages"][1], case["messages"][0]]
    dataset.write_text(json.dumps(case) + "\n")

    code = main(
        [
            "run",
            *("--config", str(write_config(tmp_path))),
            *("--dataset", str(dataset)),
            *("--target", "anth/a"),
            *("--max-tokens", "8"),
            *("--output", str(tmp_path / "out")),
            "--live",
            *BOUNDS,
        ]
    )

    assert code == 2
    assert "anth/a rejects case ticket-001" in capsys.readouterr().err


def openai_answer(prompt_tokens: int = 50) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "x",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "billing"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": 1,
                    "total_tokens": prompt_tokens + 1,
                },
            },
        )

    return handler


def stub_providers(monkeypatch: pytest.MonkeyPatch, stub: UpstreamStub) -> list[str]:
    built: list[str] = []

    def factory(name: str, config: ProviderConfig, key: SecretStr | None) -> Provider:
        built.append(name)
        return build_provider(name, config, key, stub)

    monkeypatch.setattr(cli, "build_provider", factory)
    return built


def test_an_approved_plan_runs_once_per_case_and_keeps_no_journal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SIM_KEY", "sk-test")
    stub = UpstreamStub(openai_answer())
    built = stub_providers(monkeypatch, stub)
    live(tmp_path, *BOUNDS, "--concurrency", "2")
    digest = digest_from(capsys.readouterr().out)

    code = live(tmp_path, *BOUNDS, "--concurrency", "2", "--approve", digest)

    assert code == 0
    assert built == ["sim"]
    assert len(stub.requests) == CASES
    [path] = (tmp_path / "out").iterdir()
    assert path.name.startswith("support-ticket-category-v1-development-sim-m-")
    run = EvaluationRun.from_json(json.loads(path.read_text()))
    assert not run.identity.simulated
    assert run.complete
    assert run.identity.provider_base_url == "https://sim.test/v1"
    assert run.summary.cost_complete
    assert "sk-test" not in capsys.readouterr().out


def test_provider_errors_are_not_retried(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SIM_KEY", "sk-test")
    stub = UpstreamStub(lambda _: httpx.Response(503, json={"error": {"message": "busy"}}))
    stub_providers(monkeypatch, stub)
    live(tmp_path, *BOUNDS)
    digest = digest_from(capsys.readouterr().out)

    live(tmp_path, *BOUNDS, "--approve", digest)

    assert len(stub.requests) == CASES
    [path] = (tmp_path / "out").iterdir()
    run = EvaluationRun.from_json(json.loads(path.read_text()))
    assert run.summary.error_count == CASES
    # A 5xx may have run, so its cost is unresolved and bounded by the worst case.
    assert run.summary.unresolved_case_count == CASES
    assert run.summary.unresolved_max_micro_usd is not None


def test_spending_beyond_the_estimate_stops_at_the_ceiling(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SIM_KEY", "sk-test")
    # The provider reports far more input than estimated: 2,000 micro-USD a call.
    stub = UpstreamStub(openai_answer(prompt_tokens=2000))
    stub_providers(monkeypatch, stub)
    # The plan's worst case is about 11,500 micro-USD, so it is approved.
    bounds = ("--max-cost-micro-usd", "12000", "--max-requests", "1000", "--concurrency", "1")
    live(tmp_path, *bounds)
    digest = digest_from(capsys.readouterr().out)

    code = live(tmp_path, *bounds, "--approve", digest)

    assert code == 4
    assert "cost ceiling" in capsys.readouterr().err
    [path] = (tmp_path / "out").iterdir()
    run = EvaluationRun.from_json(json.loads(path.read_text()))
    assert run.identity.stop_reason == "cost_ceiling"
    assert run.summary.case_count == len(stub.requests) == 6
    # The last call sent fit its worst-case reservation but reported more input
    # than estimated: the ceiling limits dispatch, not what a call in flight costs.
    assert 12000 < run.summary.estimated_cost_micro_usd < 12000 + 2002


def test_an_interrupted_run_keeps_its_journal_and_is_not_resumed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "out"
    output.mkdir()
    journal = RunJournal(output / "earlier.partial.jsonl", {"run_id": "earlier"})
    journal.dispatched("ticket-001")
    journal.close()
    before = journal.path.read_text()

    async def interrupted(_: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "_run", interrupted)
    arguments = ["--config", "x", "--dataset", "x", "--target", "x", "--max-tokens", "1"]
    assert main(["run", *arguments, "--output", str(output), "--simulate", "x=1"]) == 130
    assert "will not be resumed" in capsys.readouterr().err
    monkeypatch.undo()

    code = main(
        [
            "run",
            *("--config", str(write_config(tmp_path))),
            *("--dataset", str(TICKETS)),
            *("--target", "sim/m"),
            *("--max-tokens", "8"),
            *("--output", str(output)),
            *("--simulate", "sim/m=1"),
        ]
    )

    assert code == 0
    # The new run started from the first case; the old journal is untouched.
    assert journal.path.read_text() == before
    [results] = output.glob("*.json")
    assert EvaluationRun.from_json(json.loads(results.read_text())).summary.case_count == CASES


def test_the_journal_records_dispatch_and_results_durably(tmp_path: Path) -> None:
    journal = RunJournal(tmp_path / "run.partial.jsonl", {"run_id": "r"})
    journal.dispatched("a")
    journal.close()

    lines = [json.loads(line) for line in (tmp_path / "run.partial.jsonl").read_text().splitlines()]

    assert lines == [{"journal_format": 1, "run": {"run_id": "r"}}, {"dispatched": "a"}]
    with pytest.raises(FileExistsError):
        RunJournal(tmp_path / "run.partial.jsonl", {"run_id": "other"})
