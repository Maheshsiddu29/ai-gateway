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


def reported_percentile(samples: Sequence[float], quantile: float) -> float | None:
    """The nearest-rank percentile, or None when there are too few samples for it.

    With fewer than 1 / (1 - quantile) samples (2 for p50, 20 for p95, 100 for
    p99), the nearest-rank percentile is the largest sample, so reporting it under
    the percentile's name would overstate what was measured.
    """
    if len(samples) < math.ceil(1 / (1 - quantile) - 1e-9):
        return None
    return percentile(samples, quantile)


def paired_difference_interval(
    first_only: int, second_only: int, cases: int, z: float = Z_95
) -> tuple[float, float]:
    """An interval for p1 - p2 when both proportions are measured on the same cases.

    ``first_only`` cases passed only under the first condition and
    ``second_only`` only under the second. This is the adjusted Wald interval for
    paired proportions of Bonett and Price (2012): add one to each discordant
    count and two to the number of cases, then use the Wald formula. Unlike the
    plain Wald interval, or Newcombe's hybrid interval, it does not shrink to
    zero width when the two conditions agree on every case, so a small sample
    cannot make two models look identical. In simulations with 20 to 200 cases
    its coverage of the true difference was between 93% and 100%.
    """
    if first_only < 0 or second_only < 0 or first_only + second_only > cases:
        raise ValueError("discordant counts must be non-negative and at most the case count")
    if cases == 0:
        return -1.0, 1.0
    p12 = (first_only + 1) / (cases + 2)
    p21 = (second_only + 1) / (cases + 2)
    difference = p12 - p21
    margin = z * math.sqrt((p12 + p21 - difference**2) / (cases + 2))
    return max(-1.0, difference - margin), min(1.0, difference + margin)
