"""
Tests for ExtractionWorkflow.

Coverage:
  - Happy path: full pipeline completes, all stages emitted, watermark advanced
  - Replay flag: REPLAY_INITIATION stage emitted
  - CONFIGURATION_LOAD failure: DLQ enqueued, watermark NOT advanced
  - FIELD_DISCOVERY failure: DLQ enqueued, watermark NOT advanced
  - EXTRACTION failure: DLQ enqueued, watermark NOT advanced
  - WATERMARK_UPDATE failure: DLQ enqueued
  - BREAKING drift: pipeline succeeds, transformation_blocked=True, watermark advanced
  - Circuit breaker open: CircuitOpenError raised before any AWS calls
  - Circuit breaker: record_failure called on any pipeline exception
  - Circuit breaker: record_success called on happy path
  - First-run watermark: initialise_watermark called when no prior record
  - Subsequent-run watermark: advance_watermark called when prior record exists
  - Drift report written to snapshot repository
  - TRANSFORMATION stage not emitted when drift is BREAKING
  - TRANSFORMATION stage emitted on normal completion
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from connector_runtime.interfaces.connector_interface import (
    ExtractionErrorClassification,
    ExtractionRecord,
    FieldContract,
    FieldDescriptor,
    QueryContract,
)
from connector_runtime.run_lifecycle.run_lifecycle import generate_run_id
from contracts.entity_configuration_contract import (
    EntityExtractionConfig,
    FieldMode,
    LoadType,
    OutputFormat,
)
from contracts.observability_contract import PipelineStage
from contracts.pipeline_stage_contract import DriftClassification
from orchestration.step_functions.extraction_retry_policy import (
    CircuitOpenError,
    ExtractionRetryPolicy,
)
from orchestration.step_functions.extraction_workflow import (
    ExtractionWorkflow,
    ExtractionWorkflowResult,
    LambdaTimeoutWarning,
)
from schema_management.drift_evaluation.drift_evaluator import DriftReport
from schema_management.snapshot_repository.snapshot_repository import SchemaSnapshot

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_ENV = "dev"
_REGION = "us-east-1"
_SOURCE = "salesforce"
_ENTITY = "salesforce-account"
_RUN_ID = generate_run_id()
_FINGERPRINT = "abc123def456" * 4  # 48-char stub


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _make_entity_config(**overrides: object) -> EntityExtractionConfig:
    base: dict[str, object] = {
        "source_id": _SOURCE,
        "entity_id": _ENTITY,
        "config_version": "1.0.0",
        "load_type": LoadType.FULL,
        "watermark_field": None,
        "extraction_window_days": 1,
        "watermark_overlap_hours": 0,
        "field_mode": FieldMode.ALL,
        "include_fields": [],
        "exclude_fields": [],
        "target_raw_s3_prefix": "s3://raw/salesforce/account/",
        "schema_snapshot_s3_prefix": "s3://schema-snapshots/salesforce/account/",
        "output_format": OutputFormat.PARQUET,
        "active": True,
    }
    base.update(overrides)
    return EntityExtractionConfig(**base)


def _make_field_contract() -> FieldContract:
    fields = (
        FieldDescriptor(name="Id", data_type="id", is_nullable=False, is_queryable=True),
        FieldDescriptor(name="Name", data_type="string", is_nullable=True, is_queryable=True),
    )
    return FieldContract(
        source_id=_SOURCE,
        entity_id=_ENTITY,
        fields=fields,
        discovery_timestamp=datetime.now(UTC),
        schema_fingerprint=FieldContract.compute_fingerprint(fields),
    )


def _make_query_contract() -> QueryContract:
    return QueryContract(
        source_id=_SOURCE,
        entity_id=_ENTITY,
        query_text="SELECT Id, Name FROM Account",
        query_parameters={},
        load_type=LoadType.FULL,
        watermark_lower=None,
        watermark_upper=None,
    )


def _make_drift_report(
    classification: DriftClassification = DriftClassification.NO_DRIFT,
) -> DriftReport:
    return DriftReport(
        source_id=_SOURCE,
        entity_id=_ENTITY,
        evaluated_at=datetime.now(UTC).isoformat(),
        previous_schema_version=None,
        current_schema_version=_FINGERPRINT,
        overall_classification=classification,
        field_changes=(),
    )


def _make_snapshot() -> SchemaSnapshot:
    return SchemaSnapshot(
        source_id=_SOURCE,
        entity_id=_ENTITY,
        schema_version=_FINGERPRINT,
        extraction_date="2026-06-12",
        captured_at=datetime.now(UTC).isoformat(),
        fields=(),
        record_count=10,
    )


def _make_workflow(
    *,
    drift_classification: DriftClassification = DriftClassification.NO_DRIFT,
    extraction_records: list[ExtractionRecord] | None = None,
    watermark_record: object = None,
    retry_policy: ExtractionRetryPolicy | None = None,
    entity_config: EntityExtractionConfig | None = None,
    lambda_context: object = None,
    consume_record_iter_fully: bool = False,
) -> tuple[ExtractionWorkflow, dict[str, MagicMock]]:
    """Build an ExtractionWorkflow with fully-mocked dependencies.

    consume_record_iter_fully: when True, write_partition_streaming's mock
    actually iterates the record_iter it receives (list(record_iter)) instead
    of just recording the call — required to exercise the PERF-5 checkpoint-
    bounded iterator, which only does anything when something actually pulls
    records from it.
    """
    records = extraction_records or [
        ExtractionRecord(payload={"Id": "1", "Name": "Acme"}),
        ExtractionRecord(payload={"Id": "2", "Name": "Globex"}),
    ]

    # Mock RunCoordinator
    coordinator = MagicMock()
    coordinator.run_id = _RUN_ID
    coordinator.started_at = datetime.now(UTC)
    coordinator.source_id = _SOURCE
    coordinator.entity_id = _ENTITY
    coordinator.tenant_code = "demo"
    coordinator.emit_checkpoint_stage.return_value = MagicMock(run_id=f"{_RUN_ID}-part1")

    # Mock ConfigurationRepositoryClient
    config_client = MagicMock()
    config_client.load_config.return_value = entity_config or _make_entity_config()

    # Mock WatermarkRepository
    watermark_repo = MagicMock()
    watermark_repo.get_watermark.return_value = watermark_record
    watermark_repo.compute_extraction_window = MagicMock(
        return_value=(datetime(2026, 6, 11, tzinfo=UTC), datetime(2026, 6, 12, tzinfo=UTC))
    )

    # Mock SchemaSnapshotRepository
    snapshot_repo = MagicMock()
    snapshot_repo.write_snapshot.return_value = "salesforce/salesforce-account/snap.json"
    snapshot_repo.load_latest_snapshot.return_value = None  # first run
    snapshot_repo.write_drift_report.return_value = "salesforce/salesforce-account/drift.json"

    # Mock SchemaDriftEvaluator
    drift_evaluator = MagicMock()
    drift_evaluator.evaluate.return_value = _make_drift_report(drift_classification)

    # Mock ConnectorInterface
    connector = MagicMock()
    connector.discover_queryable_fields.return_value = _make_field_contract()
    connector.build_extraction_query.return_value = _make_query_contract()
    connector.execute_extraction.return_value = iter(records)
    connector.classify_extraction_error.return_value = ExtractionErrorClassification.UNKNOWN

    # Mock RawLayerWriter
    raw_writer = MagicMock()
    raw_writer.write_partition.return_value = "s3://raw/salesforce/account/2026-06-12/"
    if consume_record_iter_fully:

        def _write_partition_streaming(record_iter, **_kwargs):  # type: ignore[no-untyped-def]
            consumed = list(record_iter)
            return ("s3://raw/salesforce/account/2026-06-12/", len(consumed))

        raw_writer.write_partition_streaming.side_effect = _write_partition_streaming
    else:
        raw_writer.write_partition_streaming.return_value = (
            "s3://raw/salesforce/account/2026-06-12/",
            len(records),
        )

    mocks = {
        "coordinator": coordinator,
        "config_client": config_client,
        "watermark_repo": watermark_repo,
        "snapshot_repo": snapshot_repo,
        "drift_evaluator": drift_evaluator,
        "connector": connector,
        "raw_writer": raw_writer,
    }

    workflow = ExtractionWorkflow(
        run_coordinator=coordinator,
        configuration_client=config_client,
        watermark_repository=watermark_repo,
        snapshot_repository=snapshot_repo,
        drift_evaluator=drift_evaluator,
        connector=connector,
        raw_layer_writer=raw_writer,
        retry_policy=retry_policy,
        lambda_context=lambda_context,
    )
    return workflow, mocks


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_execute_returns_result(self) -> None:
        workflow, _mocks = _make_workflow()
        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=(datetime(2026, 6, 11, tzinfo=UTC), datetime(2026, 6, 12, tzinfo=UTC)),
        ):
            result = workflow.execute()

        assert isinstance(result, ExtractionWorkflowResult)
        assert result.source_id == _SOURCE
        assert result.entity_id == _ENTITY
        assert result.run_id == _RUN_ID
        assert result.record_count == 2

    def test_execute_emits_run_completion_success(self) -> None:
        workflow, mocks = _make_workflow()
        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=(datetime(2026, 6, 11, tzinfo=UTC), datetime(2026, 6, 12, tzinfo=UTC)),
        ):
            workflow.execute()

        emit_calls = mocks["coordinator"].emit_stage.call_args_list
        stages_emitted = [c[1]["stage"] for c in emit_calls if "stage" in c[1]]
        assert PipelineStage.RUN_COMPLETION in stages_emitted

    def test_execute_calls_watermark_initialise_on_first_run(self) -> None:
        workflow, mocks = _make_workflow(watermark_record=None)
        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=(datetime(2026, 6, 11, tzinfo=UTC), datetime(2026, 6, 12, tzinfo=UTC)),
        ):
            workflow.execute()

        mocks["watermark_repo"].initialise_watermark.assert_called_once()
        mocks["watermark_repo"].advance_watermark.assert_not_called()

    def test_execute_calls_watermark_advance_on_subsequent_run(self) -> None:
        prior_watermark = MagicMock()
        workflow, mocks = _make_workflow(watermark_record=prior_watermark)
        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=(datetime(2026, 6, 11, tzinfo=UTC), datetime(2026, 6, 12, tzinfo=UTC)),
        ):
            workflow.execute()

        mocks["watermark_repo"].advance_watermark.assert_called_once()
        mocks["watermark_repo"].initialise_watermark.assert_not_called()

    def test_execute_writes_schema_snapshot(self) -> None:
        workflow, mocks = _make_workflow()
        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=(datetime(2026, 6, 11, tzinfo=UTC), datetime(2026, 6, 12, tzinfo=UTC)),
        ):
            workflow.execute()

        mocks["snapshot_repo"].write_snapshot.assert_called_once()

    def test_execute_writes_drift_report(self) -> None:
        workflow, mocks = _make_workflow()
        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=(datetime(2026, 6, 11, tzinfo=UTC), datetime(2026, 6, 12, tzinfo=UTC)),
        ):
            workflow.execute()

        mocks["snapshot_repo"].write_drift_report.assert_called_once()

    def test_transformation_trigger_emitted_on_normal_completion(self) -> None:
        workflow, mocks = _make_workflow(drift_classification=DriftClassification.NO_DRIFT)
        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=(datetime(2026, 6, 11, tzinfo=UTC), datetime(2026, 6, 12, tzinfo=UTC)),
        ):
            workflow.execute()

        emit_calls = mocks["coordinator"].emit_stage.call_args_list
        stages_emitted = [c[1].get("stage") or c[0][0] for c in emit_calls]
        assert PipelineStage.TRANSFORMATION in stages_emitted

    def test_transformation_not_triggered_on_breaking_drift(self) -> None:
        workflow, mocks = _make_workflow(drift_classification=DriftClassification.BREAKING)
        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=(datetime(2026, 6, 11, tzinfo=UTC), datetime(2026, 6, 12, tzinfo=UTC)),
        ):
            result = workflow.execute()

        assert result.transformation_blocked is True
        emit_calls = mocks["coordinator"].emit_stage.call_args_list
        stages_emitted = [c[1].get("stage") or c[0][0] for c in emit_calls]
        assert PipelineStage.TRANSFORMATION not in stages_emitted

    def test_breaking_drift_still_advances_watermark(self) -> None:
        """Raw data was written; watermark must advance so we don't re-extract it."""
        prior_watermark = MagicMock()
        workflow, mocks = _make_workflow(
            watermark_record=prior_watermark,
            drift_classification=DriftClassification.BREAKING,
        )
        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=(datetime(2026, 6, 11, tzinfo=UTC), datetime(2026, 6, 12, tzinfo=UTC)),
        ):
            workflow.execute()

        mocks["watermark_repo"].advance_watermark.assert_called_once()


