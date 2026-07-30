"""
Run lifecycle coordinator for the Enterprise Data Lake platform.

The RunCoordinator manages the full lifecycle of a single extraction run:
  1. Generates an immutable run_id (UUID + timestamp) on construction
  2. Emits a PipelineStageContract at each stage boundary
  3. Persists each emitted contract to the DynamoDB run audit log
  4. Routes terminal failures to the SQS DLQ

run_id format: run-{YYYYMMDD-HHMMSSffffff}-{uuid4_8hex}
Example:       run-20260611-143022123456-a3f9c1d2

The run_id satisfies all platform invariants:
  - NOT a sequential integer
  - Contains a timestamp component (sortable and auditable)
  - Contains a UUID component (collision-resistant)
  - Matches the stable identifier format regex used by StructuredLogEvent

AWS resources used:
  - DynamoDB table: <prefix>-run-audit-log-<env>  (PK: run_id, SK: stage)
  - SQS queue:      datalake-extraction-failure-dlq-dev

Security:
  - Sensitive content is auto-scrubbed by PipelineStageContract validators.
  - SQS messages contain only metadata (no field values, no credentials).
  - DynamoDB and SQS clients use the IAM extraction_runtime role.
  - Audit log write failures are logged as warnings but never propagate —
    metric emission and audit logging must not fail an extraction run.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import boto3
from botocore.exceptions import ClientError

from contracts.dlq_routing import (
    DlqStage,
    dlq_queue_name,
    dlq_stage_for,
    legacy_extraction_dlq_name,
)
from contracts.observability_contract import PipelineStage, RunStatus, scrub_sensitive_values
from contracts.pipeline_stage_contract import DriftClassification, PipelineStageContract
from contracts.platform_metrics import PlatformMetric
from observability.lambda_runtime import require_env
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)


def generate_run_id() -> str:
    """
    Generate an immutable, collision-resistant run identifier.

    Format:  run-{YYYYMMDD-HHMMSSffffff}-{uuid4_8hex}
    Example: run-20260611-143022123456-a3f9c1d2

    The run_id is NOT a sequential integer (validated by StructuredLogEvent).
    The timestamp component makes runs auditable and time-sortable.
    The UUID hex component provides collision resistance within the same
    microsecond (e.g. concurrent Lambda invocations).
    """
    now = datetime.now(tz=UTC)
    timestamp_part = now.strftime("%Y%m%d-%H%M%S%f")
    uuid_part = uuid.uuid4().hex[:8]
    return f"run-{timestamp_part}-{uuid_part}"


def make_partial_run_id(run_id: str, part_number: int) -> str:
    """
    Derive a distinguishable identifier for a PARTIAL (checkpointed) run
    (PERF-5).

    Format:  {run_id}-part{part_number}
    Example: run-20260611-143022123456-a3f9c1d2-part1

    Used exclusively as the audit-log PK for a checkpoint's own stage record
    (see RunCoordinator.emit_checkpoint_stage) — it is NOT a substitute for
    the underlying run_id, which continues to identify the raw S3 partition
    and every other stage's audit record. Keeping the checkpoint's audit
    entry under a distinct PK makes it possible to tell, from the audit log
    alone, that a given run stopped early via a checkpoint rather than
    completing or failing outright.

    Raises:
        ValueError: part_number is not a positive integer.
    """
    if part_number < 1:
        raise ValueError(f"part_number must be >= 1, got {part_number}.")
    return f"{run_id}-part{part_number}"


class RunCoordinator:
    """
    Coordinates the full lifecycle of a single extraction run.

    One RunCoordinator instance per extraction run.  The run_id is generated
    at construction time and is immutable for the lifetime of this object.

    Usage pattern::

        coordinator = RunCoordinator(
            environment="dev",
            region_name="us-east-1",
            source_id="salesforce",
            entity_id="salesforce-account",
        )

        # At each stage boundary:
        contract = coordinator.emit_stage(
            stage=PipelineStage.CONFIGURATION_LOAD,
            status=RunStatus.SUCCESS,
            duration_ms=42,
        )

        # On terminal failure:
        coordinator.enqueue_dlq_entry(
            error_message="Credential refresh failed",
            error_code="deterministic_invalid_credentials",
            failed_stage=PipelineStage.CREDENTIAL_RETRIEVAL,
        )
    """

    def __init__(
        self,
        environment: str,
        region_name: str,
        source_id: str,
        entity_id: str,
        tenant_code: str = "demo",
    ) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        from contracts.identifier_policy import validate_tenant_code

        self._environment = environment
        self._source_id = source_id
        self._entity_id = entity_id
        self._tenant_code = validate_tenant_code(tenant_code)
        self._run_id: str = generate_run_id()
        self._started_at: datetime = datetime.now(tz=UTC)

        dynamodb = boto3.resource("dynamodb", region_name=region_name)
        self._audit_table = dynamodb.Table(require_env("AUDIT_LOG_TABLE"))
        self._sqs = boto3.client("sqs", region_name=region_name)
        self._region = region_name
        self._dlq_urls: dict[str, str] = {}

    @property
    def run_id(self) -> str:
        """The immutable run identifier generated for this run."""
        return self._run_id

    @property
    def started_at(self) -> datetime:
        """UTC datetime when this coordinator was initialised."""
        return self._started_at

    @property
    def source_id(self) -> str:
        """The stable source identifier for this run."""
        return self._source_id

    @property
    def entity_id(self) -> str:
        """The stable entity identifier for this run."""
        return self._entity_id

    @property
    def tenant_code(self) -> str:
        """The tenant identifier slug for this run (§1.1)."""
        return self._tenant_code

    def emit_stage(
        self,
        stage: PipelineStage,
        status: RunStatus,
        duration_ms: int = 0,
        extraction_window_start: datetime | None = None,
        extraction_window_end: datetime | None = None,
        schema_version: str | None = None,
        drift_classification: DriftClassification | None = None,
        raw_s3_prefix: str | None = None,
        schema_snapshot_s3_key: str | None = None,
        record_count: int | None = None,
        failed_record_count: int | None = None,
        error_message: str | None = None,
        error_code: str | None = None,
    ) -> PipelineStageContract:
        """
        Emit a PipelineStageContract for a stage boundary.

        The contract is persisted to the DynamoDB run audit log (best-effort —
        an audit write failure is logged as a warning but never propagates).

        Returns the emitted contract so the caller can pass it as a Step
        Functions task output.
        """
        contract = PipelineStageContract(
            run_id=self._run_id,
            source_id=self._source_id,
            entity_id=self._entity_id,
            stage=stage,
            status=status,
            environment=self._environment,
            tenant_code=self._tenant_code,
            duration_ms=duration_ms,
            extraction_window_start=extraction_window_start,
            extraction_window_end=extraction_window_end,
            schema_version=schema_version,
            drift_classification=drift_classification,
            raw_s3_prefix=raw_s3_prefix,
            schema_snapshot_s3_key=schema_snapshot_s3_key,
            record_count=record_count,
            failed_record_count=failed_record_count,
            error_message=error_message,
            error_code=error_code,
        )
        if status is RunStatus.RETRYING:
            record_platform_metric(
                PlatformMetric.STAGE_RETRIES, 1.0, Stage=str(stage), EntityId=self._entity_id
            )
        if stage is PipelineStage.RUN_COMPLETION and status is RunStatus.SUCCESS:
            record_platform_metric(
                PlatformMetric.PIPELINE_FRESHNESS_SECONDS,
                max(0.0, (datetime.now(tz=UTC) - self._started_at).total_seconds()),
                EntityId=self._entity_id,
            )
        self._persist_audit_record(contract)
        return contract

    def emit_checkpoint_stage(
        self,
        part_number: int,
        record_count: int,
        extraction_window_end: datetime | None = None,
        error_message: str | None = None,
    ) -> PipelineStageContract:
        """
        Emit a PARTIAL run-completion audit record for an EXTRACTION run that
        stopped early via a checkpoint (max_records_per_lambda_run reached, or
        Lambda remaining time ran low) rather than completing or failing
        outright (PERF-5).

        Persisted under a DISTINGUISHABLE run_id — '{run_id}-partN' — so the
        audit log can tell a checkpointed run apart from the main run's own
        stage records (which are still emitted under the unsuffixed run_id,
        unchanged). status is always RunStatus.PARTIAL.

        This does not itself advance the watermark or raise any exception —
        the caller (ExtractionWorkflow) is responsible for committing the
        partial watermark advance via the existing advance_watermark /
        initialise_watermark methods, and for raising LambdaTimeoutWarning.

        Returns the emitted contract (persisted best-effort, same as emit_stage).
        """
        partial_run_id = make_partial_run_id(self._run_id, part_number)
        contract = PipelineStageContract(
            run_id=partial_run_id,
            source_id=self._source_id,
            entity_id=self._entity_id,
            stage=PipelineStage.RUN_COMPLETION,
            status=RunStatus.PARTIAL,
            environment=self._environment,
            tenant_code=self._tenant_code,
            extraction_window_end=extraction_window_end,
            record_count=record_count,
            error_message=error_message,
        )
        self._persist_audit_record(contract)
        return contract

    def enqueue_dlq_entry(
        self,
        error_message: str,
        error_code: str,
        failed_stage: PipelineStage,
    ) -> None:
        """
        Route a terminal failure to the failed stage's own DLQ.

        `failed_stage` was accepted and then ignored: the queue name was hardcoded to
        `datalake-extraction-failure-dlq-dev`, so the five non-extraction stages had nowhere to
        enqueue and the
        nine per-stage queues had no producer. The argument already carried the routing; only the
        lookup was missing (see `contracts/dlq_routing.py`).

        The DLQ message body contains only run metadata and error code — no field values,
        credentials, or PII. Message content is governed by PipelineStageContract validators
        (auto-scrubbed).

        Failures to resolve a queue URL or send the message are logged as errors but do not
        propagate — the run has already failed, and raising here would mask the original error.
        """
        dlq_stage = dlq_stage_for(failed_stage)
        if dlq_stage is DlqStage.NOT_REPLAYABLE:
            _logger.info(
                "dlq_enqueue_skipped_not_replayable",
                run_id=self._run_id,
                failed_stage=str(failed_stage),
            )
            return

        payload: dict[str, Any] = {
            "run_id": self._run_id,
            "source_id": self._source_id,
            "entity_id": self._entity_id,
            "environment": self._environment,
            "tenant_code": self._tenant_code,
            "failed_stage": str(failed_stage),
            "dlq_stage": dlq_stage.value,
            "error_code": error_code,
            "error_message": scrub_sensitive_values(error_message),
            "enqueued_at": datetime.now(tz=UTC).isoformat(),
        }

        queue_names = [dlq_queue_name(dlq_stage, self._environment)]
        if dlq_stage is DlqStage.EXTRACTION:
            queue_names.append(legacy_extraction_dlq_name(self._environment))

        delivered = 0
        for queue_name in queue_names:
            dlq_url = self._resolve_dlq_url(queue_name)
            if dlq_url is None:
                _logger.error(
                    "dlq_url_resolution_failed",
                    run_id=self._run_id,
                    source_id=self._source_id,
                    entity_id=self._entity_id,
                    failed_stage=str(failed_stage),
                    queue_name=queue_name,
                )
                continue
            try:
                self._sqs.send_message(
                    QueueUrl=dlq_url, MessageBody=json.dumps(payload, separators=(",", ":"))
                )
                delivered += 1
            except ClientError:
                _logger.error(
                    "dlq_enqueue_failed",
                    run_id=self._run_id,
                    source_id=self._source_id,
                    entity_id=self._entity_id,
                    failed_stage=str(failed_stage),
                    queue_name=queue_name,
                )
        record_platform_metric(
            PlatformMetric.DLQ_MESSAGES_ENQUEUED, float(delivered), Stage=dlq_stage.value
        )

    def _persist_audit_record(self, contract: PipelineStageContract) -> None:
        """Write the stage contract to DynamoDB (best-effort — never propagates)."""
        try:
            self._audit_table.put_item(
                Item=_serialise_contract(contract, started_at=self._started_at)
            )
        except ClientError:
            _logger.warning(
                "audit_log_write_failed",
                run_id=self._run_id,
                source_id=self._source_id,
                entity_id=self._entity_id,
                stage=str(contract.stage),
            )

    def _resolve_dlq_url(self, queue_name: str) -> str | None:
        """Cached per queue name: one coordinator can route to more than one stage's queue."""
        cached = self._dlq_urls.get(queue_name)
        if cached is not None:
            return cached
        try:
            url = str(self._sqs.get_queue_url(QueueName=queue_name)["QueueUrl"])
        except ClientError:
            return None
        self._dlq_urls[queue_name] = url
        return url


