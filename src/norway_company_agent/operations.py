from __future__ import annotations

import math
import resource
import sys
from collections import Counter
from typing import Iterable


def percentile(values: Iterable[float], quantile: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if not 0 <= quantile <= 1:
        raise ValueError("quantile must be between zero and one")
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def latency_summary(values: Iterable[float]) -> dict[str, float | int | None]:
    measured = list(values)
    return {
        "n": len(measured),
        "p50_ms": round(percentile(measured, 0.50), 3) if measured else None,
        "p95_ms": round(percentile(measured, 0.95), 3) if measured else None,
        "max_ms": round(max(measured), 3) if measured else None,
    }


def domain_request_summary(counts: Counter[str]) -> dict[str, float | int | None]:
    values = list(counts.values())
    return {
        "domains": len(values),
        "p50_requests": percentile(values, 0.50),
        "p95_requests": percentile(values, 0.95),
        "max_requests": max(values) if values else None,
    }


def peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes; Linux and most BSD-derived CI images report KiB.
    return int(value if sys.platform == "darwin" else value * 1024)
