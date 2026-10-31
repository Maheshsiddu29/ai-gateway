"""Prometheus metrics.

Each application instance owns its own registry, exposed at ``GET /metrics``.
Labels never include API keys, principal IDs, request IDs, or model output, which
keeps cardinality bounded by configuration (providers, aliases, routes).
"""

from prometheus_client import CollectorRegistry, Counter, Histogram

# Covers fast admission decisions and long completions alike.
_LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.http_requests = Counter(
            "gateway_http_requests",
            "HTTP requests by route and status code.",
            ["route", "status"],
            registry=self.registry,
        )
        self.http_request_duration = Histogram(
            "gateway_http_request_duration_seconds",
            "Time from receiving a request until its response completed.",
            ["route"],
            buckets=_LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.deadline_exceeded = Counter(
            "gateway_deadline_exceeded",
            "Completions that failed because the request deadline expired.",
            ["stream"],
            registry=self.registry,
        )
        self.admission_decisions = Counter(
            "gateway_admission_decisions",
            "Admission decisions by outcome.",
            ["outcome"],
            registry=self.registry,
        )
        self.admission_duration = Histogram(
            "gateway_admission_duration_seconds",
            "Time taken to obtain an admission decision from Redis.",
            buckets=_LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.reservations_settled = Counter(
            "gateway_reservations_settled",
            "Reservation settlements by outcome; 'failed' leaves the reservation to expire.",
            ["outcome"],
            registry=self.registry,
        )
        self.reserved_tokens = Counter(
            "gateway_reserved_tokens",
            "Tokens reserved at admission (estimated input plus output allowance).",
            registry=self.registry,
        )
        self.charged_tokens = Counter(
            "gateway_charged_tokens",
            "Tokens charged at settlement, by whether usage was reported by the provider.",
            ["usage"],
            registry=self.registry,
        )
        self.redis_errors = Counter(
            "gateway_redis_errors",
            "Failed Redis operations.",
            ["operation"],
            registry=self.registry,
        )
        self.provider_attempts = Counter(
            "gateway_provider_attempts",
            "Provider attempts by provider and outcome (success, the error type, or"
            " circuit_open when the breaker refused the attempt).",
            ["provider", "outcome"],
            registry=self.registry,
        )
        self.provider_attempt_duration = Histogram(
            "gateway_provider_attempt_duration_seconds",
            "Provider attempt duration through completion or failure, including stream delivery.",
            ["provider"],
            buckets=_LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.provider_retries = Counter(
            "gateway_provider_retries",
            "Attempts retried after a transient provider failure.",
            ["provider"],
            registry=self.registry,
        )
        self.circuit_transitions = Counter(
            "gateway_circuit_breaker_transitions",
            "Circuit breaker state changes in this process.",
            ["provider", "state"],
            registry=self.registry,
        )
        self.provider_fallbacks = Counter(
            "gateway_provider_fallbacks",
            "Requests moved from one provider to the next configured fallback.",
            ["from_provider", "to_provider"],
            registry=self.registry,
        )
