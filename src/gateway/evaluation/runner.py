"""Run an evaluation dataset against one upstream model.

Cases are sent through the gateway's provider adapters (``Provider.complete``)
with bounded concurrency and a per-call timeout. Each case is scored with the
dataset's evaluator. A provider error is recorded as a failed case with its error
code; it is not retried, because a retry would hide unreliability.

The summary is derived only from the per-case results (``summarize``), so a
results file can be re-checked before it is recorded as evidence.
"""

import asyncio
import math
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from gateway.cost.pricing import Price
from gateway.errors import GatewayError
from gateway.evaluation.dataset import Dataset, EvaluationCase, EvaluationMethod, TaskType
from gateway.evaluation.scoring import EVALUATOR_VERSIONS, score_output
from gateway.evaluation.stats import percentile, wilson_interval
from gateway.providers.base import Provider, ProviderRequest

MAX_CONCURRENCY = 16


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    passed: bool
    score: float
    invalid_response: bool
    # None when the provider call failed.
    latency_ms: float | None
    prompt_tokens: int | None
    completion_tokens: int | None
    # Set only when usage was reported in billable dimensions and a price applied.
    cost_micro_usd: int | None
    error_code: str | None


@dataclass(frozen=True)
class RunIdentity:
    run_id: uuid.UUID
    provider: str
    model: str
    dataset: str
    dataset_version: str
    dataset_sha256: str
    task: TaskType
    method: EvaluationMethod
    evaluator_version: str
    # Results from SimulatedProvider; never used as routing evidence.
    simulated: bool
    max_output_tokens: int
    pricing_version: str | None
    started_at: datetime
    completed_at: datetime


@dataclass(frozen=True)
class RunSummary:
    case_count: int
    passed_count: int
    invalid_count: int
    error_count: int
    mean_score: float
    success_rate: float
    success_rate_low: float
    success_rate_high: float
    latency_samples: int
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    usage_reported_count: int
    prompt_tokens: int
    completion_tokens: int
    # Sum over cases with a known cost; complete only when every case has one.
    estimated_cost_micro_usd: int
    cost_complete: bool