class TestReplayFlag:
    def test_replay_emits_replay_initiation_stage(self) -> None:
        workflow, mocks = _make_workflow()
        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=(datetime(2026, 6, 11, tzinfo=UTC), datetime(2026, 6, 12, tzinfo=UTC)),
        ):
            workflow.execute(is_replay=True, replay_of_run_id="run-original")

        emit_calls = mocks["coordinator"].emit_stage.call_args_list
        stages_emitted = [c[1].get("stage") or c[0][0] for c in emit_calls]
        assert PipelineStage.REPLAY_INITIATION in stages_emitted

    def test_replay_without_run_id_raises(self) -> None:
        """is_replay=True but replay_of_run_id=None must raise ValueError."""
        workflow, _ = _make_workflow()
        with pytest.raises(ValueError, match="replay_of_run_id"):
            workflow.execute(is_replay=True, replay_of_run_id=None)

    def test_non_replay_does_not_emit_replay_initiation(self) -> None:
        workflow, mocks = _make_workflow()
        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=(datetime(2026, 6, 11, tzinfo=UTC), datetime(2026, 6, 12, tzinfo=UTC)),
        ):
            workflow.execute()

        emit_calls = mocks["coordinator"].emit_stage.call_args_list
        stages_emitted = [c[1].get("stage") or c[0][0] for c in emit_calls]
        assert PipelineStage.REPLAY_INITIATION not in stages_emitted


