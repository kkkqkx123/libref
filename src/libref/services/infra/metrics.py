"""In-process request and governance counters for the read-only metrics exit.

Counters live for the process lifetime only; they are never persisted and never
contain keys, query bodies, or document content. Reads snapshot and reset nothing.
"""

from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.Lock()
_counters: dict[str, int] = {}
_started = time.time()


def incr(name: str, amount: int = 1) -> None:
    """Increment one named counter; labels are embedded in the name by callers."""
    with _lock:
        _counters[name] = _counters.get(name, 0) + int(amount)


def snapshot() -> dict[str, Any]:
    """Read-only view of all counters plus basic latency rollup inputs."""
    with _lock:
        data = dict(_counters)
    durations = _duration_ms.copy()
    data["uptime_seconds"] = int(time.time() - _started)
    if durations:
        ordered = sorted(durations)
        data["duration_ms_p50"] = ordered[len(ordered) // 2]
        data["duration_ms_p95"] = ordered[
            min(len(ordered) - 1, int(len(ordered) * 0.95))
        ]
    return data


_duration_ms: list[int] = []


def record_duration(ms: int) -> None:
    """Keep a bounded recent-duration sample for percentile output."""
    with _lock:
        _duration_ms.append(int(ms))
        if len(_duration_ms) > 10_000:
            del _duration_ms[: len(_duration_ms) - 10_000]
