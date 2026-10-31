"""Summary statistics for evaluation results."""

import math
from collections.abc import Sequence

# Two-sided 95% normal quantile.
Z_95 = 1.959963984540054


def wilson_interval(successes: int, trials: int, z: float = Z_95) -> tuple[float, float]:
    """The Wilson score interval for a binomial proportion.

    Unlike the normal approximation it stays within [0, 1] and is informative for
    small samples: 10 successes out of 10 give a lower bound of about 0.72, not 1.
    """
    if trials <= 0:
        return 0.0, 1.0
    if not 0 <= successes <= trials:
        raise ValueError("successes must be between 0 and trials")
    p = successes / trials
    z2 = z * z
    centre = p + z2 / (2 * trials)
    margin = z * math.sqrt(p * (1 - p) / trials + z2 / (4 * trials * trials))
    denominator = 1 + z2 / trials
    return max(0.0, (centre - margin) / denominator), min(1.0, (centre + margin) / denominator)


def percentile(samples: Sequence[float], quantile: float) -> float:
    """Nearest-rank percentile: the smallest sample with at least ``quantile`` of
    the samples at or below it. Always one of the samples, never interpolated."""
    if not samples:
        raise ValueError("percentile of no samples")
    if not 0 < quantile <= 1:
        raise ValueError("quantile must be in (0, 1]")
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]