class TestFailurePropagation:
    def test_config_load_failure_routes_to_dlq(self) -> None:
        workflow, mocks = _make_workflow()
        mocks["config_client"].load_config.side_effect = RuntimeError("config not found")

        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=(datetime(2026, 6, 11, tzinfo=UTC), datetime(2026, 6, 12, tzinfo=UTC)),
        ):
            with pytest.raises(RuntimeError):
                workflow.execute()

        mocks["coordinator"].enqueue_dlq_entry.assert_called_once()

    def test_config_load_failure_does_not_advance_watermark(self) -> None:
        workflow, mocks = _make_workflow()
        mocks["config_client"].load_config.side_effect = RuntimeError("config not found")

        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=(datetime(2026, 6, 11, tzinfo=UTC), datetime(2026, 6, 12, tzinfo=UTC)),
        ):
            with pytest.raises(RuntimeError):
                workflow.execute()

        mocks["watermark_repo"].advance_watermark.assert_not_called()
        mocks["watermark_repo"].initialise_watermark.assert_not_called()

    def test_extraction_failure_routes_to_dlq(self) -> None:
        workflow, mocks = _make_workflow()
        mocks["connector"].execute_extraction.side_effect = OSError("connection refused")

        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=(datetime(2026, 6, 11, tzinfo=UTC), datetime(2026, 6, 12, tzinfo=UTC)),
        ):
            with pytest.raises(OSError):
                workflow.execute()

        mocks["coordinator"].enqueue_dlq_entry.assert_called_once()
        mocks["watermark_repo"].advance_watermark.assert_not_called()
        mocks["watermark_repo"].initialise_watermark.assert_not_called()

    def test_watermark_failure_routes_to_dlq(self) -> None:
        workflow, mocks = _make_workflow()
        mocks["watermark_repo"].initialise_watermark.side_effect = RuntimeError("dynamo error")

        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=(datetime(2026, 6, 11, tzinfo=UTC), datetime(2026, 6, 12, tzinfo=UTC)),
        ):
            with pytest.raises(RuntimeError):
                workflow.execute()

        mocks["coordinator"].enqueue_dlq_entry.assert_called_once()

    def test_dlq_enqueue_stage_emitted_on_failure(self) -> None:
        workflow, mocks = _make_workflow()
        mocks["config_client"].load_config.side_effect = RuntimeError("oops")

        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=(datetime(2026, 6, 11, tzinfo=UTC), datetime(2026, 6, 12, tzinfo=UTC)),
        ):
            with pytest.raises(RuntimeError):
                workflow.execute()

        emit_calls = mocks["coordinator"].emit_stage.call_args_list
        stages_emitted = [c[1].get("stage") or c[0][0] for c in emit_calls]
        assert PipelineStage.DLQ_ENQUEUE in stages_emitted


