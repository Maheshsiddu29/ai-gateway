"""Run an evaluation dataset against one upstream model.

Cases are sent through the gateway's provider adapters (``Provider.complete``, or
``Provider.stream`` for streaming runs) with bounded concurrency and a per-call
timeout. Each case is scored with the dataset's evaluator. A provider error is
recorded as a failed case with its error code; it is not retried, because a retry
would hide unreliability and could pay twice for a call that already ran.

An optional ``CaseObserver`` hears about each case when it is dispatched and when
its result is known, so a caller can journal progress to disk. If the run is
cancelled, every case still waiting for its provider is reported as
``evaluation_interrupted`` before the cancellation propagates.

An optional ``CostCeiling`` is checked before each dispatch. Each case reserves
its worst-case cost and settles to its known cost afterwards. When a reservation
does not fit, no further case is sent and the run ends with
``stop_reason = "cost_ceiling"``.

The summary is derived only from the per-case results (``summarize``), so a
results file can be re-checked before it is recorded as evidence.
"""

import asyncio
import hashlib
import json
import math
import time
import uuid
from collections.abc import Callable, Sequence
from contextlib import aclosing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from gateway.admission.tokens import estimate_input_tokens
from gateway.cost.pricing import Price
from gateway.errors import (
    GatewayError,
    InternalGatewayError,
    InvalidRequestError,
    ProviderError,
    UnsupportedCapabilityError,
)
from gateway.evaluation.dataset import (
    Dataset,
    EvaluationCase,
    EvaluationMethod,
    Partition,
    TaskType,
)
from gateway.evaluation.scoring import EVALUATOR_VERSIONS, Score, score_output, unanswered
from gateway.evaluation.stats import reported_percentile, wilson_interval
from gateway.providers.base import Provider, ProviderRequest, TextDelta, Usage

MAX_CONCURRENCY = 16
# Bumped whenever a field is added to or removed from results files.
RESULTS_FORMAT_VERSION = 2

COST_CEILING = "cost_ceiling"
TIMEOUT = "evaluation_timeout"
INTERRUPTED = "evaluation_interrupted"
# Finish reasons that mark a refusal or a truncated answer.
REFUSAL_FINISH_REASON = "content_filter"
TRUNCATED_FINISH_REASON = "length"


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    passed: bool
    score: float
    invalid_response: bool
    # None when the provider call failed.
    latency_ms: float | None
    # Time to the first streamed text; None for non-streaming runs and failed calls.
    first_token_ms: float | None
    finish_reason: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    # The charge at the run's price for usage reported in billable dimensions, or 0
    # when the provider certainly did not run the call. None when unknown.
    cost_micro_usd: int | None
    # When the cost is unknown: the most the call could have cost (estimated input
    # plus the full output allowance). None when there is no price.
    unresolved_max_micro_usd: int | None
    error_code: str | None
    # Extraction only: the counts behind the F1 score. Failed and invalid responses
    # predict nothing, so their expected fields count against recall.
    fields_correct: int | None
    fields_predicted: int | None
    fields_expected: int | None


@dataclass(frozen=True)
class RunIdentity:
    run_id: uuid.UUID
    provider: str
    model: str
    # The provider's configured base URL; None for simulated runs.
    provider_base_url: str | None
    dataset: str
    dataset_version: str
    dataset_sha256: str
    partition: Partition
    task: TaskType
    method: EvaluationMethod
    evaluator_version: str
    # Results from SimulatedProvider; never used as routing evidence.
    simulated: bool
    stream: bool
    max_output_tokens: int
    pricing_version: str | None
    started_at: datetime
    completed_at: datetime
    # Why the run ended before every case was sent, or None if every case ran.
    # Incomplete runs are never routing evidence.
    stop_reason: str | None = None


