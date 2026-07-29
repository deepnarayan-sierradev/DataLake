"""
Tests for the shared stage scaffold (DL-OPS-05, DL-OPS-07).

The bug this module exists to prevent is a missing `finally`: stale contextvars leaking into the
next warm invocation, and buffered metrics never delivered. Every guarantee in the docstring is
asserted here, including on the failure path.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
import structlog

from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import platform_metric_recorder, record_platform_metric
from observability.stage_execution import (
    StageExecution,
    StageIdentity,
    derive_correlation_id,
    stage_execution,
)

_IDENTITY = StageIdentity(
    tenant_code="demo",
    source_id="salesforce",
    entity_id="salesforce-account",
    run_id="run-0001",
    environment="dev",
    stage="extraction",
)


class _RecordingEmitter:
    """Stands in for CloudWatchMetricsEmitter; records rather than calling AWS."""

    def __init__(self) -> None:
        self.emitted: list[tuple[PlatformMetric, float, dict[str, str]]] = []
        self.flush_count = 0
        self.tenant_code: str | None = None
        self.raise_on_flush = False

    def set_tenant_context(self, tenant_code: str) -> None:
        self.tenant_code = tenant_code

    def emit_metric(
        self,
        metric: PlatformMetric,
        value: float = 1.0,
        *,
        environment: str | None = None,
        dimensions: dict[str, str] | None = None,
    ) -> None:
        self.emitted.append((metric, value, dict(dimensions or {})))

    def flush(self) -> None:
        self.flush_count += 1
        if self.raise_on_flush:
            raise RuntimeError("CloudWatch unavailable")

    def metrics_of(self, metric: PlatformMetric) -> list[tuple[PlatformMetric, float, dict]]:
        return [point for point in self.emitted if point[0] is metric]


class _FakeLambdaContext:
    def __init__(self, remaining_ms: int) -> None:
        self._remaining_ms = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self._remaining_ms


def _make_execution(**overrides: Any) -> tuple[StageExecution, _RecordingEmitter]:
    emitter = _RecordingEmitter()
    kwargs: dict[str, Any] = {
        "identity": _IDENTITY,
        "region_name": "us-east-1",
        "metrics": emitter,
    }
    kwargs.update(overrides)
    return StageExecution(**kwargs), emitter


@pytest.fixture(autouse=True)
def _clean_process_state() -> Any:
    platform_metric_recorder.clear()
    structlog.contextvars.clear_contextvars()
    yield
    platform_metric_recorder.clear()
    structlog.contextvars.clear_contextvars()


class TestCorrelationId:
    def test_plain_run_uses_its_own_run_id(self) -> None:
        assert derive_correlation_id("run-1") == "run-1"

    def test_replay_inherits_the_original_run_id(self) -> None:
        # DL-OPS-07: a replay is the same logical operation, so one id spans both.
        assert derive_correlation_id("run-2", replay_of_run_id="run-1") == "run-1"

    def test_empty_replay_id_falls_back_to_the_run_id(self) -> None:
        assert derive_correlation_id("run-2", replay_of_run_id="") == "run-2"


class TestStageIdentity:
    def test_bound_context_defaults_correlation_id_to_run_id(self) -> None:
        assert _IDENTITY.bound_context()["correlation_id"] == "run-0001"

    def test_bound_context_prefers_an_explicit_correlation_id(self) -> None:
        identity = StageIdentity(**{**_IDENTITY.__dict__, "correlation_id": "run-0000"})
        assert identity.bound_context()["correlation_id"] == "run-0000"

    def test_optional_dimensions_are_omitted_when_unset(self) -> None:
        context = _IDENTITY.bound_context()
        assert "connection_id" not in context
        assert "scope_unit_id" not in context
        assert "ConnectionId" not in _IDENTITY.metric_dimensions()

    def test_connection_and_scope_unit_are_carried_when_set(self) -> None:
        identity = StageIdentity(
            **{**_IDENTITY.__dict__, "connection_id": "sf-west", "scope_unit_id": "brand-a"}
        )
        assert identity.bound_context()["connection_id"] == "sf-west"
        assert identity.bound_context()["scope_unit_id"] == "brand-a"
        assert identity.metric_dimensions()["ConnectionId"] == "sf-west"

    def test_scope_unit_is_not_a_metric_dimension(self) -> None:
        # Scope units are unbounded per tenant; as a CloudWatch dimension they would
        # multiply custom-metric cost without bound.
        identity = StageIdentity(**{**_IDENTITY.__dict__, "scope_unit_id": "brand-a"})
        assert "ScopeUnitId" not in identity.metric_dimensions()


class TestLifecycleGuarantees:
    def test_tenant_context_is_set_on_construction(self) -> None:
        _, emitter = _make_execution()
        assert emitter.tenant_code == "demo"

    def test_contextvars_are_bound_inside_and_cleared_after(self) -> None:
        execution, _ = _make_execution()
        with execution:
            bound = structlog.contextvars.get_contextvars()
            assert bound["run_id"] == "run-0001"
            assert bound["stage"] == "extraction"
        assert structlog.contextvars.get_contextvars() == {}

    def test_contextvars_are_cleared_even_when_the_body_raises(self) -> None:
        # This is the warm-container leak that was a real, previously-fixed bug.
        execution, _ = _make_execution()
        with pytest.raises(ValueError), execution:
            raise ValueError("boom")
        assert structlog.contextvars.get_contextvars() == {}

    def test_exceptions_are_never_swallowed(self) -> None:
        execution, _ = _make_execution()
        with pytest.raises(RuntimeError, match="propagate me"), execution:
            raise RuntimeError("propagate me")

    def test_duration_metric_is_emitted_on_the_happy_path(self) -> None:
        execution, emitter = _make_execution()
        with execution:
            pass
        durations = emitter.metrics_of(PlatformMetric.STAGE_DURATION_MS)
        assert len(durations) == 1
        assert durations[0][1] >= 0.0

    def test_duration_metric_is_emitted_on_the_failure_path_too(self) -> None:
        execution, emitter = _make_execution()
        with pytest.raises(ValueError), execution:
            raise ValueError("boom")
        assert emitter.metrics_of(PlatformMetric.STAGE_DURATION_MS)

    def test_flush_happens_exactly_once_per_execution(self) -> None:
        execution, emitter = _make_execution()
        with execution:
            pass
        assert emitter.flush_count == 1

    def test_flush_never_raises_out_of_exit(self) -> None:
        # Telemetry delivery failing must not turn a successful stage into a failed one.
        execution, emitter = _make_execution()
        emitter.raise_on_flush = True
        with execution:
            pass
        assert emitter.flush_count == 1

    def test_emitted_metrics_carry_the_stage_dimensions(self) -> None:
        execution, emitter = _make_execution()
        with execution as stage:
            stage.emit(PlatformMetric.RECORDS_EXTRACTED, 42.0)
        _, value, dimensions = emitter.metrics_of(PlatformMetric.RECORDS_EXTRACTED)[0]
        assert value == 42.0
        assert dimensions == {
            "SourceId": "salesforce",
            "EntityId": "salesforce-account",
            "Stage": "extraction",
        }

    def test_extra_dimensions_merge_over_the_stage_dimensions(self) -> None:
        execution, emitter = _make_execution()
        with execution as stage:
            stage.emit(
                PlatformMetric.RECORDS_EXTRACTED, 1.0, extra_dimensions={"Stage": "override"}
            )
        _, _, dimensions = emitter.metrics_of(PlatformMetric.RECORDS_EXTRACTED)[0]
        assert dimensions["Stage"] == "override"


class TestRecorderDraining:
    def test_a_metric_recorded_deep_in_a_module_is_delivered_by_the_stage(self) -> None:
        # The recorder is the only path a domain module has; if the stage did not drain it,
        # every one of those metrics would silently never arrive.
        execution, emitter = _make_execution()
        with execution:
            record_platform_metric(PlatformMetric.EMPTY_SCOPE_DENIALS, 3.0, ScopeUnitId="brand-a")
        recorded = emitter.metrics_of(PlatformMetric.EMPTY_SCOPE_DENIALS)
        assert len(recorded) == 1
        assert recorded[0][1] == 3.0
        assert recorded[0][2]["ScopeUnitId"] == "brand-a"
        assert recorded[0][2]["SourceId"] == "salesforce"

    def test_the_recorder_is_empty_after_the_stage_completes(self) -> None:
        execution, _ = _make_execution()
        with execution:
            record_platform_metric(PlatformMetric.EMPTY_SCOPE_DENIALS)
        assert platform_metric_recorder.buffered == 0

    def test_recorded_metrics_are_drained_on_the_failure_path(self) -> None:
        execution, emitter = _make_execution()
        with pytest.raises(ValueError), execution:
            record_platform_metric(PlatformMetric.EMPTY_SCOPE_DENIALS)
            raise ValueError("boom")
        assert emitter.metrics_of(PlatformMetric.EMPTY_SCOPE_DENIALS)


class TestFailureRecord:
    def test_failure_callback_receives_the_error_code_and_message(self) -> None:
        seen: list[tuple[str, str]] = []
        execution, _ = _make_execution(on_failure_record=lambda code, msg: seen.append((code, msg)))
        with pytest.raises(ValueError), execution:
            raise ValueError("bad payload")
        assert seen == [("ValueError", "bad payload")]

    def test_records_failed_metric_accompanies_the_failure_record(self) -> None:
        execution, emitter = _make_execution()
        with pytest.raises(ValueError), execution:
            raise ValueError("boom")
        assert emitter.metrics_of(PlatformMetric.RECORDS_FAILED)

    def test_failure_record_is_written_at_most_once(self) -> None:
        calls: list[str] = []
        execution, _ = _make_execution(on_failure_record=lambda code, msg: calls.append(code))
        execution._record_failure("first", "one")
        execution._record_failure("second", "two")
        assert calls == ["first"]

    def test_a_failing_failure_writer_does_not_mask_the_original_exception(self) -> None:
        def explode(code: str, message: str) -> None:
            raise RuntimeError("DynamoDB down")

        execution, _ = _make_execution(on_failure_record=explode)
        with pytest.raises(ValueError, match="original"), execution:
            raise ValueError("original")

    def test_no_failure_callback_is_tolerated(self) -> None:
        execution, emitter = _make_execution(on_failure_record=None)
        with pytest.raises(ValueError), execution:
            raise ValueError("boom")
        assert emitter.metrics_of(PlatformMetric.RECORDS_FAILED)


class TestHardKillWatchdog:
    def test_watchdog_is_armed_when_a_lambda_context_reports_remaining_time(self) -> None:
        execution, _ = _make_execution(
            lambda_context=_FakeLambdaContext(60_000), hard_kill_margin_ms=5_000
        )
        with execution:
            assert isinstance(execution._watchdog, threading.Timer)
            assert execution._watchdog.is_alive()
        assert execution._watchdog is None

    def test_no_watchdog_without_a_lambda_context(self) -> None:
        execution, _ = _make_execution()
        with execution:
            assert execution._watchdog is None

    def test_no_watchdog_when_less_than_the_margin_remains(self) -> None:
        # Arming a timer for a negative delay would fire immediately and record a
        # failure for a stage that has not failed.
        execution, _ = _make_execution(
            lambda_context=_FakeLambdaContext(1_000), hard_kill_margin_ms=5_000
        )
        with execution:
            assert execution._watchdog is None

    def test_a_context_whose_remaining_time_call_fails_is_treated_as_absent(self) -> None:
        class Hostile:
            def get_remaining_time_in_millis(self) -> int:
                raise RuntimeError("no runtime API")

        execution, _ = _make_execution(lambda_context=Hostile())
        with execution:
            assert execution._watchdog is None

    def test_watchdog_fires_a_failure_record_and_flushes(self) -> None:
        seen: list[str] = []
        execution, emitter = _make_execution(
            lambda_context=_FakeLambdaContext(5_020),
            hard_kill_margin_ms=5_000,
            on_failure_record=lambda code, msg: seen.append(code),
        )
        with execution:
            watchdog = execution._watchdog
            assert watchdog is not None
            watchdog.join(1.0)
        assert seen == ["lambda_hard_timeout"]
        assert emitter.metrics_of(PlatformMetric.RECORDS_FAILED)

    def test_watchdog_does_not_fire_for_a_stage_that_finishes_first(self) -> None:
        seen: list[str] = []
        execution, _ = _make_execution(
            lambda_context=_FakeLambdaContext(60_000),
            on_failure_record=lambda code, msg: seen.append(code),
        )
        with execution:
            pass
        assert seen == []


class TestConvenienceWrapper:
    def test_wrapper_yields_a_usable_execution_and_clears_context(self) -> None:
        with stage_execution(_IDENTITY, region_name="us-east-1") as stage:
            assert isinstance(stage, StageExecution)
            assert structlog.contextvars.get_contextvars()["stage"] == "extraction"
        assert structlog.contextvars.get_contextvars() == {}

    def test_wrapper_propagates_the_failure_callback(self) -> None:
        seen: list[str] = []
        with pytest.raises(ValueError):
            with stage_execution(
                _IDENTITY,
                region_name="us-east-1",
                on_failure_record=lambda code, msg: seen.append(code),
            ):
                raise ValueError("boom")
        assert seen == ["ValueError"]
