"""Small statistics helpers (standard library only)."""

from __future__ import annotations

import random
from collections import Counter
from typing import Optional, Sequence


def mean(xs: Sequence[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def sample_variance(xs: Sequence[float]) -> Optional[float]:
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return sum((x - m) ** 2 for x in xs) / (len(xs) - 1)


def median(xs: Sequence[float]) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def bootstrap_ci(
    values: Sequence[float], n_boot: int = 2000, seed: int = 12345, alpha: float = 0.05
) -> Optional[tuple[float, float]]:
    """Percentile bootstrap CI of the mean, resampling the items (questions) with replacement."""
    n = len(values)
    if n == 0:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(n_boot):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    lo = means[int((alpha / 2) * n_boot)]
    hi = means[min(n_boot - 1, int((1 - alpha / 2) * n_boot))]
    return lo, hi


def modal_share(signatures: Sequence[str]) -> Optional[float]:
    """Share of runs that agree with the most common result (None when fewer than 2 runs)."""
    if len(signatures) < 2:
        return None
    return Counter(signatures).most_common(1)[0][1] / len(signatures)
