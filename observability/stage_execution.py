"""
Shared Lambda stage-handler scaffold (DL-OPS-05, DL-OPS-07, REU-01).

Replaces the bind-contextvars / try / except / finally boilerplate repeated across
every stage entrypoint with one template-method lifecycle that cannot forget the
`finally` clear, the `finally` metrics flush, or the failure record on a hard kill.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Final, Literal

import structlog

from contracts.platform_metrics import PlatformMetric
from observability.lambda_utils import configure_xray
from observability.metric_recorder import platform_metric_recorder
from observability.metrics_emitter import CloudWatchMetricsEmitter
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# Milliseconds before the Lambda runtime kills the process at which the watchdog
# writes the guaranteed failure record (DL-OPS-05).
DEFAULT_HARD_KILL_MARGIN_MS: Final[int] = 5_000


def derive_correlation_id(run_id: str, replay_of_run_id: str | None = None) -> str:
    """One logical operation, one id — a replay inherits the original run's id (DL-OPS-07)."""
    return replay_of_run_id or run_id


@dataclass(frozen=True)
class StageIdentity:
    """The dimensions every stage log line, metric, and audit record carries."""

    tenant_code: str
    source_id: str
    entity_id: str
    run_id: str
    environment: str
    stage: str
    correlation_id: str = ""
    connection_id: str | None = None
    scope_unit_id: str | None = None

    def bound_context(self) -> dict[str, str]:
        context = {
            "run_id": self.run_id,
            "correlation_id": self.correlation_id or self.run_id,
            "tenant_code": self.tenant_code,
            "source_id": self.source_id,
            "entity_id": self.entity_id,
            "stage": self.stage,
        }
        if self.connection_id:
            context["connection_id"] = self.connection_id
        if self.scope_unit_id:
            context["scope_unit_id"] = self.scope_unit_id
        return context

    def metric_dimensions(self) -> dict[str, str]:
        dimensions = {
            "SourceId": self.source_id,
            "EntityId": self.entity_id,
            "Stage": self.stage,
        }
        if self.connection_id:
            dimensions["ConnectionId"] = self.connection_id
        return dimensions


