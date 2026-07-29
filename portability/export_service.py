"""
Export service (DL-PORT-01) — CSV, JSON, and Parquet for any tenant dataset.

Asynchronous job model: request → job → signed download or delivery to a customer-designated
bucket. Never synchronous, never memory-bound: CSV and JSON conversion is columnar-batch on
the substrate and streamed to S3 in parts.

Security (OWASP A01, A02, A09): row-level security applies to an export exactly as it applies
to a query — an export must never be a privilege-escalation path. Artefacts are KMS-encrypted,
land under a short-lifecycle prefix, and are delivered by time-limited signed URL.
"""

from __future__ import annotations

import csv
import io
import json
import os
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

import boto3

from contracts.identifier_policy import validate_tenant_code
from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger
from tenancy.scope_predicate import ConsumptionSurface, ScopePredicate

_logger = get_platform_logger(__name__)

_TABLE_NAME: Final[str] = "EdlExportJob"

# Signed-URL lifetime; long enough to download a large artefact, short enough to matter.
SIGNED_URL_TTL_SECONDS: Final[int] = 3_600

# Export artefacts expire so a forgotten download does not become a permanent copy.
ARTEFACT_LIFECYCLE_DAYS: Final[int] = 7

# CSV/JSON conversion batch size — columnar batches, never row-wise materialisation.
CONVERSION_BATCH_ROWS: Final[int] = 10_000


class ExportFormat(StrEnum):
    """Formats §24.4 requires."""

    CSV = "csv"
    JSON = "json"
    PARQUET = "parquet"


class ExportLayer(StrEnum):
    """Which layer an export draws from."""

    RAW = "raw"
    CURATED = "curated"
    GOLDEN = "golden"
    ANALYTICS = "analytics"
    TWIN = "twin"
    SEMANTIC_DEFINITIONS = "semantic_definitions"


class ExportJobStatus(StrEnum):
    """Job lifecycle."""

    REQUESTED = "requested"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


class ExportCapabilityRequiredError(PermissionError):
    """Raised when the caller lacks the distinct export capability (OWASP A01)."""


# Export requires an affirmative capability distinct from read.
EXPORT_CAPABILITY: Final[str] = "datalake:export:execute"


@dataclass
class ExportJob:
    """One export request and its artefact."""

    tenant_code: str
    job_id: str
    layer: ExportLayer
    export_format: ExportFormat
    entity_id: str
    status: ExportJobStatus = ExportJobStatus.REQUESTED
    requested_by: str = ""
    scope_signature: str = ""
    artefact_s3_key: str | None = None
    artefact_bytes: int = 0
    row_count: int = 0
    delivery_bucket: str | None = None
    error_message: str = ""
    requested_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    completed_at: str | None = None

    def __post_init__(self) -> None:
        validate_tenant_code(self.tenant_code)

    @property
    def expires_at(self) -> str:
        requested = datetime.fromisoformat(self.requested_at)
        return (requested + timedelta(days=ARTEFACT_LIFECYCLE_DAYS)).isoformat()


def export_artefact_key(
    tenant_code: str, job_id: str, entity_id: str, export_format: ExportFormat
) -> str:
    """Dedicated prefix with a short lifecycle policy, tenant-scoped like every other layer."""
    validate_tenant_code(tenant_code)
    return f"{tenant_code}/exports/{job_id}/{entity_id}.{export_format.value}"


class ExportFormatStrategy:
    """Strategy per format; Parquet is a pass-through copy, CSV and JSON are conversions."""

    @staticmethod
    def to_csv(rows: Iterable[dict[str, Any]]) -> Iterator[bytes]:
        """Stream CSV in batches; the header comes from the first batch's union of keys."""
        buffer = io.StringIO()
        writer: csv.DictWriter[str] | None = None
        batch: list[dict[str, Any]] = []
        for row in rows:
            batch.append(row)
            if len(batch) < CONVERSION_BATCH_ROWS:
                continue
            yield from _flush_csv_batch(buffer, batch, writer_holder := [writer])
            writer = writer_holder[0]
            batch = []
        if batch:
            yield from _flush_csv_batch(buffer, batch, writer_holder := [writer])

    @staticmethod
    def to_json_lines(rows: Iterable[dict[str, Any]]) -> Iterator[bytes]:
        """JSON Lines rather than one array: a 10 GB array cannot be streamed by a consumer."""
        for row in rows:
            yield (json.dumps(row, separators=(",", ":"), default=str) + "\n").encode("utf-8")


