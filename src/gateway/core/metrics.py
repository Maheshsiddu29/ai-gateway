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
