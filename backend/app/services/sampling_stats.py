"""Defensible-sampling statistics (P3-2). Pure, dependency-free.

Wilson score intervals are the standard for proportion estimates in
e-discovery validation (well-behaved near 0 and 1, unlike the normal
approximation); sample sizes use the normal approximation with finite
population correction — the formulas opposing experts will check.
"""

from __future__ import annotations

import math

Z = {90: 1.6449, 95: 1.9599, 99: 2.5758}


def _z(confidence: int) -> float:
    if confidence not in Z:
        raise ValueError(f"confidence must be one of {sorted(Z)}")
    return Z[confidence]


def sample_size(population: int, confidence: int = 95,
                margin: float = 0.05, expected_rate: float = 0.5) -> int:
    """Documents to draw to estimate a proportion within ±margin.

    Normal approximation with finite population correction; expected_rate
    0.5 is the conservative default (maximizes n).
    """
    if population < 1:
        raise ValueError("population must be >= 1")
    if not (0 < margin < 1):
        raise ValueError("margin must be in (0, 1)")
    if not (0 < expected_rate < 1):
        raise ValueError("expected_rate must be in (0, 1)")
    z = _z(confidence)
    p = expected_rate
    n0 = (z * z * p * (1 - p)) / (margin * margin)
    n = n0 / (1 + (n0 - 1) / population)
    return min(population, math.ceil(n))


def wilson_ci(positives: int, n: int, confidence: int = 95) -> tuple[float, float, float]:
    """(rate, low, high) Wilson score interval; (0, 0, 0) when n == 0."""
    if n == 0:
        return (0.0, 0.0, 0.0)
    if not (0 <= positives <= n):
        raise ValueError("positives must be in [0, n]")
    z = _z(confidence)
    phat = positives / n
    z2 = z * z
    denom = 1 + z2 / n
    center = (phat + z2 / (2 * n)) / denom
    half = (z * math.sqrt(phat * (1 - phat) / n + z2 / (4 * n * n))) / denom
    return (phat, max(0.0, center - half), min(1.0, center + half))


def acceptance(defects: int, n: int, tolerable_rate: float,
               confidence: int = 95) -> dict:
    """Accept the lot iff the upper Wilson bound of the observed defect
    rate is at or below the tolerable rate."""
    if not (0 < tolerable_rate < 1):
        raise ValueError("tolerable_rate must be in (0, 1)")
    rate, _low, upper = wilson_ci(defects, n, confidence)
    return {
        "accept": n > 0 and upper <= tolerable_rate,
        "rate": rate,
        "upper_bound": upper,
        "tolerable_rate": tolerable_rate,
        "defects": defects,
        "n": n,
        "confidence": confidence,
    }