@dataclass(frozen=True)
class EvaluationRun:
    identity: RunIdentity
    summary: RunSummary
    results: tuple[CaseResult, ...]

    def to_json(self) -> dict[str, Any]:
        identity = asdict(self.identity)
        identity["run_id"] = str(self.identity.run_id)
        identity["started_at"] = self.identity.started_at.isoformat()
        identity["completed_at"] = self.identity.completed_at.isoformat()
        return {
            "identity": identity,
            "summary": asdict(self.summary),
            "results": [asdict(result) for result in self.results],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "EvaluationRun":
        """Load a results file, rejecting it if its summary does not match its cases."""
        raw = dict(data["identity"])
        identity = RunIdentity(
            **{
                **raw,
                "run_id": uuid.UUID(raw["run_id"]),
                "task": TaskType(raw["task"]),
                "method": EvaluationMethod(raw["method"]),
                "started_at": datetime.fromisoformat(raw["started_at"]),
                "completed_at": datetime.fromisoformat(raw["completed_at"]),
            }
        )
        results = tuple(CaseResult(**result) for result in data["results"])
        for result in results:
            _check_case(result)
        summary = summarize(results)
        if asdict(summary) != data["summary"]:
            raise ValueError("the results file summary does not match its case results")
        if len({result.case_id for result in results}) != len(results):
            raise ValueError("the results file repeats a case")
        if identity.started_at > identity.completed_at:
            raise ValueError("the results file ends before it starts")
        return cls(identity, summary, results)


def _check_case(result: CaseResult) -> None:
    if not 0 <= result.score <= 1:
        raise ValueError(f"case {result.case_id} has a score outside [0, 1]")
    measurements = (
        result.latency_ms,
        result.prompt_tokens,
        result.completion_tokens,
        result.cost_micro_usd,
    )
    if any(value is not None and value < 0 for value in measurements):
        raise ValueError(f"case {result.case_id} has a negative measurement")
    if result.passed and (result.invalid_response or result.error_code is not None):
        raise ValueError(f"case {result.case_id} passed despite an invalid response or error")


def summarize(results: Sequence[CaseResult]) -> RunSummary:
    if not results:
        raise ValueError("a run needs at least one case")
    passed = sum(result.passed for result in results)
    latencies = [result.latency_ms for result in results if result.latency_ms is not None]
    with_usage = [
        result
        for result in results
        if result.prompt_tokens is not None and result.completion_tokens is not None
    ]
    costs = [result.cost_micro_usd for result in results if result.cost_micro_usd is not None]
    low, high = wilson_interval(passed, len(results))
    return RunSummary(
        case_count=len(results),
        passed_count=passed,
        invalid_count=sum(result.invalid_response for result in results),
        error_count=sum(result.error_code is not None for result in results),
        mean_score=sum(result.score for result in results) / len(results),
        success_rate=passed / len(results),
        success_rate_low=low,
        success_rate_high=high,
        latency_samples=len(latencies),
        latency_p50_ms=percentile(latencies, 0.5) if latencies else None,
        latency_p95_ms=percentile(latencies, 0.95) if latencies else None,
        usage_reported_count=len(with_usage),
        prompt_tokens=sum(result.prompt_tokens or 0 for result in with_usage),
        completion_tokens=sum(result.completion_tokens or 0 for result in with_usage),
        estimated_cost_micro_usd=sum(costs),
        cost_complete=len(costs) == len(results),
    )


async def run_evaluation(
    provider: Provider,
    model: str,
    dataset: Dataset,
    *,
    max_output_tokens: int,
    price: Price | None,
    simulated: bool,
    concurrency: int = 4,
    call_timeout_seconds: float = 60.0,
    clock: Callable[[], float] = time.perf_counter,
) -> EvaluationRun:
    if not 1 <= concurrency <= MAX_CONCURRENCY:
        raise ValueError(f"concurrency must be between 1 and {MAX_CONCURRENCY}")
    started_at = datetime.now(UTC)
    semaphore = asyncio.Semaphore(concurrency)

    async def one(case: EvaluationCase) -> CaseResult:
        async with semaphore:
            return await _run_case(
                provider, model, case, max_output_tokens, price, call_timeout_seconds, clock
            )

    results = tuple(await asyncio.gather(*(one(case) for case in dataset.cases)))
    identity = RunIdentity(
        run_id=uuid.uuid4(),
        provider=provider.name,
        model=model,
        dataset=dataset.name,
        dataset_version=dataset.version,
        dataset_sha256=dataset.sha256,
        task=dataset.task,
        method=dataset.method,
        evaluator_version=EVALUATOR_VERSIONS[dataset.method],
        simulated=simulated,
        max_output_tokens=max_output_tokens,
        pricing_version=price.version if price is not None else None,
        started_at=started_at,
        completed_at=datetime.now(UTC),
    )
    return EvaluationRun(identity, summarize(results), results)


async def _run_case(
    provider: Provider,
    model: str,
    case: EvaluationCase,
    max_output_tokens: int,
    price: Price | None,
    call_timeout_seconds: float,
    clock: Callable[[], float],
) -> CaseResult:
    request = ProviderRequest(
        model=model,
        messages=tuple(case.messages),
        max_tokens=max_output_tokens,
        temperature=None,
        top_p=None,
        stop=(),
        include_usage=False,
    )
    started = clock()
    try:
        async with asyncio.timeout(call_timeout_seconds):
            completion = await provider.complete(request)
    except GatewayError as exc:
        return _failed(case, exc.code)
    except TimeoutError:
        return _failed(case, "evaluation_timeout")
    latency_ms = round((clock() - started) * 1000, 3)
    score = score_output(case, completion.content)
    usage = completion.usage
    cost = (
        price.charge_micro_usd(usage.prompt_tokens, usage.completion_tokens)
        if price is not None and usage is not None and usage.billing_dimensions_supported
        else None
    )
    return CaseResult(
        case_id=case.id,
        passed=score.passed,
        score=score.value,
        invalid_response=score.invalid_response,
        latency_ms=latency_ms,
        prompt_tokens=usage.prompt_tokens if usage else None,
        completion_tokens=usage.completion_tokens if usage else None,
        cost_micro_usd=cost,
        error_code=None,
    )


def _failed(case: EvaluationCase, code: str) -> CaseResult:
    return CaseResult(
        case_id=case.id,
        passed=False,
        score=0.0,
        invalid_response=False,
        latency_ms=None,
        prompt_tokens=None,
        completion_tokens=None,
        cost_micro_usd=None,
        error_code=code,
    )


def mean_completion_tokens(summary: RunSummary) -> int | None:
    """Mean output tokens per case with reported usage, rounded up."""
    if summary.usage_reported_count == 0:
        return None
    return math.ceil(summary.completion_tokens / summary.usage_reported_count)
