"""Operational surface: per-person rate limit and Prometheus metrics. No dependencies.

Both are per process. Behind several workers, the limit is per worker and each worker
reports its own metrics; put a shared limit at the gateway if you need an exact one.
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict

_BUCKETS = (0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)


def rate_limit_per_minute() -> int:
    """SECURE_QUERY_RATE_LIMIT_PER_MINUTE; default 60 in production, off (0) elsewhere."""
    raw = (os.environ.get("SECURE_QUERY_RATE_LIMIT_PER_MINUTE") or "").strip()
    if raw:
        return max(0, int(raw))
    return 60 if (os.environ.get("SECURE_QUERY_ENV") or "").strip().lower() == "production" else 0


class RateLimiter:
    """Token bucket per key: `per_minute` requests, refilled continuously."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last refill time)

    def retry_after(self, key: str, per_minute: int, *, now: float | None = None) -> float:
        """0 when the request may proceed; otherwise the seconds until it may."""
        if per_minute <= 0:
            return 0.0
        now = time.monotonic() if now is None else now
        rate = per_minute / 60.0
        with self._lock:
            tokens, last = self._buckets.get(key, (float(per_minute), now))
            tokens = min(float(per_minute), tokens + (now - last) * rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                return 0.0
            self._buckets[key] = (tokens, now)
            return (1.0 - tokens) / rate

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


class Metrics:
    """Counters and one latency histogram, rendered in the Prometheus text format."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._durations: dict[str, list[float]] = {}  # route -> [bucket counts..., +Inf, sum]

    def count(self, name: str, **labels: str) -> None:
        with self._lock:
            self._counters[(name, tuple(sorted(labels.items())))] += 1

    def observe(self, route: str, seconds: float) -> None:
        with self._lock:
            slots = self._durations.setdefault(route, [0.0] * (len(_BUCKETS) + 2))
            for i, bound in enumerate(_BUCKETS):
                if seconds <= bound:
                    slots[i] += 1
            slots[-2] += 1
            slots[-1] += seconds

    def render(self) -> str:
        def labelled(labels: tuple[tuple[str, str], ...] | dict[str, str]) -> str:
            pairs = labels.items() if isinstance(labels, dict) else labels
            inner = ",".join(f'{k}="{str(v).replace(chr(34), "")}"' for k, v in pairs)
            return f"{{{inner}}}" if inner else ""

        with self._lock:
            lines: list[str] = []
            for name in sorted({name for name, _ in self._counters}):
                lines.append(f"# TYPE {name} counter")
                for (n, labels), value in sorted(self._counters.items()):
                    if n == name:
                        lines.append(f"{name}{labelled(labels)} {value:g}")
            if self._durations:
                lines.append("# TYPE secure_query_request_seconds histogram")
            for route, slots in sorted(self._durations.items()):
                for bound, value in zip((*_BUCKETS, "+Inf"), slots[:-1]):
                    lines.append(
                        f"secure_query_request_seconds_bucket{labelled({'route': route, 'le': str(bound)})} {value:g}"
                    )
                lines.append(f"secure_query_request_seconds_count{labelled({'route': route})} {slots[-2]:g}")
                lines.append(f"secure_query_request_seconds_sum{labelled({'route': route})} {slots[-1]:.6f}")
        return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._durations.clear()


limiter = RateLimiter()
metrics = Metrics()