@dataclass
class StageExecution:
    """
    Template-method lifecycle for a pipeline stage Lambda.

    Guarantees, in order, whatever the body does: contextvars bound then cleared,
    metrics flushed, stage duration emitted, and a failure record written on both
    an exception and a hard Lambda kill.
    """

    identity: StageIdentity
    region_name: str
    lambda_context: Any = None
    metrics: CloudWatchMetricsEmitter | None = None
    on_failure_record: Callable[[str, str], None] | None = None
    hard_kill_margin_ms: int = DEFAULT_HARD_KILL_MARGIN_MS
    _start_ms: float = field(default=0.0, init=False)
    _watchdog: threading.Timer | None = field(default=None, init=False)
    _failure_recorded: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.metrics is None:
            self.metrics = CloudWatchMetricsEmitter(region_name=self.region_name)
        self.metrics.set_tenant_context(self.identity.tenant_code)

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self) -> StageExecution:
        self._start_ms = time.monotonic() * 1000
        structlog.contextvars.bind_contextvars(**self.identity.bound_context())
        configure_xray(
            tenant_code=self.identity.tenant_code,
            source_id=self.identity.source_id,
            entity_id=self.identity.entity_id,
            run_id=self.identity.run_id,
        )
        self._arm_hard_kill_watchdog()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> Literal[False]:
        self._disarm_hard_kill_watchdog()
        try:
            if exc is not None:
                self._record_failure(type(exc).__name__, str(exc))
            self.emit(
                PlatformMetric.STAGE_DURATION_MS,
                time.monotonic() * 1000 - self._start_ms,
            )
        finally:
            self.flush()
            structlog.contextvars.clear_contextvars()
        return False

    # ── Metric surface ────────────────────────────────────────────────────────

    @property
    def _emitter(self) -> CloudWatchMetricsEmitter:
        """__post_init__ always sets `metrics`; this spares every caller the None narrowing."""
        if self.metrics is None:  # pragma: no cover — unreachable once constructed
            self.metrics = CloudWatchMetricsEmitter(region_name=self.region_name)
        return self.metrics

    def emit(
        self,
        metric: PlatformMetric,
        value: float = 1.0,
        extra_dimensions: dict[str, str] | None = None,
    ) -> None:
        """Buffer a catalogued metric with this stage's dimensions already applied."""
        dimensions = {**self.identity.metric_dimensions(), **(extra_dimensions or {})}
        self._emitter.emit_metric(
            metric,
            value,
            environment=self.identity.environment,
            dimensions=dimensions,
        )

    def flush(self) -> None:
        """
        Drain the process recorder and deliver everything; never raises.

        Draining here is what makes a metric recorded deep inside a domain module arrive:
        the module has no CloudWatch client, and the `finally` in `__exit__` is the only
        place guaranteed to run.
        """
        try:
            self._drain_recorder()
            self._emitter.flush()
        except Exception as exc:
            _logger.warning("stage_metrics_flush_failed", error=str(exc))

    def _drain_recorder(self) -> None:
        for point in platform_metric_recorder.drain():
            self._emitter.emit_metric(
                point.metric,
                point.value,
                environment=self.identity.environment,
                dimensions={
                    **self.identity.metric_dimensions(),
                    **point.dimension_map(),
                },
            )

    # ── Failure recording ─────────────────────────────────────────────────────

    def _record_failure(self, error_code: str, error_message: str) -> None:
        if self._failure_recorded:
            return
        self._failure_recorded = True
        self.emit(PlatformMetric.RECORDS_FAILED, 1.0)
        _logger.error(
            "stage_failed",
            stage=self.identity.stage,
            error_code=error_code,
            error=error_message,
        )
        if self.on_failure_record is None:
            return
        try:
            self.on_failure_record(error_code, error_message)
        except Exception as exc:
            _logger.error("stage_failure_record_write_failed", error=str(exc))

    def _arm_hard_kill_watchdog(self) -> None:
        remaining_ms = _remaining_time_ms(self.lambda_context)
        if remaining_ms is None:
            return
        delay_s = (remaining_ms - self.hard_kill_margin_ms) / 1000.0
        if delay_s <= 0:
            return
        timer = threading.Timer(delay_s, self._on_hard_kill_imminent)
        timer.daemon = True
        timer.start()
        self._watchdog = timer

    def _disarm_hard_kill_watchdog(self) -> None:
        if self._watchdog is not None:
            self._watchdog.cancel()
            self._watchdog = None

    def _on_hard_kill_imminent(self) -> None:
        # Runs on the watchdog thread with only milliseconds left — write the
        # failure record and flush, then let the runtime kill the process.
        self._record_failure(
            "lambda_hard_timeout",
            f"Stage {self.identity.stage} did not complete before the Lambda timeout.",
        )
        self.flush()


def _remaining_time_ms(lambda_context: Any) -> int | None:
    if lambda_context is None or not hasattr(lambda_context, "get_remaining_time_in_millis"):
        return None
    try:
        return int(lambda_context.get_remaining_time_in_millis())
    except Exception:
        return None


@contextmanager
def stage_execution(
    identity: StageIdentity,
    region_name: str,
    lambda_context: Any = None,
    on_failure_record: Callable[[str, str], None] | None = None,
    metrics: CloudWatchMetricsEmitter | None = None,
) -> Iterator[StageExecution]:
    """
    Convenience wrapper so a handler body reads as one `with` statement.

    `metrics` lets a handler that already constructed an emitter hand it over rather than ending up
    with two: two emitters means two flushes, and points buffered on the handler's own instance
    would never be delivered by the stage's `finally`.
    """
    execution = StageExecution(
        metrics=metrics,
        identity=identity,
        region_name=region_name,
        lambda_context=lambda_context,
        on_failure_record=on_failure_record,
    )
    with execution:
        yield execution