@dataclass(frozen=True)
class RunSummary:
    case_count: int
    passed_count: int
    invalid_count: int
    error_count: int
    timeout_count: int
    refusal_count: int
    truncated_count: int
    mean_score: float
    success_rate: float
    success_rate_low: float
    success_rate_high: float
    # Micro-averaged over all expected and predicted fields; extraction only.
    field_precision: float | None
    field_recall: float | None
    field_f1: float | None
    latency_samples: int
    # Percentiles are None when there are too few samples (see stats).
    latency_p50_ms: float | None
    latency_p95_ms: float | None
    latency_p99_ms: float | None
    first_token_samples: int
    first_token_p50_ms: float | None
    first_token_p95_ms: float | None
    usage_reported_count: int
    prompt_tokens: int
    completion_tokens: int
    # Sum over cases with a known cost; complete only when every case has one.
    estimated_cost_micro_usd: int
    cost_complete: bool
    unresolved_case_count: int
    # Upper bound on the unknown costs; None when some unknown cost has no bound.
    unresolved_max_micro_usd: int | None


@dataclass(frozen=True)
class EvaluationRun:
    identity: RunIdentity
    summary: RunSummary
    results: tuple[CaseResult, ...]

    @property
    def complete(self) -> bool:
        return self.identity.stop_reason is None

    def results_sha256(self) -> str:
        """Digest of the canonical results file; recorded to detect conflicting copies."""
        canonical = json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def to_json(self) -> dict[str, Any]:
        identity = asdict(self.identity)
        identity["run_id"] = str(self.identity.run_id)
        identity["started_at"] = self.identity.started_at.isoformat()
        identity["completed_at"] = self.identity.completed_at.isoformat()
        return {
            "format_version": RESULTS_FORMAT_VERSION,
            "identity": identity,
            "summary": asdict(self.summary),
            "results": [asdict(result) for result in self.results],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "EvaluationRun":
        """Load a results file, rejecting it if its summary does not match its cases."""
        if data.get("format_version") != RESULTS_FORMAT_VERSION:
            raise ValueError(
                f"results files must have format_version {RESULTS_FORMAT_VERSION};"
                " re-run older evaluations"
            )
        raw = dict(data["identity"])
        identity = RunIdentity(
            **{
                **raw,
                "run_id": uuid.UUID(raw["run_id"]),
                "partition": Partition(raw["partition"]),
                "task": TaskType(raw["task"]),
                "method": EvaluationMethod(raw["method"]),
                "started_at": datetime.fromisoformat(raw["started_at"]),
                "completed_at": datetime.fromisoformat(raw["completed_at"]),
            }
        )
        results = tuple(CaseResult(**result) for result in data["results"])
        for result in results:
            _check_case(result, identity)
        summary = summarize(results)
        if asdict(summary) != data["summary"]:
            raise ValueError("the results file summary does not match its case results")
        if len({result.case_id for result in results}) != len(results):
            raise ValueError("the results file repeats a case")
        if identity.started_at > identity.completed_at:
            raise ValueError("the results file ends before it starts")
        return cls(identity, summary, results)


def _check_case(result: CaseResult, identity: RunIdentity) -> None:
    if not 0 <= result.score <= 1:
        raise ValueError(f"case {result.case_id} has a score outside [0, 1]")
    measurements = (
        result.latency_ms,
        result.first_token_ms,
        result.prompt_tokens,
        result.completion_tokens,
        result.cost_micro_usd,
        result.unresolved_max_micro_usd,
        result.fields_correct,
        result.fields_predicted,
        result.fields_expected,
    )
    if any(value is not None and value < 0 for value in measurements):
        raise ValueError(f"case {result.case_id} has a negative measurement")
    if result.passed and (result.invalid_response or result.error_code is not None):
        raise ValueError(f"case {result.case_id} passed despite an invalid response or error")
    if result.first_token_ms is not None and not identity.stream:
        raise ValueError(f"case {result.case_id} has a first-token time in a non-streaming run")
    counts = (result.fields_correct, result.fields_predicted, result.fields_expected)
    present = {count is not None for count in counts}
    if present != {identity.method is EvaluationMethod.FIELD_F1}:
        raise ValueError(f"case {result.case_id} has field counts that do not fit its method")
    correct, predicted, expected = counts
    if (
        correct is not None
        and predicted is not None
        and expected is not None
        and correct > min(predicted, expected)
    ):
        raise ValueError(f"case {result.case_id} has more correct fields than possible")


def summarize(results: Sequence[CaseResult]) -> RunSummary:
    if not results:
        raise ValueError("a run needs at least one case")
    passed = sum(result.passed for result in results)
    latencies = [result.latency_ms for result in results if result.latency_ms is not None]
    first_tokens = [r.first_token_ms for r in results if r.first_token_ms is not None]
    with_usage = [
        result
        for result in results
        if result.prompt_tokens is not None and result.completion_tokens is not None
    ]
    costs = [result.cost_micro_usd for result in results if result.cost_micro_usd is not None]
    unresolved = [result for result in results if result.cost_micro_usd is None]
    bounds = [result.unresolved_max_micro_usd for result in unresolved]
    low, high = wilson_interval(passed, len(results))
    precision, recall, f1 = _field_scores(results)
    return RunSummary(
        case_count=len(results),
        passed_count=passed,
        invalid_count=sum(result.invalid_response for result in results),
        error_count=sum(result.error_code is not None for result in results),
        timeout_count=sum(result.error_code == TIMEOUT for result in results),
        refusal_count=sum(r.finish_reason == REFUSAL_FINISH_REASON for r in results),
        truncated_count=sum(r.finish_reason == TRUNCATED_FINISH_REASON for r in results),
        mean_score=sum(result.score for result in results) / len(results),
        success_rate=passed / len(results),
        success_rate_low=low,
        success_rate_high=high,
        field_precision=precision,
        field_recall=recall,
        field_f1=f1,
        latency_samples=len(latencies),
        latency_p50_ms=reported_percentile(latencies, 0.5),
        latency_p95_ms=reported_percentile(latencies, 0.95),
        latency_p99_ms=reported_percentile(latencies, 0.99),
        first_token_samples=len(first_tokens),
        first_token_p50_ms=reported_percentile(first_tokens, 0.5),
        first_token_p95_ms=reported_percentile(first_tokens, 0.95),
        usage_reported_count=len(with_usage),
        prompt_tokens=sum(result.prompt_tokens or 0 for result in with_usage),
        completion_tokens=sum(result.completion_tokens or 0 for result in with_usage),
        estimated_cost_micro_usd=sum(costs),
        cost_complete=not unresolved,
        unresolved_case_count=len(unresolved),
        unresolved_max_micro_usd=(
            None if any(bound is None for bound in bounds) else sum(b or 0 for b in bounds)
        ),
    )


def _field_scores(
    results: Sequence[CaseResult],
) -> tuple[float | None, float | None, float | None]:
    if any(result.fields_expected is None for result in results):
        return None, None, None
    correct = sum(result.fields_correct or 0 for result in results)
    predicted = sum(result.fields_predicted or 0 for result in results)
    expected = sum(result.fields_expected or 0 for result in results)
    if not expected:
        return None, None, None
    recall = correct / expected
    if not predicted:
        # Nothing was predicted, so nothing was found: recall and F1 are 0.
        return None, recall, 0.0
    precision = correct / predicted
    f1 = 2 * precision * recall / (precision + recall) if correct else 0.0
    return precision, recall, f1


class CostCeilingError(Exception):
    pass


class CostCeiling:
    """A spending limit shared by the runs of one evaluation command.

    Committed spend counts each finished case at its known cost and every other
    sent case at its worst case. A provider can still report more input tokens
    than the estimate, so calls already in flight can take spend past the limit;
    the limit is a safeguard, not a bound on a provider's invoice.
    """

    def __init__(self, limit_micro_usd: int) -> None:
        if limit_micro_usd < 0:
            raise ValueError("the cost ceiling cannot be negative")
        self.limit_micro_usd = limit_micro_usd
        self.committed_micro_usd = 0
        self.reached = False

    def reserve(self, amount: int) -> bool:
        if self.reached or self.committed_micro_usd + amount > self.limit_micro_usd:
            self.reached = True
            return False
        self.committed_micro_usd += amount
        return True

    def settle(self, reserved: int, cost: int | None) -> None:
        if cost is not None:
            self.committed_micro_usd += cost - reserved


class CaseObserver(Protocol):
    def dispatched(self, case_id: str) -> None: ...

    def finished(self, result: CaseResult) -> None: ...


def case_max_cost(price: Price, case: EvaluationCase, max_output_tokens: int) -> int:
    """The most one case can cost: its estimated input and the full output allowance."""
    return price.charge_micro_usd(estimate_input_tokens(case.messages), max_output_tokens)


async def run_evaluation(
    provider: Provider,
    model: str,
    dataset: Dataset,
    *,
    max_output_tokens: int,
    price: Price | None,
    simulated: bool,
    stream: bool = False,
    provider_base_url: str | None = None,
    run_id: uuid.UUID | None = None,
    observer: CaseObserver | None = None,
    ceiling: CostCeiling | None = None,
    concurrency: int = 4,
    call_timeout_seconds: float = 60.0,
    clock: Callable[[], float] = time.perf_counter,
) -> EvaluationRun:
    if not 1 <= concurrency <= MAX_CONCURRENCY:
        raise ValueError(f"concurrency must be between 1 and {MAX_CONCURRENCY}")
    if ceiling is not None and price is None:
        raise ValueError("a cost ceiling needs a price")
    started_at = datetime.now(UTC)
    semaphore = asyncio.Semaphore(concurrency)
    finished: dict[str, CaseResult] = {}

    def record(result: CaseResult) -> None:
        finished[result.case_id] = result
        if observer is not None:
            observer.finished(result)

    async def one(case: EvaluationCase) -> None:
        async with semaphore:
            reserved = 0
            if ceiling is not None and price is not None:
                reserved = case_max_cost(price, case, max_output_tokens)
                if not ceiling.reserve(reserved):
                    return
            if observer is not None:
                observer.dispatched(case.id)
            try:
                result = await _run_case(
                    provider,
                    model,
                    case,
                    max_output_tokens,
                    price,
                    stream,
                    call_timeout_seconds,
                    clock,
                )
            except asyncio.CancelledError:
                # The provider may already be running (and charging for) the call.
                record(_failed(case, INTERRUPTED, price, max_output_tokens, may_have_run=True))
                raise
            if ceiling is not None:
                ceiling.settle(reserved, result.cost_micro_usd)
            record(result)

    async with asyncio.TaskGroup() as group:
        for case in dataset.cases:
            group.create_task(one(case))

    results = tuple(finished[case.id] for case in dataset.cases if case.id in finished)
    if not results:
        raise CostCeilingError("no case could be sent within the cost ceiling")
    identity = RunIdentity(
        run_id=run_id or uuid.uuid4(),
        provider=provider.name,
        model=model,
        provider_base_url=provider_base_url,
        dataset=dataset.name,
        dataset_version=dataset.version,
        dataset_sha256=dataset.sha256,
        partition=dataset.partition,
        task=dataset.task,
        method=dataset.method,
        evaluator_version=EVALUATOR_VERSIONS[dataset.method],
        simulated=simulated,
        stream=stream,
        max_output_tokens=max_output_tokens,
        pricing_version=price.version if price is not None else None,
        started_at=started_at,
        completed_at=datetime.now(UTC),
        stop_reason=None if len(results) == len(dataset.cases) else COST_CEILING,
    )
    return EvaluationRun(identity, summarize(results), results)


async def _run_case(
    provider: Provider,
    model: str,
    case: EvaluationCase,
    max_output_tokens: int,
    price: Price | None,
    stream: bool,
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
        include_usage=stream,
    )
    started = clock()
    try:
        async with asyncio.timeout(call_timeout_seconds):
            answer = await _call(provider, request, stream, clock)
    except GatewayError as exc:
        return _failed(case, exc.code, price, max_output_tokens, _may_have_run(exc))
    except TimeoutError:
        return _failed(case, TIMEOUT, price, max_output_tokens, may_have_run=True)
    latency_ms = round((clock() - started) * 1000, 3)
    score = score_output(case, answer.content)
    usage = answer.usage
    cost = _cost(price, usage)
    return CaseResult(
        case_id=case.id,
        passed=score.passed,
        score=score.value,
        invalid_response=score.invalid_response,
        latency_ms=latency_ms,
        first_token_ms=(
            None if answer.first_token is None else round((answer.first_token - started) * 1000, 3)
        ),
        finish_reason=answer.finish_reason,
        prompt_tokens=usage.prompt_tokens if usage else None,
        completion_tokens=usage.completion_tokens if usage else None,
        cost_micro_usd=cost,
        unresolved_max_micro_usd=(
            case_max_cost(price, case, max_output_tokens)
            if cost is None and price is not None
            else None
        ),
        error_code=None,
        **_field_counts(score),
    )


@dataclass(frozen=True)
class _Answer:
    content: str
    finish_reason: str | None
    usage: Usage | None
    # Clock reading when the first streamed text arrived.
    first_token: float | None


async def _call(
    provider: Provider, request: ProviderRequest, stream: bool, clock: Callable[[], float]
) -> _Answer:
    if not stream:
        completion = await provider.complete(request)
        return _Answer(completion.content, completion.finish_reason, completion.usage, None)
    parts: list[str] = []
    first_token: float | None = None
    async with aclosing(provider.stream(request)) as events:
        async for event in events:
            if isinstance(event, TextDelta):
                first_token = clock() if first_token is None else first_token
                parts.append(event.text)
            else:
                return _Answer("".join(parts), event.finish_reason, event.usage, first_token)
    # Adapters raise instead of ending a stream without StreamEnd.
    raise InternalGatewayError("The provider stream ended without a final event.")


def _cost(price: Price | None, usage: Usage | None) -> int | None:
    if price is None or usage is None or not usage.billing_dimensions_supported:
        return None
    return price.charge_micro_usd(usage.prompt_tokens, usage.completion_tokens)


def _may_have_run(exc: GatewayError) -> bool:
    if isinstance(exc, ProviderError):
        return exc.may_have_processed
    # The adapter refused the request before sending it.
    return not isinstance(exc, InvalidRequestError | UnsupportedCapabilityError)


def _field_counts(score: Score) -> dict[str, int | None]:
    return {
        "fields_correct": score.fields_correct,
        "fields_predicted": score.fields_predicted,
        "fields_expected": score.fields_expected,
    }


def _failed(
    case: EvaluationCase,
    code: str,
    price: Price | None,
    max_output_tokens: int,
    may_have_run: bool,
) -> CaseResult:
    unscored = unanswered(case)
    return CaseResult(
        case_id=case.id,
        passed=False,
        score=0.0,
        invalid_response=False,
        latency_ms=None,
        first_token_ms=None,
        finish_reason=None,
        prompt_tokens=None,
        completion_tokens=None,
        cost_micro_usd=None if may_have_run else 0,
        unresolved_max_micro_usd=(
            case_max_cost(price, case, max_output_tokens)
            if may_have_run and price is not None
            else None
        ),
        error_code=code,
        **_field_counts(unscored),
    )


def mean_completion_tokens(summary: RunSummary) -> int | None:
    """Mean output tokens per case with reported usage, rounded up."""
    if summary.usage_reported_count == 0:
        return None
    return math.ceil(summary.completion_tokens / summary.usage_reported_count)