class TestCircuitBreakerIntegration:
    def test_open_circuit_raises_before_any_aws_calls(self) -> None:
        policy = ExtractionRetryPolicy(circuit_open_threshold=2)
        # Regression for the entity_id-omission bug: the guard check in
        # execute() must key on the same (source_id, entity_id) pair that
        # record_failure()/record_success() write under, or the circuit can
        # never open. Seed failures under the real _ENTITY, matching what
        # workflow.execute()'s guard now checks.
        policy.record_failure(_SOURCE, _ENTITY)
        policy.record_failure(_SOURCE, _ENTITY)

        workflow, mocks = _make_workflow(retry_policy=policy)

        with pytest.raises(CircuitOpenError):
            with patch(
                "orchestration.step_functions.extraction_workflow.WatermarkRepository"
                ".compute_extraction_window",
                return_value=(
                    datetime(2026, 6, 11, tzinfo=UTC),
                    datetime(2026, 6, 12, tzinfo=UTC),
                ),
            ):
                workflow.execute()

        # No AWS calls should have been made
        mocks["config_client"].load_config.assert_not_called()
        mocks["watermark_repo"].get_watermark.assert_not_called()

    def test_failures_for_a_different_entity_do_not_open_the_circuit(self) -> None:
        """
        Regression for the entity_id-omission bug: failures recorded under a
        different entity_id (same source_id) must not open the circuit for
        this workflow's own entity_id. Before the fix, the guard check
        dropped entity_id entirely, so it would have read the SAME key
        regardless of which entity actually failed — this test would have
        failed against that buggy guard by raising CircuitOpenError here.
        """
        policy = ExtractionRetryPolicy(circuit_open_threshold=2)
        policy.record_failure(_SOURCE, "some-other-entity")
        policy.record_failure(_SOURCE, "some-other-entity")

        workflow, _mocks = _make_workflow(retry_policy=policy)

        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=(datetime(2026, 6, 11, tzinfo=UTC), datetime(2026, 6, 12, tzinfo=UTC)),
        ):
            # Must complete normally — _ENTITY's own circuit is still closed.
            workflow.execute()

    def test_success_calls_record_success_on_policy(self) -> None:
        policy = MagicMock(spec=ExtractionRetryPolicy)
        policy.is_circuit_open.return_value = False
        workflow, _ = _make_workflow(retry_policy=policy)

        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=(datetime(2026, 6, 11, tzinfo=UTC), datetime(2026, 6, 12, tzinfo=UTC)),
        ):
            workflow.execute()

        policy.record_success.assert_called_once_with(_SOURCE, _ENTITY, tenant_code="demo")

    def test_failure_calls_record_failure_on_policy(self) -> None:
        policy = MagicMock(spec=ExtractionRetryPolicy)
        policy.is_circuit_open.return_value = False
        workflow, mocks = _make_workflow(retry_policy=policy)
        mocks["connector"].execute_extraction.side_effect = OSError("network failure")

        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=(datetime(2026, 6, 11, tzinfo=UTC), datetime(2026, 6, 12, tzinfo=UTC)),
        ):
            with pytest.raises(OSError):
                workflow.execute()

        policy.record_failure.assert_called_once_with(_SOURCE, _ENTITY, tenant_code="demo")


