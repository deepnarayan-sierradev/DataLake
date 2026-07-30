"""
Process-local metric recorder (DL-OPS-05).

Domain modules record metrics at the point the event occurs without holding a CloudWatch
client or knowing which Lambda they are running in. `StageExecution.flush()` drains the
recorder, so a metric recorded anywhere in a stage is delivered in the `finally` block —
which is the guarantee the missing-flush bug broke.

Recording never raises and never blocks: a metric is telemetry, and telemetry must not change
whether a pipeline run succeeds.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Final

from contracts.platform_metrics import PlatformMetric

MAX_BUFFERED_POINTS: Final[int] = 5_000


@dataclass(frozen=True)
class RecordedMetric:
    """One recorded data point, awaiting delivery."""

    metric: PlatformMetric
    value: float
    dimensions: tuple[tuple[str, str], ...] = ()

    def dimension_map(self) -> dict[str, str]:
        return dict(self.dimensions)


@dataclass
class MetricRecorder:
    """Thread-safe buffer; one per process, drained by the stage lifecycle."""

    _points: list[RecordedMetric] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    dropped: int = 0

    def record(self, metric: PlatformMetric, value: float = 1.0, **dimensions: str) -> None:
        with self._lock:
            if len(self._points) >= MAX_BUFFERED_POINTS:
                self.dropped += 1
                return
            self._points.append(
                RecordedMetric(
                    metric=metric,
                    value=float(value),
                    dimensions=tuple(sorted(dimensions.items())),
                )
            )

    def drain(self) -> list[RecordedMetric]:
        """Return and clear the buffer."""
        with self._lock:
            points = self._points
            self._points = []
            return points

    def snapshot(self) -> list[RecordedMetric]:
        """Read without clearing; for assertions in tests."""
        with self._lock:
            return list(self._points)

    def clear(self) -> None:
        with self._lock:
            self._points = []
            self.dropped = 0

    @property
    def buffered(self) -> int:
        with self._lock:
            return len(self._points)


platform_metric_recorder: Final[MetricRecorder] = MetricRecorder()


def record_platform_metric(metric: PlatformMetric, value: float = 1.0, **dimensions: str) -> None:
    """Record one data point; the single entry point every domain module calls."""
    platform_metric_recorder.record(metric, value, **dimensions)
