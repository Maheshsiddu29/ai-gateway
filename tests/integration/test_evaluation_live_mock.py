"""The live evaluation path end to end, against the local mock provider over HTTP.

The CLI runs in a subprocess, as an operator would run it, and the mock listens
on a localhost port. Nothing leaves the machine and nothing is charged.
"""

import asyncio
import json
import os
import re
import sys
from pathlib import Path

from gateway.evaluation.runner import EvaluationRun
from mock_upstream import create_app as create_mock_app
from tests.servers import run_server

HOLDOUT = (
    Path(__file__).parents[2]
    / "evaluations"
    / "datasets"
    / "support-ticket-category-v1-holdout.jsonl"
)

CONFIG = """
[providers.mock-openai]
type = "openai_compatible"
base_url = "{url}/v1"
api_key_env = "MOCK_UPSTREAM_API_KEY"

[providers.mock-anthropic]
type = "anthropic"
base_url = "{url}"
api_key_env = "MOCK_UPSTREAM_API_KEY"

[models.fast]
provider = "mock-openai"
model = "mock-small"
max_output_tokens = 8

[cost]
enabled = true

[[cost.pricing]]
provider = "mock-openai"
model = "mock-small"
version = "mock-1"
effective_at = 2020-01-01T00:00:00Z
input_per_million_usd = "1.00"
output_per_million_usd = "2.00"

[[cost.pricing]]
provider = "mock-anthropic"
model = "mock-large"
version = "mock-1"
effective_at = 2020-01-01T00:00:00Z
input_per_million_usd = "3.00"
output_per_million_usd = "15.00"

[[routing.models]]
provider = "mock-openai"
model = "mock-small"
context_window_tokens = 8192
max_output_tokens = 1024
capabilities = ["streaming", "stream_usage"]

[[routing.models]]
provider = "mock-anthropic"
model = "mock-large"
context_window_tokens = 8192
max_output_tokens = 1024
capabilities = ["streaming", "stream_usage"]
"""


async def evaluate(*arguments: str) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "gateway.evaluation",
        *arguments,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "MOCK_UPSTREAM_API_KEY": "mock-upstream-key"},
    )
    stdout, stderr = await process.communicate()
    assert process.returncode is not None
    return process.returncode, stdout.decode(), stderr.decode()


async def test_an_approved_live_run_against_the_mock_provider(tmp_path: Path) -> None:
    async with run_server(create_mock_app(stream_delay_seconds=0)) as url:
        config = tmp_path / "gateway.toml"
        config.write_text(CONFIG.format(url=url))
        arguments = [
            "run",
            *("--config", str(config)),
            *("--dataset", str(HOLDOUT)),
            *("--target", "mock-openai/mock-small"),
            *("--target", "mock-anthropic/mock-large"),
            *("--max-tokens", "8"),
            *("--output", str(tmp_path / "out")),
            "--live",
            *("--max-cost-micro-usd", "100000"),
            *("--max-requests", "120"),
            "--stream",
        ]

        code, out, _ = await evaluate(*arguments)
        assert code == 3
        assert not (tmp_path / "out").exists()
        match = re.search(r"plan sha256 ([0-9a-f]{64})", out)
        assert match is not None

        code, out, err = await evaluate(*arguments, "--approve", match[1])

    assert code == 0, err
    runs = [
        EvaluationRun.from_json(json.loads(path.read_text()))
        for path in sorted((tmp_path / "out").glob("*.json"))
    ]
    assert not list((tmp_path / "out").glob("*.partial.jsonl"))
    assert {(run.identity.provider, run.identity.model) for run in runs} == {
        ("mock-openai", "mock-small"),
        ("mock-anthropic", "mock-large"),
    }
    for run in runs:
        identity, summary = run.identity, run.summary
        assert identity.partition.value == "holdout"
        assert identity.stream
        assert not identity.simulated
        assert identity.provider_base_url is not None
        assert identity.provider_base_url.startswith(url)
        assert summary.case_count == 60
        # The mock answers with a sentence, never a label.
        assert summary.invalid_count == 60
        assert summary.first_token_samples == 60
        assert summary.usage_reported_count == 60
        assert summary.cost_complete