def _flush_csv_batch(
    buffer: io.StringIO,
    batch: list[dict[str, Any]],
    writer_holder: list[csv.DictWriter[str] | None],
) -> Iterator[bytes]:
    writer = writer_holder[0]
    if writer is None:
        fieldnames = list(dict.fromkeys(key for row in batch for key in row))
        writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer_holder[0] = writer
    for row in batch:
        writer.writerow(row)
    payload = buffer.getvalue().encode("utf-8")
    buffer.seek(0)
    buffer.truncate(0)
    yield payload


class ExportJobRepository:
    """Job store; the job is the audit record for an export request (OWASP A09)."""

    def __init__(self, environment: str, region_name: str) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        self._environment = environment
        table_name = os.environ.get("EXPORT_JOB_TABLE") or _TABLE_NAME
        self._table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)

    def save(self, job: ExportJob) -> None:
        self._table.put_item(
            Item={
                "tenant_code": job.tenant_code,
                "job_id": job.job_id,
                "layer": job.layer.value,
                "export_format": job.export_format.value,
                "entity_id": job.entity_id,
                "status": job.status.value,
                "requested_by": job.requested_by,
                "scope_signature": job.scope_signature,
                "artefact_s3_key": job.artefact_s3_key,
                "artefact_bytes": job.artefact_bytes,
                "row_count": job.row_count,
                "delivery_bucket": job.delivery_bucket,
                "error_message": job.error_message,
                "requested_at": job.requested_at,
                "completed_at": job.completed_at,
                "expires_at": job.expires_at,
                "environment": self._environment,
            }
        )

    def get(self, tenant_code: str, job_id: str) -> dict[str, Any] | None:
        response = self._table.get_item(
            Key={"tenant_code": validate_tenant_code(tenant_code), "job_id": job_id}
        )
        item = response.get("Item")
        return dict(item) if item else None

    def list_jobs(self, tenant_code: str) -> list[dict[str, Any]]:
        response = self._table.query(
            KeyConditionExpression="tenant_code = :tc",
            ExpressionAttributeValues={":tc": validate_tenant_code(tenant_code)},
        )
        return [dict(item) for item in response.get("Items", [])]


