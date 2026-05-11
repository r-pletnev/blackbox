from __future__ import annotations

import sys
import threading
import tracemalloc
from dataclasses import dataclass

try:
    import resource
except ImportError:  # pragma: no cover - Windows fallback
    resource = None


@dataclass(frozen=True)
class MemorySnapshot:
    python_current_bytes: int | None
    python_peak_bytes: int | None
    process_max_rss_bytes: int | None


@dataclass(frozen=True)
class MemoryMeasurement:
    start: MemorySnapshot


_TRACEMALLOC_LOCK = threading.RLock()
_TRACEMALLOC_USERS = 0
_BLACKBOX_STARTED_TRACEMALLOC = False


def begin_memory_measurement() -> MemoryMeasurement:
    _acquire_tracemalloc()
    return MemoryMeasurement(start=_capture_memory_snapshot())


def finish_memory_measurement(
    measurement: MemoryMeasurement,
) -> dict[str, int | str | None]:
    try:
        return build_memory_report(measurement.start, _capture_memory_snapshot())
    finally:
        _release_tracemalloc()


def _capture_memory_snapshot() -> MemorySnapshot:
    current, peak = tracemalloc.get_traced_memory()
    return MemorySnapshot(
        python_current_bytes=current,
        python_peak_bytes=peak,
        process_max_rss_bytes=_process_max_rss_bytes(),
    )


def build_memory_report(
    start: MemorySnapshot,
    end: MemorySnapshot,
) -> dict[str, int | str | None]:
    return {
        "backend": _backend_name(start, end),
        "pythonCurrentStartBytes": start.python_current_bytes,
        "pythonCurrentEndBytes": end.python_current_bytes,
        "pythonCurrentDeltaBytes": _delta(
            start.python_current_bytes,
            end.python_current_bytes,
        ),
        "pythonPeakBytes": end.python_peak_bytes,
        "processMaxRssStartBytes": start.process_max_rss_bytes,
        "processMaxRssEndBytes": end.process_max_rss_bytes,
        "processMaxRssDeltaBytes": _delta(
            start.process_max_rss_bytes,
            end.process_max_rss_bytes,
        ),
    }


def _backend_name(start: MemorySnapshot, end: MemorySnapshot) -> str:
    backends: list[str] = []
    if start.python_current_bytes is not None and end.python_current_bytes is not None:
        backends.append("tracemalloc")
    if start.process_max_rss_bytes is not None and end.process_max_rss_bytes is not None:
        backends.append("resource")
    return "+".join(backends) or "none"


def _delta(start: int | None, end: int | None) -> int | None:
    if start is None or end is None:
        return None
    return end - start


def _process_max_rss_bytes() -> int | None:
    if resource is None:
        return None

    usage = resource.getrusage(resource.RUSAGE_SELF)
    max_rss = int(usage.ru_maxrss)
    if sys.platform == "darwin":
        return max_rss
    return max_rss * 1024


def _acquire_tracemalloc() -> None:
    global _BLACKBOX_STARTED_TRACEMALLOC, _TRACEMALLOC_USERS

    with _TRACEMALLOC_LOCK:
        if not tracemalloc.is_tracing():
            tracemalloc.start()
            _BLACKBOX_STARTED_TRACEMALLOC = True
        _TRACEMALLOC_USERS += 1


def _release_tracemalloc() -> None:
    global _BLACKBOX_STARTED_TRACEMALLOC, _TRACEMALLOC_USERS

    with _TRACEMALLOC_LOCK:
        _TRACEMALLOC_USERS = max(0, _TRACEMALLOC_USERS - 1)
        if _TRACEMALLOC_USERS == 0 and _BLACKBOX_STARTED_TRACEMALLOC:
            if tracemalloc.is_tracing():
                tracemalloc.stop()
            _BLACKBOX_STARTED_TRACEMALLOC = False
