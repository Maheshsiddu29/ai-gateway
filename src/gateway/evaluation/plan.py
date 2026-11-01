"""Preflight plans for evaluations that call real providers.

A live evaluation spends money, so it runs only after the operator has approved
an exact plan. ``build_live_plan`` checks everything that can be checked without
calling a provider and describes what would be sent: the dataset (by SHA-256),
each target with its endpoint and price version, the request count, generation
settings, and the worst-case cost. The plan's SHA-256 is the approval token: the
CLI prints the plan and refuses to dispatch unless ``--approve`` repeats that
digest. Any change to the dataset, targets, endpoints, prices, or limits changes
the digest and needs a new approval.

Worst-case cost is the estimated input of every case plus the full output
allowance, at the configured prices. It bounds what the gateway's accounting
would charge, not what a provider invoices.
"""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

import httpx
from pydantic import SecretStr

from gateway.admission.tokens import estimate_input_tokens
from gateway.core.config import GatewayConfig
from gateway.cost.pricing import PriceCatalog
from gateway.errors import GatewayError
from gateway.evaluation.dataset import Dataset
from gateway.evaluation.runner import case_max_cost
from gateway.providers.base import ProviderRequest
from gateway.providers.factory import build_provider
from gateway.routing.config import Capability, ModelCapabilities


class PlanError(Exception):
    """The evaluation cannot be approved as requested."""


class OfflineTransport(httpx.AsyncBaseTransport):
    """For adapters built only to validate requests, which performs no I/O."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise RuntimeError("an adapter built for validation must not call its provider")


@dataclass(frozen=True)
class TargetPlan:
    provider: str
    model: str
    base_url: str
    pricing_version: str
    input_per_million_usd: str
    output_per_million_usd: str
    requests: int
    worst_case_micro_usd: int


@dataclass(frozen=True)
class LivePlan:
    dataset: str
    dataset_version: str
    partition: str
    dataset_sha256: str
    cases: int
    max_tokens: int
    stream: bool
    timeout_seconds: float
    concurrency: int
    max_requests: int
    max_cost_micro_usd: int
    targets: tuple[TargetPlan, ...]

    @property
    def requests(self) -> int:
        return sum(target.requests for target in self.targets)

    @property
    def worst_case_micro_usd(self) -> int:
        return sum(target.worst_case_micro_usd for target in self.targets)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    def sha256(self) -> str:
        canonical = json.dumps(self.to_json(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def describe(self) -> str:
        lines = [
            "Live evaluation plan. No provider has been called.",
            f"  dataset   {self.dataset} v{self.dataset_version} ({self.partition}),"
            f" {self.cases} cases, sha256 {self.dataset_sha256}",
            f"  settings  max_tokens {self.max_tokens},"
            f" {'streaming' if self.stream else 'non-streaming'},"
            f" timeout {self.timeout_seconds:g} s, concurrency {self.concurrency},"
            " one attempt per case (no retries)",
        ]
        for target in self.targets:
            lines.append(
                f"  target    {target.provider}/{target.model} at {target.base_url},"
                f" price {target.pricing_version} (input {target.input_per_million_usd},"
                f" output {target.output_per_million_usd} USD per million tokens):"
                f" {target.requests} requests, worst case {target.worst_case_micro_usd}"
                " micro-USD"
            )
        lines.append(
            f"  total     {self.requests} requests (limit {self.max_requests}), worst case"
            f" {self.worst_case_micro_usd} micro-USD (ceiling {self.max_cost_micro_usd})"
        )
        return "\n".join(lines)


def build_live_plan(
    config: GatewayConfig,
    dataset: Dataset,
    targets: list[tuple[str, str]],
    *,
    max_tokens: int,
    stream: bool,
    timeout_seconds: float,
    concurrency: int,
    max_requests: int,
    max_cost_micro_usd: int,
    now: datetime,
) -> LivePlan:
    """Check a live evaluation and describe it, or raise PlanError."""
    requests = len(dataset.cases) * len(targets)
    if requests > max_requests:
        raise PlanError(f"the run needs {requests} requests, above --max-requests {max_requests}")
    catalog = PriceCatalog(config.cost.pricing)
    registry = config.routing.registry()
    plans = []
    for provider, model in targets:
        price = catalog.find(provider, model, now)
        if price is None:
            # Without a price the worst case cannot be bounded.
            raise PlanError(f"no effective price for {provider}/{model} in [[cost.pricing]]")
        _check_target(config, dataset, provider, model, max_tokens, stream, registry)
        plans.append(
            TargetPlan(
                provider=provider,
                model=model,
                base_url=str(config.providers[provider].base_url),
                pricing_version=price.version,
                input_per_million_usd=str(price.input_per_million_usd),
                output_per_million_usd=str(price.output_per_million_usd),
                requests=len(dataset.cases),
                worst_case_micro_usd=sum(
                    case_max_cost(price, case, max_tokens) for case in dataset.cases
                ),
            )
        )
    plan = LivePlan(
        dataset=dataset.name,
        dataset_version=dataset.version,
        partition=dataset.partition.value,
        dataset_sha256=dataset.sha256,
        cases=len(dataset.cases),
        max_tokens=max_tokens,
        stream=stream,
        timeout_seconds=timeout_seconds,
        concurrency=concurrency,
        max_requests=max_requests,
        max_cost_micro_usd=max_cost_micro_usd,
        targets=tuple(plans),
    )
    if plan.worst_case_micro_usd > max_cost_micro_usd:
        raise PlanError(
            f"worst-case estimated cost {plan.worst_case_micro_usd} micro-USD exceeds"
            f" --max-cost-micro-usd {max_cost_micro_usd}"
        )
    return plan


def _check_target(
    config: GatewayConfig,
    dataset: Dataset,
    provider: str,
    model: str,
    max_tokens: int,
    stream: bool,
    registry: dict[tuple[str, str], ModelCapabilities],
) -> None:
    """Reject a target whose documented limits or adapter would refuse the cases."""
    entry = registry.get((provider, model))
    if entry is None:
        raise PlanError(
            f"{provider}/{model} has no [[routing.models]] entry, so its limits cannot be checked"
        )
    if max_tokens > entry.max_output_tokens:
        raise PlanError(
            f"--max-tokens {max_tokens} exceeds the output limit of {entry.max_output_tokens}"
            f" for {provider}/{model}"
        )
    needed = {Capability.STREAMING, Capability.STREAM_USAGE} if stream else set()
    missing = needed - entry.capabilities
    if missing:
        raise PlanError(f"{provider}/{model} does not list {sorted(missing)} capabilities")
    adapter = build_provider(
        provider, config.providers[provider], SecretStr("validation-only"), OfflineTransport()
    )
    for case in dataset.cases:
        if estimate_input_tokens(case.messages) + max_tokens > entry.context_window_tokens:
            raise PlanError(f"case {case.id} does not fit the context window of {provider}/{model}")
        request = ProviderRequest(
            model=model,
            messages=tuple(case.messages),
            max_tokens=max_tokens,
            temperature=None,
            top_p=None,
            stop=(),
            include_usage=stream,
        )
        try:
            adapter.validate(request)
        except GatewayError as exc:
            raise PlanError(f"{provider}/{model} rejects case {case.id}: {exc.message}") from None