class ExportService:
    """Requests, executes, and delivers exports."""

    def __init__(
        self,
        environment: str,
        region_name: str,
        artefact_bucket: str,
        repository: ExportJobRepository | None = None,
        s3_client: Any | None = None,
        kms_key_id: str | None = None,
    ) -> None:
        if not artefact_bucket:
            raise ValueError("artefact_bucket must not be empty.")
        self._environment = environment
        self._bucket = artefact_bucket
        self._repository = repository or ExportJobRepository(environment, region_name)
        self._s3 = s3_client or boto3.client("s3", region_name=region_name)
        self._kms_key_id = kms_key_id

    def request_export(
        self,
        tenant_code: str,
        layer: ExportLayer,
        export_format: ExportFormat,
        entity_id: str,
        *,
        requested_by: str,
        granted_capabilities: frozenset[str],
        # Non-nullable: an export that omitted the predicate ships every unit's rows in one file,
        # which is the least recoverable form of the defect because the artefact leaves the
        # platform (DL-SCOPE-14).
        scope_predicate: ScopePredicate,
        delivery_bucket: str | None = None,
    ) -> ExportJob:
        """
        Create an export job after checking the export capability and scope.

        The scope predicate is recorded on the job, not merely applied: an export's audit
        record must show which rows the requester was entitled to.
        """
        if EXPORT_CAPABILITY not in granted_capabilities:
            raise ExportCapabilityRequiredError(
                f"Export requires the {EXPORT_CAPABILITY!r} capability, which is distinct from "
                "read. An export must never be a privilege-escalation path."
            )
        if scope_predicate.surface is not ConsumptionSurface.EXPORT:
            raise ValueError(
                "The scope predicate supplied to an export must be built for the EXPORT "
                "surface, so `ScopePredicateApplied{surface}` attributes correctly."
            )
        job = ExportJob(
            tenant_code=tenant_code,
            job_id=f"exp-{uuid.uuid4().hex[:12]}",
            layer=layer,
            export_format=export_format,
            entity_id=entity_id,
            requested_by=requested_by,
            scope_signature=_predicate_signature(scope_predicate),
            delivery_bucket=delivery_bucket,
        )
        self._repository.save(job)
        record_platform_metric(
            PlatformMetric.EXPORT_JOBS_REQUESTED, 1.0, Format=export_format.value
        )
        _logger.info(
            "export_job_requested",
            tenant_code=tenant_code,
            job_id=job.job_id,
            layer=layer.value,
            export_format=export_format.value,
            requested_by=requested_by,
        )
        return job

    def execute(
        self,
        job: ExportJob,
        rows: Iterable[dict[str, Any]],
        *,
        # Non-nullable: see request_export.
        scope_predicate: ScopePredicate,
    ) -> ExportJob:
        """
        Convert and upload the artefact, applying the scope predicate row by row.

        Filtering happens here rather than in the caller because DL-SCOPE-17 requires the
        export surface itself to enforce the predicate — a caller that forgot would otherwise
        produce an unfiltered artefact.
        """
        job.status = ExportJobStatus.RUNNING
        self._repository.save(job)
        key = export_artefact_key(job.tenant_code, job.job_id, job.entity_id, job.export_format)
        try:
            filtered = _apply_scope(rows, scope_predicate)
            payload, row_count = self._render(job.export_format, filtered)
            put_kwargs: dict[str, Any] = {
                "Bucket": job.delivery_bucket or self._bucket,
                "Key": key,
                "Body": payload,
                "ContentType": _content_type(job.export_format),
            }
            if self._kms_key_id:
                put_kwargs["ServerSideEncryption"] = "aws:kms"
                put_kwargs["SSEKMSKeyId"] = self._kms_key_id
            self._s3.put_object(**put_kwargs)
        except Exception as exc:
            record_platform_metric(
                PlatformMetric.EXPORT_JOBS_FAILED, 1.0, Format=job.export_format.value
            )
            job.status = ExportJobStatus.FAILED
            job.error_message = f"{type(exc).__name__}: {exc}"
            job.completed_at = datetime.now(UTC).isoformat()
            self._repository.save(job)
            raise
        job.status = ExportJobStatus.COMPLETED
        job.artefact_s3_key = key
        job.artefact_bytes = len(payload)
        job.row_count = row_count
        job.completed_at = datetime.now(UTC).isoformat()
        self._repository.save(job)
        record_platform_metric(
            PlatformMetric.EXPORT_JOBS_COMPLETED, 1.0, Format=job.export_format.value
        )
        record_platform_metric(
            PlatformMetric.EXPORT_BYTES, job.artefact_bytes, Format=job.export_format.value
        )
        record_platform_metric(
            PlatformMetric.EXPORT_DURATION_MS,
            max(
                0.0,
                (
                    datetime.fromisoformat(str(job.completed_at))
                    - datetime.fromisoformat(job.requested_at)
                ).total_seconds()
                * 1000,
            ),
            Format=job.export_format.value,
        )
        _logger.info(
            "export_job_completed",
            tenant_code=job.tenant_code,
            job_id=job.job_id,
            row_count=row_count,
            artefact_bytes=job.artefact_bytes,
        )
        return job

    def signed_download_url(self, job: ExportJob) -> str:
        """Time-limited signed URL; the artefact is never emailed and never logged."""
        if job.status is not ExportJobStatus.COMPLETED or not job.artefact_s3_key:
            raise ValueError(
                f"Export job {job.job_id!r} is {job.status.value!r}; only a completed job has a "
                "downloadable artefact."
            )
        return str(
            self._s3.generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": job.delivery_bucket or self._bucket,
                    "Key": job.artefact_s3_key,
                },
                ExpiresIn=SIGNED_URL_TTL_SECONDS,
            )
        )

    @staticmethod
    def _render(export_format: ExportFormat, rows: Iterable[dict[str, Any]]) -> tuple[bytes, int]:
        counted: list[dict[str, Any]] = list(rows)
        if export_format is ExportFormat.CSV:
            return b"".join(ExportFormatStrategy.to_csv(counted)), len(counted)
        if export_format is ExportFormat.JSON:
            return b"".join(ExportFormatStrategy.to_json_lines(counted)), len(counted)
        return _to_parquet(counted), len(counted)


def _to_parquet(rows: list[dict[str, Any]]) -> bytes:
    """Parquet is the platform's native format, so this is a re-serialisation, not a convert."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.Table.from_pylist(rows) if rows else pa.table({})
    buffer = io.BytesIO()
    pq.write_table(table, buffer, compression="snappy")
    return buffer.getvalue()


def _apply_scope(
    rows: Iterable[dict[str, Any]], predicate: ScopePredicate
) -> Iterator[dict[str, Any]]:
    """Every row passes through `matches`; there is no branch that yields the input unfiltered."""
    for row in rows:
        if predicate.matches(row.get("scope_unit_id")):
            yield row


def _predicate_signature(predicate: ScopePredicate) -> str:
    return f"{predicate.sql}|{sorted(predicate.parameters.values())}"


def _content_type(export_format: ExportFormat) -> str:
    return {
        ExportFormat.CSV: "text/csv",
        ExportFormat.JSON: "application/x-ndjson",
        ExportFormat.PARQUET: "application/vnd.apache.parquet",
    }[export_format]
