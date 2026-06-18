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
  - DynamoDB table: {environment}-run-audit-log  (PK: run_id, SK: stage)
  - SQS queue:      {environment}-extraction-dlq

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
from typing import Any, Final

import boto3
from botocore.exceptions import ClientError

from contracts.observability_contract import PipelineStage, RunStatus, scrub_sensitive_values
from contracts.pipeline_stage_contract import DriftClassification, PipelineStageContract
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

_AUDIT_TABLE_TEMPLATE: Final[str] = "{environment}-run-audit-log"
_DLQ_NAME_TEMPLATE: Final[str] = "{environment}-extraction-dlq"


# ---------------------------------------------------------------------------
# run_id generator
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


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
    ) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        self._environment = environment
        self._source_id = source_id
        self._entity_id = entity_id
        self._run_id: str = generate_run_id()
        self._started_at: datetime = datetime.now(tz=UTC)

        dynamodb = boto3.resource("dynamodb", region_name=region_name)
        self._audit_table = dynamodb.Table(_AUDIT_TABLE_TEMPLATE.format(environment=environment))
        self._sqs = boto3.client("sqs", region_name=region_name)
        self._region = region_name
        # DLQ URL cached after first successful resolution to avoid redundant API calls.
        self._dlq_url: str | None = None

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

    # ── Stage emission ─────────────────────────────────────────────────────────

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
        self._persist_audit_record(contract)
        return contract

    # ── DLQ routing ────────────────────────────────────────────────────────────

    def enqueue_dlq_entry(
        self,
        error_message: str,
        error_code: str,
        failed_stage: PipelineStage,
    ) -> None:
        """
        Route a terminal failure entry to the SQS DLQ.

        The DLQ message body contains only run metadata and error code — no
        field values, credentials, or PII.  Message content is governed by
        PipelineStageContract validators (auto-scrubbed).

        Failures to resolve the DLQ URL or send the SQS message are logged
        as errors but do not propagate — the extraction run has already failed
        and raising here would mask the original error.
        """
        dlq_url = self._resolve_dlq_url()
        if dlq_url is None:
            _logger.error(
                "dlq_url_resolution_failed",
                run_id=self._run_id,
                source_id=self._source_id,
                entity_id=self._entity_id,
                failed_stage=str(failed_stage),
            )
            return

        payload: dict[str, Any] = {
            "run_id": self._run_id,
            "source_id": self._source_id,
            "entity_id": self._entity_id,
            "environment": self._environment,
            "failed_stage": str(failed_stage),
            "error_code": error_code,
            # Scrub before enqueue — the payload bypasses PipelineStageContract
            # validators, so sensitive patterns must be removed here explicitly.
            "error_message": scrub_sensitive_values(error_message),
            "enqueued_at": datetime.now(tz=UTC).isoformat(),
        }
        try:
            self._sqs.send_message(
                QueueUrl=dlq_url,
                MessageBody=json.dumps(payload, separators=(",", ":")),
            )
        except ClientError:
            _logger.error(
                "dlq_enqueue_failed",
                run_id=self._run_id,
                source_id=self._source_id,
                entity_id=self._entity_id,
                failed_stage=str(failed_stage),
            )

    # ── Private ────────────────────────────────────────────────────────────────

    def _persist_audit_record(self, contract: PipelineStageContract) -> None:
        """Write the stage contract to DynamoDB (best-effort — never propagates)."""
        try:
            self._audit_table.put_item(Item=_serialise_contract(contract))
        except ClientError:
            _logger.warning(
                "audit_log_write_failed",
                run_id=self._run_id,
                source_id=self._source_id,
                entity_id=self._entity_id,
                stage=str(contract.stage),
            )

    def _resolve_dlq_url(self) -> str | None:
        if self._dlq_url is not None:
            return self._dlq_url
        dlq_name = _DLQ_NAME_TEMPLATE.format(environment=self._environment)
        try:
            response = self._sqs.get_queue_url(QueueName=dlq_name)
            self._dlq_url = response["QueueUrl"]
            return self._dlq_url
        except ClientError:
            return None


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------


def _serialise_contract(contract: PipelineStageContract) -> dict[str, Any]:
    """Convert a PipelineStageContract to a DynamoDB-compatible item dict."""

    def _dt(v: datetime | None) -> str | None:
        return v.isoformat() if v is not None else None

    return {
        "run_id": contract.run_id,
        "stage": str(contract.stage),  # composite SK: run_id + stage for uniqueness
        "source_id": contract.source_id,
        "entity_id": contract.entity_id,
        "status": str(contract.status),
        "environment": contract.environment,
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