def _serialise_contract(contract: PipelineStageContract, started_at: datetime) -> dict[str, Any]:
    """
    Convert a PipelineStageContract to a DynamoDB-compatible item dict.

    Args:
        contract:   The stage contract being persisted.
        started_at: The owning RunCoordinator's run-start timestamp — NOT
            necessarily contract.completed_at. Populating source_entity_key /
            started_at here (ARCH-18, pre-go-live fix) is what makes the
            source-entity-time-index GSI actually cover every run: before
            this fix, only DLQ-routed failures fed the GSI (dlq_processor_handler
            writes its own item with these attributes), so a query for a
            source/entity's full run history silently omitted every
            successful run.
    """

    def _dt(v: datetime | None) -> str | None:
        return v.isoformat() if v is not None else None

    return {
        "run_id": contract.run_id,
        "stage": str(contract.stage),  # composite SK: run_id + stage for uniqueness
        "source_id": contract.source_id,
        "entity_id": contract.entity_id,
        "status": str(contract.status),
        "environment": contract.environment,
        "tenant_code": contract.tenant_code,
        "source_entity_key": f"{contract.tenant_code}#{contract.source_id}#{contract.entity_id}",
        "started_at": started_at.isoformat(),  # GSI range key
        "completed_at": _dt(contract.completed_at),
        "duration_ms": contract.duration_ms,
        "extraction_window_start": _dt(contract.extraction_window_start),
        "extraction_window_end": _dt(contract.extraction_window_end),
        "schema_version": contract.schema_version,
        "drift_classification": (
            str(contract.drift_classification)
            if contract.drift_classification is not None
            else None
        ),
        "raw_s3_prefix": contract.raw_s3_prefix,
        "schema_snapshot_s3_key": contract.schema_snapshot_s3_key,
        "record_count": contract.record_count,
        "failed_record_count": contract.failed_record_count,
        "error_message": contract.error_message,
        "error_code": contract.error_code,
    }
