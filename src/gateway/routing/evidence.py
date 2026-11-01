"""Evaluation evidence for quality-constrained routing.

The request path never queries PostgreSQL for evidence. ``EvidenceCache`` holds
an immutable snapshot of the latest qualifying run per provider, model, dataset,
and dataset version, and a background task replaces the whole snapshot
periodically. A decision reads the snapshot reference once, so it never mixes
two snapshots.

A run qualifies when it is not simulated, used a development (not holdout)
dataset partition, ran every case, and was scored with the current evaluator
version for its method. Freshness and sample-size rules are applied per
decision (see ``gateway.routing.router``), so a snapshot that cannot be refreshed
while the database is unavailable does not keep evidence valid for longer.
"""

import hashlib
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.core.metrics import Metrics
from gateway.evaluation.scoring import EVALUATOR_VERSIONS
from gateway.evaluation.stats import wilson_interval

logger = logging.getLogger(__name__)

# provider, model, dataset, dataset version
type EvidenceKey = tuple[str, str, str, str]

# Bounds the snapshot's memory; far above any realistic number of evaluated
# provider/model/dataset combinations.
MAX_EVIDENCE_ROWS = 10_000


@dataclass(frozen=True)
class Evidence:
    run_id: uuid.UUID
    completed_at: datetime
    case_count: int
    passed_count: int
    latency_samples: int
    latency_p95_ms: float | None
    # Mean output tokens per case with reported usage, rounded up.
    expected_output_tokens: int | None
    # The max_tokens each case was sent with.
    max_output_tokens: int

    @property
    def success_rate_low(self) -> float:
        """Lower bound of the 95% Wilson interval of the pass rate."""
        return wilson_interval(self.passed_count, self.case_count)[0]


@dataclass(frozen=True)
class EvidenceSnapshot:
    runs: Mapping[EvidenceKey, Evidence] = field(default_factory=dict)
    # Identifies the set of runs, for reconstructing decisions from logs.
    version: str = "empty"

    @classmethod
    def of(cls, runs: Mapping[EvidenceKey, Evidence]) -> "EvidenceSnapshot":
        ids = ",".join(sorted(str(evidence.run_id) for evidence in runs.values()))
        version = hashlib.sha256(ids.encode()).hexdigest()[:16] if runs else "empty"
        return cls(dict(runs), version)


async def load_evidence(engine: AsyncEngine) -> EvidenceSnapshot:
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                text("""SELECT DISTINCT ON
                (provider, model, dataset, dataset_version, method, evaluator_version)
                run_id, provider, model, dataset, dataset_version, method, evaluator_version,
                completed_at, case_count, passed_count, latency_samples, latency_p95_ms,
                usage_reported_count, completion_tokens, max_output_tokens
                FROM evaluation_runs
                WHERE NOT simulated AND partition = 'development' AND stop_reason IS NULL
                ORDER BY provider, model, dataset, dataset_version, method, evaluator_version,
                completed_at DESC, run_id
                LIMIT :limit"""),
                {"limit": MAX_EVIDENCE_ROWS},
            )
        ).mappings()
        runs: dict[EvidenceKey, Evidence] = {}
        current = {method.value: version for method, version in EVALUATOR_VERSIONS.items()}
        for row in rows:
            if current.get(row["method"]) != row["evaluator_version"]:
                continue
            reported = row["usage_reported_count"]
            runs[(row["provider"], row["model"], row["dataset"], row["dataset_version"])] = (
                Evidence(
                    run_id=row["run_id"],
                    completed_at=row["completed_at"],
                    case_count=row["case_count"],
                    passed_count=row["passed_count"],
                    latency_samples=row["latency_samples"],
                    latency_p95_ms=row["latency_p95_ms"],
                    # Integer ceiling division.
                    expected_output_tokens=(
                        (row["completion_tokens"] + reported - 1) // reported if reported else None
                    ),
                    max_output_tokens=row["max_output_tokens"],
                )
            )
    return EvidenceSnapshot.of(runs)


class EvidenceCache:
    def __init__(self, engine: AsyncEngine, metrics: Metrics | None = None) -> None:
        self._engine = engine
        self._metrics = metrics
        self.current = EvidenceSnapshot()

    async def refresh(self) -> bool:
        """Replace the snapshot; on failure keep the previous one and return False."""
        try:
            snapshot = await load_evidence(self._engine)
        except (SQLAlchemyError, OSError) as exc:
            logger.error("routing evidence refresh failed: %s", type(exc).__name__)
            self._count("failed")
            return False
        self.current = snapshot
        self._count("loaded")
        return True

    def _count(self, outcome: str) -> None:
        if self._metrics is not None:
            self._metrics.routing_evidence_refreshes.labels(outcome).inc()
