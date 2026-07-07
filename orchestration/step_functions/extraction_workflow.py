"""
Extraction pipeline workflow orchestrator for the Enterprise Data Lake platform.

ExtractionWorkflow wires together all platform services into a single, atomic
pipeline run:

  1. CONFIGURATION_LOAD      — load EntityExtractionConfig from the config repository
  2. CREDENTIAL_RETRIEVAL    — resolve watermark and compute extraction window
  3. METADATA_DISCOVERY      — discover queryable fields from source metadata
  4. QUERY_BUILD             — build parameterized extraction query with watermark bounds
  5. EXTRACTION              — stream records from source and write raw Parquet to S3
  6. SCHEMA_SNAPSHOT         — persist field schema snapshot to S3
  7. SCHEMA_DRIFT_EVALUATION — compare schema against previous snapshot; write drift report
  8. RAW_WRITE               — validate extracted record count
  9. WATERMARK_UPDATE        — advance watermark (success-only)
 10. RUN_COMPLETION          — emit final audit record and return result

Failure contract:
  - Any exception at any stage causes the pipeline to fail.
  - The failing stage is emitted to the audit log with FAILURE status.
  - A DLQ entry is enqueued with full context for replay.
  - The exception is re-raised so Step Functions records the failure.
  - The watermark is NEVER advanced on failure.

Checkpoint / partial-run contract (PERF-5):
  - Opt-in per entity via EntityExtractionConfig.max_records_per_lambda_run.
  - Only applies to INCREMENTAL loads (a resume point requires a watermark).
  - During EXTRACTION, the record stream is checked every
    _CHECKPOINT_TIME_CHECK_INTERVAL_RECORDS records for remaining Lambda time,
    and against the configured record cap after every record.
  - When either threshold is hit AND further source data actually remains
    (a one-record lookahead avoids a spurious checkpoint exactly at EOF),
    extraction stops early with whatever was already written. SCHEMA_SNAPSHOT,
    SCHEMA_DRIFT_EVALUATION, and RAW_WRITE all proceed normally against this
    partial dataset.
  - WATERMARK_UPDATE then commits a PARTIAL watermark advance — up to the
    last successfully written record's own watermark-field value (clamped to
    never regress before the prior watermark) — via the SAME
    advance_watermark()/initialise_watermark() methods used by a full run
    (same optimistic-concurrency, WatermarkRecord.version model; unchanged).
  - A dedicated audit record is persisted under a distinguishable run_id
    ('{run_id}-partN') via RunCoordinator.emit_checkpoint_stage(), separate
    from the main run's own stage records (still keyed by the plain run_id).
  - LambdaTimeoutWarning is then raised — a NEW exception, distinct from a
    hard failure: it is NOT routed to the DLQ and does NOT mark the circuit
    breaker as failed (see the dedicated `except LambdaTimeoutWarning: raise`
    clause in execute()).
  - What remains OUT OF SCOPE here (see infrastructure/modules/orchestration/
    main.tf's ExecuteExtraction Catch block and the comment above it): Step
    Functions does not yet automatically re-invoke extraction from the
    checkpoint. Doing so safely requires threading a resume/part-number field
    through the state machine's Parameters on retry (ASL's Catch does not by
    itself feed exception details back into a Task's input) — a state-machine
    redesign intentionally left undone rather than guessed at without live
    testing (see the module docstring note in main.tf).

Drift handling:
  - BREAKING drift: raw data written, snapshot persisted, watermark advanced.
    Downstream transformation is blocked (ExtractionWorkflowResult.transformation_blocked).
  - All other drift: pipeline proceeds normally.

Design:
  - All dependencies are injected via constructor (testable without AWS).
  - One ExtractionWorkflow instance per extraction run.
  - The RawLayerWriterProtocol is a structural Protocol — any object with
    write_partition() satisfies it; no inheritance required.

Security (OWASP A03, A07, A09):
  - No credentials, tokens, or PII flow through this module.
  - All AWS access uses IAM role credentials from the runtime environment.
  - Error messages are scrubbed by PipelineStageContract before audit logging.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, NoReturn, Protocol, runtime_checkable

import structlog

from connector_runtime.configuration_repository.configuration_repository import (
    ConfigurationRepositoryClient,
)
from connector_runtime.interfaces.connector_interface import (
    ConnectorInterface,
    ExtractionRecord,
    FieldContract,
    QueryContract,
)
from connector_runtime.run_lifecycle.run_lifecycle import RunCoordinator
from contracts.entity_configuration_contract import EntityExtractionConfig, LoadType
from contracts.observability_contract import PipelineStage, RunStatus
from contracts.pipeline_stage_contract import DriftClassification
from observability.lambda_utils import check_lambda_timeout_periodic
from observability.structured_logger import get_platform_logger
from orchestration.step_functions.extraction_retry_policy import (
    CircuitOpenError,
    ExtractionRetryPolicy,
)
from schema_management.drift_evaluation.drift_evaluator import DriftReport, SchemaDriftEvaluator
from schema_management.snapshot_repository.snapshot_repository import (
    FieldSnapshot,
    SchemaSnapshot,
    SchemaSnapshotRepository,
)
from watermark_management.watermark_repository.watermark_repository import (
    WatermarkConcurrencyError,
    WatermarkRecord,
    WatermarkRepository,
)

_logger = get_platform_logger(__name__)

# ---------------------------------------------------------------------------
# Checkpoint / partial-run tuning (PERF-5)
# ---------------------------------------------------------------------------

# How often (in records) the checkpoint-bounded iterator checks remaining
# Lambda execution time. Checking every record would call
# context.get_remaining_time_in_millis() far more often than useful; checking
# too rarely risks overshooting the time budget between checks.
_CHECKPOINT_TIME_CHECK_INTERVAL_RECORDS: Final[int] = 1_000

# Minimum Lambda time (ms) that must remain when a periodic checkpoint check
# runs. Chosen to leave enough headroom to finish writing the current chunk,
# the schema snapshot, drift evaluation, and a watermark advance — all of
# which run AFTER extraction stops — before the hard 900s Lambda timeout.
_CHECKPOINT_MIN_REMAINING_MS: Final[int] = 90_000


class LambdaTimeoutWarning(Exception):  # noqa: N818 — name mandated by PERF-5's spec
    """
    Raised when EXTRACTION stops early via a checkpoint (PERF-5) — either
    EntityExtractionConfig.max_records_per_lambda_run was reached, or Lambda
    remaining time fell below _CHECKPOINT_MIN_REMAINING_MS during a periodic
    check — rather than running to completion or failing outright.

    Non-fatal and DISTINCT from a hard pipeline failure:
      - The records written so far are valid and already durably in S3.
      - A PARTIAL watermark advance has already been committed (up to the
        last successfully written record's watermark-field value) via the
        SAME optimistic-concurrency advance_watermark()/initialise_watermark()
        methods a full run uses.
      - A dedicated audit record was persisted under a distinguishable
        '{run_id}-partN' identifier (RunCoordinator.emit_checkpoint_stage()).
      - This exception is NOT routed to the DLQ and does NOT count against
        the circuit breaker (see the dedicated except clause in execute()).

    Step Functions should catch this distinctly from `States.ALL` (which
    routes to the terminal ExtractionFailed state) and re-trigger extraction
    so it resumes from the committed partial watermark. That re-trigger wiring
    is NOT implemented here — see the module docstring's "Checkpoint /
    partial-run contract" section for exactly what remains and why.
    """

    def __init__(
        self,
        message: str,
        *,
        run_id: str,
        partial_run_id: str,
        source_id: str,
        entity_id: str,
        records_written: int,
        checkpoint_watermark: str,
        reason: str,
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.partial_run_id = partial_run_id
        self.source_id = source_id
        self.entity_id = entity_id
        self.records_written = records_written
        self.checkpoint_watermark = checkpoint_watermark
        self.reason = reason


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class RawLayerWriterProtocol(Protocol):
    """
    Structural protocol for raw-layer Parquet writers.

    All per-source raw layer writers (SalesforceRawLayerWriter,
    NetSuiteRawLayerWriter, MySqlRdsRawLayerWriter) satisfy this protocol
    without requiring inheritance.
    """

    def write_partition(
        self,
        records: list[ExtractionRecord],
        source_id: str,
        entity_id: str,
        run_id: str,
        schema_fingerprint: str,
        extraction_date: str,
    ) -> str:
        """Write records as a Parquet partition to the S3 raw layer."""
        ...

    def write_partition_streaming(
        self,
        record_iter: Iterator[ExtractionRecord],
        source_id: str,
        entity_id: str,
        run_id: str,
        schema_fingerprint: str,
        extraction_date: str,
        chunk_size: int = 50_000,
    ) -> tuple[str, int]:
        """
        Write records from a lazy iterator in memory-bounded chunks.

        Returns (partition_prefix, total_record_count).
        Peak memory is O(chunk_size) regardless of total record volume.
        """
        ...


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtractionWorkflowResult:
    """
    Immutable result returned by ExtractionWorkflow.execute().

    Passed back to Step Functions as the task output.
    """

    run_id: str
    source_id: str
    entity_id: str
    record_count: int
    schema_fingerprint: str
    raw_s3_prefix: str
    drift_classification: DriftClassification
    # StrEnum — serialises as string value via dataclasses.asdict()
    transformation_blocked: bool
    started_at: str  # ISO-8601 UTC
    completed_at: str  # ISO-8601 UTC
    partial: bool = False
    # True when watermark advance failed due to a concurrent update.
    # Extraction data was written successfully; the step function should
    # handle this as a recoverable partial run, not a DLQ candidate.


@dataclass(frozen=True)
class _CheckpointInfo:
    """
    Internal record of a mid-extraction checkpoint (PERF-5).

    Populated by _checkpoint_bounded_iter() when EXTRACTION stops early;
    consumed by execute() to commit a partial watermark advance and raise
    LambdaTimeoutWarning instead of completing normally.
    """

    records_written: int
    last_source_timestamp: str | None
    reason: str


# ---------------------------------------------------------------------------
# Stage timer helper (E-2 / F-14)
# ---------------------------------------------------------------------------


class _StageTimer:
    """Lightweight monotonic timer for per-stage duration_ms tracking."""

    def __init__(self) -> None:
        self._start = time.monotonic()

    def elapsed_ms(self) -> int:
        """Return elapsed milliseconds since construction (integer)."""
        return int((time.monotonic() - self._start) * 1_000)


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------


class ExtractionWorkflow:
    """
    Single-run extraction pipeline orchestrator.

    One instance per extraction run.  The run_coordinator carries the
    immutable run_id; create a fresh instance for each new run.

    Constructor arguments (all required):
      run_coordinator       RunCoordinator for stage emission and DLQ routing
      configuration_client  Loads EntityExtractionConfig from DynamoDB/S3
      watermark_repository  Tracks extraction window progress in DynamoDB
      snapshot_repository   Persists and loads schema snapshots from S3
      drift_evaluator       Stateless schema drift evaluator
      connector             ConnectorInterface implementation for the source
      raw_layer_writer      Writes raw Parquet files to S3

    Optional:
      retry_policy          Circuit-breaker and retry decision engine.
                            When None, circuit checks are skipped.
      lambda_context        Lambda runtime context (PERF-5). Used only to
                            check remaining execution time for the
                            max_records_per_lambda_run checkpoint path; when
                            None (e.g. local tests, or entities without a
                            checkpoint configured), only the record-count
                            threshold is enforced — no time-based check.
    """

    def __init__(
        self,
        run_coordinator: RunCoordinator,
        configuration_client: ConfigurationRepositoryClient,
        watermark_repository: WatermarkRepository,
        snapshot_repository: SchemaSnapshotRepository,
        drift_evaluator: SchemaDriftEvaluator,
        connector: ConnectorInterface,
        raw_layer_writer: RawLayerWriterProtocol,
        retry_policy: ExtractionRetryPolicy | None = None,
        chunk_size: int = 50_000,
        lambda_context: Any = None,
    ) -> None:
        self._coordinator = run_coordinator
        self._config_client = configuration_client
        self._watermark_repo = watermark_repository
        self._snapshot_repo = snapshot_repository
        self._drift_evaluator = drift_evaluator
        self._connector = connector
        self._raw_writer = raw_layer_writer
        self._retry_policy = retry_policy
        self._chunk_size = chunk_size
        self._lambda_context = lambda_context

    # ── Public API ──────────────────────────────────────────────────────────

    def execute(
        self,
        is_replay: bool = False,
        replay_of_run_id: str | None = None,
    ) -> ExtractionWorkflowResult:
        """
        Execute the full extraction pipeline for one entity run.

        Parameters
        ----------
        is_replay : bool
            True when re-running a previously-failed run from the DLQ.
            Replay runs produce a new run_id and do not regress watermarks.
        replay_of_run_id : str | None
            The original run_id being replayed (for audit lineage).
            Only relevant when is_replay=True.

        Returns
        -------
        ExtractionWorkflowResult
            Summary of the completed run.

        Raises
        ------
        CircuitOpenError
            When the circuit breaker is open for this source.
        Exception
            Re-raised when any pipeline stage fails after DLQ routing.
        """
        if is_replay and replay_of_run_id is None:
            raise ValueError(
                "replay_of_run_id must be provided when is_replay=True. "
                "Pass the original run_id for audit lineage."
            )

        source_id = self._coordinator.source_id
        entity_id = self._coordinator.entity_id
        run_id = self._coordinator.run_id
        started_at = self._coordinator.started_at

        # Bind run/entity context to every log line in this thread for the
        # duration of the run (F-13).  Cleared in the finally block below.
        structlog.contextvars.bind_contextvars(
            run_id=run_id,
            source_id=source_id,
            entity_id=entity_id,
        )

        if is_replay:
            self._coordinator.emit_stage(
                stage=PipelineStage.REPLAY_INITIATION,
                status=RunStatus.SUCCESS,
                error_message=f"Replaying run_id={replay_of_run_id}",
            )

        # Guard: check circuit breaker before incurring any AWS costs.
        if self._retry_policy is not None and self._retry_policy.is_circuit_open(source_id):
            raise CircuitOpenError(
                f"Circuit breaker is open for source_id={source_id!r}: "
                f"{self._retry_policy.consecutive_failures(source_id)} consecutive "
                f"failures meet or exceed the threshold of "
                f"{self._retry_policy.circuit_open_threshold}."
            )

        current_stage = PipelineStage.CONFIGURATION_LOAD
        try:
            # ── Stage 1: CONFIGURATION_LOAD ─────────────────────────────────
            _t1 = _StageTimer()
            config = self._stage_load_config(source_id, entity_id)
            # duration_ms is emitted inside _stage_load_config; override here to include timer
            self._coordinator.emit_stage(
                stage=PipelineStage.CONFIGURATION_LOAD,
                status=RunStatus.SUCCESS,
                duration_ms=_t1.elapsed_ms(),
            )

            # ── Stage 2: CREDENTIAL_RETRIEVAL (watermark + window) ──────────
            current_stage = PipelineStage.CREDENTIAL_RETRIEVAL
            _t2 = _StageTimer()
            watermark, lower_bound, upper_bound = self._stage_resolve_watermark(config, run_id)
            self._coordinator.emit_stage(
                stage=PipelineStage.CREDENTIAL_RETRIEVAL,
                status=RunStatus.SUCCESS,
                duration_ms=_t2.elapsed_ms(),
                extraction_window_start=lower_bound,
                extraction_window_end=upper_bound,
            )

            # ── Stage 3: METADATA_DISCOVERY ─────────────────────────────
            current_stage = PipelineStage.METADATA_DISCOVERY
            _t3 = _StageTimer()
            field_contract = self._stage_discover_fields(config)
            self._coordinator.emit_stage(
                stage=PipelineStage.METADATA_DISCOVERY,
                status=RunStatus.SUCCESS,
                duration_ms=_t3.elapsed_ms(),
            )

            # ── Stage 4: QUERY_BUILD ─────────────────────────────────────
            current_stage = PipelineStage.QUERY_BUILD
            _t4 = _StageTimer()
            query_contract = self._connector.build_extraction_query(
                field_contract=field_contract,
                load_type=config.load_type,
                watermark_field=config.watermark_field,
                watermark_lower=lower_bound.isoformat(),
                watermark_upper=upper_bound.isoformat(),
                extraction_window_days=config.extraction_window_days,
            )
            self._coordinator.emit_stage(
                stage=PipelineStage.QUERY_BUILD,
                status=RunStatus.SUCCESS,
                extraction_window_start=lower_bound,
                extraction_window_end=upper_bound,
                duration_ms=_t4.elapsed_ms(),
            )

            # ── Stage 5: EXTRACTION + RAW write ─────────────────────────────
            current_stage = PipelineStage.EXTRACTION
            _t5 = _StageTimer()
            record_count, raw_s3_prefix, checkpoint_info = self._stage_extract_and_write(
                config=config,
                field_contract=field_contract,
                query_contract=query_contract,
                run_id=run_id,
                upper_bound=upper_bound,
            )
            # Always SUCCESS here — the EXTRACTION stage itself did not error.
            # A checkpoint (PERF-5) gets its own distinct RunStatus.PARTIAL
            # audit record later via emit_checkpoint_stage(), under a
            # distinguishable run_id, once the partial watermark is committed.
            self._coordinator.emit_stage(
                stage=PipelineStage.EXTRACTION,
                status=RunStatus.SUCCESS,
                record_count=record_count,
                duration_ms=_t5.elapsed_ms(),
            )

            # ── Stage 6: SCHEMA_SNAPSHOT ────────────────────────────────────
            current_stage = PipelineStage.SCHEMA_SNAPSHOT
            _t6 = _StageTimer()
            snapshot = self._build_schema_snapshot(field_contract, record_count, upper_bound)
            snapshot_key = self._snapshot_repo.write_snapshot(
                snapshot, tenant_code=self._coordinator.tenant_code
            )
            self._coordinator.emit_stage(
                stage=PipelineStage.SCHEMA_SNAPSHOT,
                status=RunStatus.SUCCESS,
                schema_version=snapshot.schema_version,
                schema_snapshot_s3_key=snapshot_key,
                duration_ms=_t6.elapsed_ms(),
            )

            # ── Stage 7: SCHEMA_DRIFT_EVALUATION ────────────────────────
            current_stage = PipelineStage.SCHEMA_DRIFT_EVALUATION
            _t7 = _StageTimer()
            drift_report = self._stage_evaluate_drift(config, snapshot)
            self._coordinator.emit_stage(
                stage=PipelineStage.SCHEMA_DRIFT_EVALUATION,
                status=RunStatus.SUCCESS,
                duration_ms=_t7.elapsed_ms(),
            )

            # ── Stage 8: RAW_WRITE (count validation + partition audit) ────
            current_stage = PipelineStage.RAW_WRITE
            self._coordinator.emit_stage(
                stage=PipelineStage.RAW_WRITE,
                status=RunStatus.SUCCESS,
                raw_s3_prefix=raw_s3_prefix,
                record_count=record_count,
                schema_version=snapshot.schema_version,
                drift_classification=drift_report.overall_classification,
            )

            # ── Stage 9: WATERMARK_UPDATE ────────────────────────────────────
            current_stage = PipelineStage.WATERMARK_UPDATE
            concurrency_partial_result = self._stage_watermark_update_or_checkpoint(
                checkpoint_info=checkpoint_info,
                watermark=watermark,
                config=config,
                lower_bound=lower_bound,
                upper_bound=upper_bound,
                run_id=run_id,
                source_id=source_id,
                entity_id=entity_id,
                record_count=record_count,
                snapshot=snapshot,
                drift_report=drift_report,
                raw_s3_prefix=raw_s3_prefix,
                started_at=started_at,
            )
            if concurrency_partial_result is not None:
                return concurrency_partial_result

            # ── Stage 10: RUN_COMPLETION ─────────────────────────────────────
            completed_at = datetime.now(tz=UTC)
            self._coordinator.emit_stage(
                stage=PipelineStage.RUN_COMPLETION,
                status=RunStatus.SUCCESS,
                record_count=record_count,
                extraction_window_start=lower_bound,
                extraction_window_end=upper_bound,
                schema_version=snapshot.schema_version,
                drift_classification=drift_report.overall_classification,
                raw_s3_prefix=raw_s3_prefix,
                duration_ms=int((completed_at - started_at).total_seconds() * 1000),
            )

            if drift_report.is_transformation_blocked:
                _logger.warning(
                    "extraction_complete_transformation_blocked",
                    run_id=run_id,
                    source_id=source_id,
                    entity_id=entity_id,
                    drift_classification=drift_report.overall_classification,
                )
            else:
                self._coordinator.emit_stage(
                    stage=PipelineStage.TRANSFORMATION,
                    status=RunStatus.SUCCESS,
                    raw_s3_prefix=raw_s3_prefix,
                )

            if self._retry_policy is not None:
                self._retry_policy.record_success(source_id, entity_id)

            _logger.info(
                "extraction_run_completed",
                run_id=run_id,
                source_id=source_id,
                entity_id=entity_id,
                record_count=record_count,
                drift_classification=drift_report.overall_classification,
                transformation_blocked=drift_report.is_transformation_blocked,
            )

            return ExtractionWorkflowResult(
                run_id=run_id,
                source_id=source_id,
                entity_id=entity_id,
                record_count=record_count,
                schema_fingerprint=snapshot.schema_version,
                raw_s3_prefix=raw_s3_prefix,
                drift_classification=drift_report.overall_classification,
                transformation_blocked=drift_report.is_transformation_blocked,
                started_at=started_at.isoformat(),
                completed_at=completed_at.isoformat(),
            )

        except (CircuitOpenError, LambdaTimeoutWarning):
            # LambdaTimeoutWarning (PERF-5) is a non-fatal checkpoint: the
            # partial watermark advance and checkpoint audit record were
            # already committed in _commit_checkpoint_and_raise(). Like
            # CircuitOpenError, it must NOT be treated as a pipeline failure —
            # no DLQ entry, no circuit-breaker failure count.
            raise
        except Exception as exc:
            if self._retry_policy is not None:
                self._retry_policy.record_failure(source_id, entity_id)
            self._handle_stage_failure(
                exc=exc,
                failed_stage=current_stage,
                source_id=source_id,
                entity_id=entity_id,
                run_id=run_id,
            )
            raise
        finally:
            # Always clear the structlog context vars so they do not leak into
            # subsequent runs that reuse the same thread (e.g. Lambda container reuse).
            structlog.contextvars.clear_contextvars()

    # ── Private stage methods ───────────────────────────────────────────────

    def _stage_load_config(self, source_id: str, entity_id: str) -> EntityExtractionConfig:
        """Load entity extraction config. Stage emission handled by execute() with duration_ms."""
        return self._config_client.load_config(
            source_id=source_id,
            entity_id=entity_id,
            tenant_code=self._coordinator.tenant_code,
        )

    def _stage_resolve_watermark(
        self,
        config: EntityExtractionConfig,
        run_id: str,
    ) -> tuple[WatermarkRecord | None, datetime, datetime]:
        """Retrieve the current watermark and compute the extraction window.
        Stage emission is handled by execute() with duration_ms.
        """
        reference_time = datetime.now(tz=UTC)
        watermark = self._watermark_repo.get_watermark(
            source_id=config.source_id,
            entity_id=config.entity_id,
            tenant_code=self._coordinator.tenant_code,
        )
        lower_bound, upper_bound = WatermarkRepository.compute_extraction_window(
            watermark=watermark,
            config=config,
            reference_time=reference_time,
        )
        return watermark, lower_bound, upper_bound

    def _stage_discover_fields(self, config: EntityExtractionConfig) -> FieldContract:
        """Discover queryable fields from the source.
        Stage emission is handled by execute() with duration_ms.
        """
        return self._connector.discover_queryable_fields(
            source_id=config.source_id,
            entity_id=config.entity_id,
            field_mode=config.field_mode,
            include_fields=list(config.include_fields),
            exclude_fields=list(config.exclude_fields),
        )

    def _stage_extract_and_write(
        self,
        config: EntityExtractionConfig,
        field_contract: FieldContract,
        query_contract: QueryContract,
        run_id: str,
        upper_bound: datetime,
    ) -> tuple[int, str, _CheckpointInfo | None]:
        """Stream records from the connector and write raw Parquet to S3 in chunks.
        Stage emission is handled by execute() with duration_ms.

        When config.max_records_per_lambda_run is set AND load_type is
        INCREMENTAL, the record stream is wrapped so extraction can stop
        early via a checkpoint (PERF-5) — see _checkpoint_bounded_iter().
        Returns (record_count, raw_s3_prefix, checkpoint_info); checkpoint_info
        is None unless a checkpoint actually fired.
        """
        record_iter: Iterator[ExtractionRecord] = self._connector.execute_extraction(
            query_contract, run_id=run_id
        )

        checkpoint_holder: list[_CheckpointInfo] = []
        if config.max_records_per_lambda_run is not None:
            if config.load_type == LoadType.INCREMENTAL:
                record_iter = self._checkpoint_bounded_iter(
                    record_iter, config.max_records_per_lambda_run, checkpoint_holder
                )
            else:
                # A checkpoint's resume point is the watermark — full loads
                # have no watermark to resume from, so the cap is not
                # meaningful here. Ignore it rather than silently truncating
                # a full load's output.
                _logger.warning(
                    "extraction_checkpoint_ignored_for_full_load",
                    source_id=config.source_id,
                    entity_id=config.entity_id,
                    run_id=run_id,
                )

        extraction_date = upper_bound.strftime("%Y-%m-%d")
        raw_s3_prefix, record_count = self._raw_writer.write_partition_streaming(
            record_iter=record_iter,
            source_id=config.source_id,
            entity_id=config.entity_id,
            run_id=run_id,
            schema_fingerprint=field_contract.schema_fingerprint,
            extraction_date=extraction_date,
            chunk_size=self._chunk_size,
        )
        checkpoint_info = checkpoint_holder[0] if checkpoint_holder else None
        return record_count, raw_s3_prefix, checkpoint_info

    def _checkpoint_bounded_iter(
        self,
        record_iter: Iterator[ExtractionRecord],
        max_records: int,
        checkpoint_holder: list[_CheckpointInfo],
    ) -> Iterator[ExtractionRecord]:
        """
        Wrap a connector's record iterator to stop early on a checkpoint
        (PERF-5), without any raw-layer-writer or connector changes: the
        writer simply sees the iterator end, exactly as if extraction had
        naturally finished.

        Checked on every record: has max_records been reached?
        Checked every _CHECKPOINT_TIME_CHECK_INTERVAL_RECORDS records: is
        remaining Lambda time below _CHECKPOINT_MIN_REMAINING_MS?

        A one-record lookahead on the max_records boundary avoids a spurious
        checkpoint when the source happens to be exhausted at exactly that
        count — in that case this is a normal completion, not a checkpoint.
        The lookahead record itself is intentionally NOT yielded when a
        checkpoint DOES fire: it is not lost, since the resumed run's
        watermark lower-bound (plus overlap) will naturally re-fetch it.

        On firing, appends a _CheckpointInfo to checkpoint_holder (a
        single-element list used as a mutable out-parameter, since a
        generator cannot both yield values and return extra data to its
        caller) and stops yielding.
        """
        count = 0
        last_source_timestamp: str | None = None
        for record in record_iter:
            count += 1
            if record.source_timestamp is not None:
                last_source_timestamp = record.source_timestamp
            yield record

            if count >= max_records:
                sentinel = object()
                lookahead = next(record_iter, sentinel)
                if lookahead is sentinel:
                    return  # exhausted naturally at exactly the cap — not a checkpoint
                checkpoint_holder.append(
                    _CheckpointInfo(
                        records_written=count,
                        last_source_timestamp=last_source_timestamp,
                        reason=f"max_records_per_lambda_run ({max_records}) reached",
                    )
                )
                return

            if count % _CHECKPOINT_TIME_CHECK_INTERVAL_RECORDS == 0:
                try:
                    check_lambda_timeout_periodic(
                        self._lambda_context,
                        min_remaining_ms=_CHECKPOINT_MIN_REMAINING_MS,
                        operation_name="extraction_checkpoint",
                    )
                except RuntimeError:
                    checkpoint_holder.append(
                        _CheckpointInfo(
                            records_written=count,
                            last_source_timestamp=last_source_timestamp,
                            reason="Lambda remaining time below checkpoint threshold",
                        )
                    )
                    return

    def _build_schema_snapshot(
        self,
        field_contract: FieldContract,
        record_count: int,
        upper_bound: datetime,
    ) -> SchemaSnapshot:
        """Build a SchemaSnapshot value object from the discovered FieldContract."""
        fields = tuple(
            FieldSnapshot(
                name=f.name,
                data_type=f.data_type,
                is_nullable=f.is_nullable,
                is_queryable=f.is_queryable,
                length=f.length,
                precision=f.precision,
                scale=f.scale,
                is_custom=f.is_custom,
            )
            for f in field_contract.fields
        )
        return SchemaSnapshot(
            source_id=field_contract.source_id,
            entity_id=field_contract.entity_id,
            schema_version=field_contract.schema_fingerprint,
            extraction_date=upper_bound.strftime("%Y-%m-%d"),
            captured_at=datetime.now(tz=UTC).isoformat(),
            fields=fields,
            record_count=record_count,
        )

    def _stage_evaluate_drift(
        self,
        config: EntityExtractionConfig,
        current_snapshot: SchemaSnapshot,
    ) -> DriftReport:
        """Load previous snapshot, evaluate drift, and persist the drift report."""
        previous_snapshot = self._snapshot_repo.load_latest_snapshot(
            source_id=config.source_id,
            entity_id=config.entity_id,
            tenant_code=self._coordinator.tenant_code,
        )
        drift_report = self._drift_evaluator.evaluate(
            current=current_snapshot,
            previous=previous_snapshot,
        )
        self._snapshot_repo.write_drift_report(
            source_id=config.source_id,
            entity_id=config.entity_id,
            schema_version=current_snapshot.schema_version,
            extraction_date=current_snapshot.extraction_date,
            tenant_code=self._coordinator.tenant_code,
            report_json=drift_report.to_json(),
        )
        self._coordinator.emit_stage(
            stage=PipelineStage.SCHEMA_DRIFT_EVALUATION,
            status=RunStatus.SUCCESS,
            drift_classification=drift_report.overall_classification,
            schema_version=current_snapshot.schema_version,
        )
        return drift_report

    def _stage_watermark_update_or_checkpoint(
        self,
        checkpoint_info: _CheckpointInfo | None,
        watermark: WatermarkRecord | None,
        config: EntityExtractionConfig,
        lower_bound: datetime,
        upper_bound: datetime,
        run_id: str,
        source_id: str,
        entity_id: str,
        record_count: int,
        snapshot: SchemaSnapshot,
        drift_report: DriftReport,
        raw_s3_prefix: str,
        started_at: datetime,
    ) -> ExtractionWorkflowResult | None:
        """
        Stage 9 (WATERMARK_UPDATE) dispatcher, extracted out of execute() to
        keep its cyclomatic complexity in check.

        Three possible outcomes:
          - Checkpoint fired (PERF-5): commits a partial watermark advance and
            raises LambdaTimeoutWarning (NoReturn — never returns here).
          - Watermark concurrency conflict: returns a partial
            ExtractionWorkflowResult for execute() to return immediately
            (pre-existing behaviour, unchanged).
          - Normal advance: returns None — execute() proceeds to stage 10.
        """
        if checkpoint_info is not None:
            self._commit_checkpoint_and_raise(
                checkpoint_info=checkpoint_info,
                watermark=watermark,
                config=config,
                lower_bound=lower_bound,
                upper_bound=upper_bound,
                run_id=run_id,
                source_id=source_id,
                entity_id=entity_id,
                record_count=record_count,
            )

        try:
            self._stage_advance_watermark(watermark, config, upper_bound, run_id)
        except WatermarkConcurrencyError as wce:
            # Another Lambda instance advanced the watermark concurrently.
            # This is not a data-loss event; the record was written successfully.
            # Return PARTIAL status rather than routing to the DLQ.
            _logger.warning(
                "watermark_concurrency_conflict_partial_success",
                run_id=run_id,
                source_id=source_id,
                entity_id=entity_id,
                detail=str(wce),
            )
            return ExtractionWorkflowResult(
                run_id=run_id,
                source_id=source_id,
                entity_id=entity_id,
                record_count=record_count,
                schema_fingerprint=snapshot.schema_version,
                raw_s3_prefix=raw_s3_prefix,
                drift_classification=drift_report.overall_classification,
                transformation_blocked=drift_report.is_transformation_blocked,
                started_at=started_at.isoformat(),
                completed_at=datetime.now(tz=UTC).isoformat(),
                partial=True,
            )
        return None

    def _stage_advance_watermark(
        self,
        watermark: WatermarkRecord | None,
        config: EntityExtractionConfig,
        upper_bound: datetime,
        run_id: str,
    ) -> None:
        """Initialise or advance the watermark on successful extraction."""
        if watermark is None:
            self._watermark_repo.initialise_watermark(
                source_id=config.source_id,
                entity_id=config.entity_id,
                upper_watermark=upper_bound,
                run_id=run_id,
                tenant_code=self._coordinator.tenant_code,
            )
        else:
            self._watermark_repo.advance_watermark(
                current=watermark,
                new_upper_watermark=upper_bound,
                run_id=run_id,
            )
        self._coordinator.emit_stage(
            stage=PipelineStage.WATERMARK_UPDATE,
            status=RunStatus.SUCCESS,
            extraction_window_end=upper_bound,
        )

    def _commit_checkpoint_and_raise(
        self,
        checkpoint_info: _CheckpointInfo,
        watermark: WatermarkRecord | None,
        config: EntityExtractionConfig,
        lower_bound: datetime,
        upper_bound: datetime,
        run_id: str,
        source_id: str,
        entity_id: str,
        record_count: int,
    ) -> NoReturn:
        """
        Commit a PARTIAL watermark advance for a checkpointed EXTRACTION run
        and raise LambdaTimeoutWarning (PERF-5). Always raises — never returns.

        The partial upper bound is the last successfully written record's own
        watermark-field value (parsed from ExtractionRecord.source_timestamp),
        clamped to never regress before the prior watermark's own upper bound
        — advance_watermark() rejects a regression, and the checkpoint's
        overlap-window records can legitimately have a watermark value below
        the prior run's upper bound.
        """
        if checkpoint_info.last_source_timestamp is not None:
            try:
                partial_upper_bound = datetime.fromisoformat(
                    checkpoint_info.last_source_timestamp
                )
                if partial_upper_bound.tzinfo is None:
                    partial_upper_bound = partial_upper_bound.replace(tzinfo=UTC)
            except ValueError:
                # Watermark field value was not a parseable ISO-8601 timestamp
                # (should not happen for a well-formed watermark_field, but a
                # checkpoint must never crash on this) — fall back to the
                # window's lower bound, the most conservative safe choice.
                partial_upper_bound = lower_bound
        else:
            # No record carried a watermark-field value at all — fall back to
            # the window's lower bound rather than falsely advancing to the
            # full upper_bound (which would incorrectly claim records beyond
            # the checkpoint were processed).
            partial_upper_bound = lower_bound

        if watermark is not None and partial_upper_bound < watermark.upper_watermark:
            partial_upper_bound = watermark.upper_watermark

        try:
            self._stage_advance_watermark(watermark, config, partial_upper_bound, run_id)
        except WatermarkConcurrencyError as wce:
            # Another invocation already advanced the watermark past this
            # point — the checkpoint's own partial progress is superseded,
            # not lost. Proceed to raise LambdaTimeoutWarning regardless: the
            # data already written to S3 for THIS run remains valid, and a
            # resumed run will naturally pick up from whichever watermark
            # value is currently stored (unaffected by this race).
            _logger.warning(
                "checkpoint_watermark_concurrency_conflict",
                run_id=run_id,
                source_id=source_id,
                entity_id=entity_id,
                detail=str(wce),
            )

        part_number = 1  # see module docstring: multi-checkpoint numbering
        # requires threading a resume/part counter through Step Functions,
        # which is out of scope here (documented, not guessed at).
        partial_run_id = self._coordinator.emit_checkpoint_stage(
            part_number=part_number,
            record_count=checkpoint_info.records_written,
            extraction_window_end=partial_upper_bound,
            error_message=checkpoint_info.reason,
        ).run_id

        _logger.warning(
            "extraction_checkpoint_triggered",
            run_id=run_id,
            partial_run_id=partial_run_id,
            source_id=source_id,
            entity_id=entity_id,
            records_written=checkpoint_info.records_written,
            total_records_this_invocation=record_count,
            checkpoint_watermark=partial_upper_bound.isoformat(),
            reason=checkpoint_info.reason,
        )

        raise LambdaTimeoutWarning(
            f"Extraction for source_id={source_id!r} entity_id={entity_id!r} "
            f"stopped at a checkpoint after {checkpoint_info.records_written} records "
            f"({checkpoint_info.reason}). A partial watermark advance to "
            f"{partial_upper_bound.isoformat()} was committed. Re-trigger extraction "
            "to resume the remaining window.",
            run_id=run_id,
            partial_run_id=partial_run_id,
            source_id=source_id,
            entity_id=entity_id,
            records_written=checkpoint_info.records_written,
            checkpoint_watermark=partial_upper_bound.isoformat(),
            reason=checkpoint_info.reason,
        )

    def _handle_stage_failure(
        self,
        exc: Exception,
        failed_stage: PipelineStage,
        source_id: str,
        entity_id: str,
        run_id: str,
    ) -> None:
        """Emit a FAILURE stage contract and route the failure to the DLQ."""
        error_classification = self._connector.classify_extraction_error(exc)
        error_message = str(exc)

        _logger.error(
            "extraction_pipeline_stage_failed",
            run_id=run_id,
            source_id=source_id,
            entity_id=entity_id,
            failed_stage=str(failed_stage),
            error_classification=str(error_classification),
        )

        self._coordinator.emit_stage(
            stage=failed_stage,
            status=RunStatus.FAILED,
            error_message=error_message,
            error_code=str(error_classification),
        )
        self._coordinator.enqueue_dlq_entry(
            error_message=error_message,
            error_code=str(error_classification),
            failed_stage=failed_stage,
        )
        self._coordinator.emit_stage(
            stage=PipelineStage.DLQ_ENQUEUE,
            status=RunStatus.SUCCESS,
        )