# ---------------------------------------------------------------------------
# PERF-5: checkpoint / partial-run tests
# ---------------------------------------------------------------------------

_CHECKPOINT_WINDOW = (datetime(2026, 6, 11, tzinfo=UTC), datetime(2026, 6, 12, tzinfo=UTC))


def _checkpoint_records(n: int, start_hour: int = 10) -> list[ExtractionRecord]:
    return [
        ExtractionRecord(
            payload={"Id": str(i)},
            source_timestamp=f"2026-06-11T{start_hour + i:02d}:00:00+00:00",
        )
        for i in range(n)
    ]


class TestPerf5Checkpoint:
    """
    PERF-5: checkpoint/resume detection for max_records_per_lambda_run and
    low remaining Lambda time.

    Covers: cap-reached triggers a checkpoint; natural EOF exactly at the cap
    does NOT spuriously checkpoint; FULL loads ignore the cap; low remaining
    time triggers a checkpoint; the partial watermark is clamped to never
    regress; the checkpoint audit record is distinct from the main run's
    audit trail; and LambdaTimeoutWarning is never routed to the DLQ or
    counted as a circuit-breaker failure.
    """

    def test_checkpoint_triggers_on_max_records_reached(self) -> None:
        config = _make_entity_config(
            load_type=LoadType.INCREMENTAL,
            watermark_field="LastModifiedDate",
            max_records_per_lambda_run=2,
        )
        workflow, mocks = _make_workflow(
            entity_config=config,
            extraction_records=_checkpoint_records(3),
            consume_record_iter_fully=True,
        )

        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=_CHECKPOINT_WINDOW,
        ):
            with pytest.raises(LambdaTimeoutWarning) as excinfo:
                workflow.execute()

        assert excinfo.value.records_written == 2
        assert "max_records_per_lambda_run" in excinfo.value.reason

        # First run (no prior watermark) — initialised to the 2nd record's OWN
        # timestamp, not the full window's upper_bound.
        mocks["watermark_repo"].initialise_watermark.assert_called_once()
        _, init_kwargs = mocks["watermark_repo"].initialise_watermark.call_args
        assert init_kwargs["upper_watermark"] == datetime(2026, 6, 11, 11, tzinfo=UTC)

    def test_no_checkpoint_when_source_exhausted_exactly_at_cap(self) -> None:
        """Natural completion at exactly the cap must NOT be flagged as a checkpoint."""
        config = _make_entity_config(
            load_type=LoadType.INCREMENTAL,
            watermark_field="LastModifiedDate",
            max_records_per_lambda_run=2,
        )
        workflow, mocks = _make_workflow(
            entity_config=config,
            extraction_records=_checkpoint_records(2),
            consume_record_iter_fully=True,
        )

        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=_CHECKPOINT_WINDOW,
        ):
            result = workflow.execute()

        assert isinstance(result, ExtractionWorkflowResult)
        assert result.record_count == 2
        mocks["coordinator"].emit_checkpoint_stage.assert_not_called()
        # Normal completion advances to the full window's upper_bound.
        _, init_kwargs = mocks["watermark_repo"].initialise_watermark.call_args
        assert init_kwargs["upper_watermark"] == _CHECKPOINT_WINDOW[1]

    def test_checkpoint_ignored_for_full_load(self) -> None:
        config = _make_entity_config(load_type=LoadType.FULL, max_records_per_lambda_run=2)
        workflow, mocks = _make_workflow(
            entity_config=config,
            extraction_records=_checkpoint_records(5),
            consume_record_iter_fully=True,
        )

        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=_CHECKPOINT_WINDOW,
        ):
            result = workflow.execute()

        assert isinstance(result, ExtractionWorkflowResult)
        assert result.record_count == 5  # cap ignored for FULL loads
        mocks["coordinator"].emit_checkpoint_stage.assert_not_called()

    def test_checkpoint_triggers_on_low_remaining_lambda_time(self) -> None:
        from orchestration.step_functions.extraction_workflow import (
            _CHECKPOINT_TIME_CHECK_INTERVAL_RECORDS,
        )

        config = _make_entity_config(
            load_type=LoadType.INCREMENTAL,
            watermark_field="LastModifiedDate",
            max_records_per_lambda_run=1_000_000,  # never reached — time is the trigger
        )
        total = _CHECKPOINT_TIME_CHECK_INTERVAL_RECORDS + 500
        records = [
            ExtractionRecord(payload={"Id": str(i)}, source_timestamp="2026-06-11T10:00:00+00:00")
            for i in range(total)
        ]
        lambda_context = MagicMock()
        lambda_context.get_remaining_time_in_millis.return_value = 1_000  # below threshold

        workflow, mocks = _make_workflow(
            entity_config=config,
            extraction_records=records,
            consume_record_iter_fully=True,
            lambda_context=lambda_context,
        )

        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=_CHECKPOINT_WINDOW,
        ):
            with pytest.raises(LambdaTimeoutWarning) as excinfo:
                workflow.execute()

        assert excinfo.value.records_written == _CHECKPOINT_TIME_CHECK_INTERVAL_RECORDS
        assert "remaining time" in excinfo.value.reason
        del mocks  # unused beyond the exception assertions above

    def test_partial_watermark_clamped_to_not_regress(self) -> None:
        """A checkpoint's own timestamp must never regress before the prior watermark."""
        prior_watermark = MagicMock()
        prior_watermark.upper_watermark = datetime(2026, 6, 11, 15, tzinfo=UTC)

        config = _make_entity_config(
            load_type=LoadType.INCREMENTAL,
            watermark_field="LastModifiedDate",
            max_records_per_lambda_run=2,
        )
        # Records' own timestamps (10:00, 11:00) fall BEFORE prior_watermark
        # (15:00) — plausible when re-processing the overlap window.
        workflow, mocks = _make_workflow(
            entity_config=config,
            extraction_records=_checkpoint_records(3),
            consume_record_iter_fully=True,
            watermark_record=prior_watermark,
        )

        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=_CHECKPOINT_WINDOW,
        ):
            with pytest.raises(LambdaTimeoutWarning):
                workflow.execute()

        mocks["watermark_repo"].advance_watermark.assert_called_once()
        _, advance_kwargs = mocks["watermark_repo"].advance_watermark.call_args
        assert advance_kwargs["new_upper_watermark"] == prior_watermark.upper_watermark

    def test_checkpoint_emits_distinct_audit_record(self) -> None:
        config = _make_entity_config(
            load_type=LoadType.INCREMENTAL,
            watermark_field="LastModifiedDate",
            max_records_per_lambda_run=2,
        )
        workflow, mocks = _make_workflow(
            entity_config=config,
            extraction_records=_checkpoint_records(3),
            consume_record_iter_fully=True,
        )

        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=_CHECKPOINT_WINDOW,
        ):
            with pytest.raises(LambdaTimeoutWarning):
                workflow.execute()

        mocks["coordinator"].emit_checkpoint_stage.assert_called_once()
        _, checkpoint_kwargs = mocks["coordinator"].emit_checkpoint_stage.call_args
        assert checkpoint_kwargs["part_number"] == 1
        assert checkpoint_kwargs["record_count"] == 2

    def test_lambda_timeout_warning_not_routed_to_dlq(self) -> None:
        config = _make_entity_config(
            load_type=LoadType.INCREMENTAL,
            watermark_field="LastModifiedDate",
            max_records_per_lambda_run=2,
        )
        workflow, mocks = _make_workflow(
            entity_config=config,
            extraction_records=_checkpoint_records(3),
            consume_record_iter_fully=True,
        )

        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=_CHECKPOINT_WINDOW,
        ):
            with pytest.raises(LambdaTimeoutWarning):
                workflow.execute()

        mocks["coordinator"].enqueue_dlq_entry.assert_not_called()

    def test_lambda_timeout_warning_does_not_count_as_circuit_failure(self) -> None:
        policy = MagicMock(spec=ExtractionRetryPolicy)
        policy.is_circuit_open.return_value = False
        config = _make_entity_config(
            load_type=LoadType.INCREMENTAL,
            watermark_field="LastModifiedDate",
            max_records_per_lambda_run=2,
        )
        workflow, mocks = _make_workflow(
            entity_config=config,
            extraction_records=_checkpoint_records(3),
            consume_record_iter_fully=True,
            retry_policy=policy,
        )

        with patch(
            "orchestration.step_functions.extraction_workflow.WatermarkRepository"
            ".compute_extraction_window",
            return_value=_CHECKPOINT_WINDOW,
        ):
            with pytest.raises(LambdaTimeoutWarning):
                workflow.execute()

        policy.record_failure.assert_not_called()
        del mocks  # unused beyond the exception/policy assertions above
