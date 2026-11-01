"""``python -m gateway.evaluation status``; set GATEWAY_TEST_DATABASE_URL."""

import asyncio
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from gateway.evaluation.cli import main
from gateway.evaluation.dataset import load_dataset
from gateway.evaluation.runner import run_evaluation
from gateway.evaluation.simulated import SimulatedProvider
from gateway.evaluation.store import record_run

TICKETS = (
    Path(__file__).parents[2] / "evaluations" / "datasets" / "support-ticket-category-v1.jsonl"
)

CONFIG = """
[providers.{cheap}]
type = "openai_compatible"
base_url = "https://cheap.invalid/v1"

[providers.{premium}]
type = "openai_compatible"
base_url = "https://premium.invalid/v1"

[models.classify]
provider = "{premium}"
model = "large"
max_output_tokens = 8

[models.classify.routing]
policy = "quality_constrained"
candidates = [{{ provider = "{cheap}", model = "small" }}]
dataset = "support-ticket-category"
dataset_version = "1"
min_success_rate = 0.85
min_samples = 40
max_evidence_age_days = 30

[cost]
enabled = true

[[cost.pricing]]
provider = "{cheap}"
model = "small"
version = "v1"
effective_at = 2020-01-01T00:00:00Z
input_per_million_usd = "1"
output_per_million_usd = "1"

[[cost.pricing]]
provider = "{premium}"
model = "large"
version = "v1"
effective_at = 2020-01-01T00:00:00Z
input_per_million_usd = "5"
output_per_million_usd = "5"

[routing]
enabled = true

[[routing.models]]
provider = "{cheap}"
model = "small"
context_window_tokens = 8192
max_output_tokens = 64

[[routing.models]]
provider = "{premium}"
model = "large"
context_window_tokens = 8192
max_output_tokens = 64
"""


async def record_measurement(url: str, provider: str, model: str) -> None:
    dataset = load_dataset(TICKETS)
    run = await run_evaluation(
        SimulatedProvider(provider, dataset, {model: 1.0}),
        model,
        dataset,
        max_output_tokens=8,
        price=None,
        # Presented as a measurement; routing ignores simulated runs.
        simulated=False,
    )
    engine = create_async_engine(url)
    try:
        await record_run(engine, run)
    finally:
        await engine.dispose()


def test_status_shows_which_candidates_qualify(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    url = os.environ.get("GATEWAY_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set GATEWAY_TEST_DATABASE_URL for PostgreSQL tests")
    suffix = uuid.uuid4().hex[:8]
    cheap, premium = f"cheap{suffix}", f"premium{suffix}"
    config = tmp_path / "gateway.toml"
    config.write_text(CONFIG.format(cheap=cheap, premium=premium))
    asyncio.run(record_measurement(url, cheap, "small"))
    monkeypatch.setenv("GATEWAY_DATABASE_URL", url)

    assert main(["status", "--config", str(config)]) == 0

    out = capsys.readouterr().out
    assert "classify: support-ticket-category v1" in out
    assert "when nothing qualifies: primary" in out
    assert f"{cheap}/small: qualifies; run " in out
    assert "44/44 passed" in out
    assert f"{premium}/large: no_evidence; no development run recorded" in out


def test_status_needs_a_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "gateway.toml"
    config.write_text(CONFIG.format(cheap="cheap", premium="premium"))
    monkeypatch.delenv("GATEWAY_DATABASE_URL", raising=False)

    assert main(["status", "--config", str(config)]) == 2
    assert "GATEWAY_DATABASE_URL" in capsys.readouterr().err
