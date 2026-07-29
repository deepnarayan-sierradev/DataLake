"""
Backfill orchestrator (DL-DQ-01) and reprocessing execution (DL-CFG-11).

`EdlBackfillJob` holds the chunk plan, per-chunk state, and resume pointer. Each chunk is an
independent, resumable Step Functions execution with its own watermark checkpoint, so a
multi-million-row backfill never restarts from zero.

Saga with compensation: a failed chunk's compensating action deletes its partition, so a
partial chunk never leaves half-written state behind.

A reprocess is a backfill with a `reprocess_reason` and a pinned target configuration
version — deliberately the same engine, not a second chunked-replay implementation.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

import boto3

from config_propagation.capability import ConfigCapability
from contracts.identifier_policy import validate_tenant_code
from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

_TABLE_NAME: Final[str] = "EdlBackfillJob"


def _as_item_list(value: Any) -> list[dict[str, Any]]:
    """DynamoDB list attributes deserialise to a broad union; narrow to item dicts."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


# Observed throughput floor used when no measurement exists yet; chunk size is derived from
# measured rows-per-second thereafter rather than fixed (DL-02 performance clause).
DEFAULT_ASSUMED_ROWS_PER_SECOND: Final[float] = 500.0

# Target wall-clock per chunk. Well inside the 900s Lambda ceiling so a chunk has room to
# checkpoint and write its audit record before the runtime kills it.
TARGET_CHUNK_SECONDS: Final[float] = 600.0

MIN_CHUNK_DAYS: Final[int] = 1
MAX_CHUNK_DAYS: Final[int] = 90
MAX_CHUNKS_PER_JOB: Final[int] = 2_000


class ChunkState(StrEnum):
    """Per-chunk lifecycle."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    COMPENSATED = "compensated"
    CANCELLED = "cancelled"


class JobState(StrEnum):
    """Overall job lifecycle."""

    PLANNED = "planned"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class BackfillJobNotFoundError(Exception):
    """Raised when a job id does not resolve for the tenant."""


class BackfillCancelledError(Exception):
    """Raised when a cancelled job is advanced."""


@dataclass
class BackfillChunk:
    """One date-ranged unit of work."""

    sequence: int
    window_start: date
    window_end: date
    state: ChunkState = ChunkState.PENDING
    rows_processed: int = 0
    attempts: int = 0
    partition_prefix: str = ""
    error_message: str = ""

    def to_item(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "window_start": self.window_start.isoformat(),
            "window_end": self.window_end.isoformat(),
            "state": self.state.value,
            "rows_processed": self.rows_processed,
            "attempts": self.attempts,
            "partition_prefix": self.partition_prefix,
            "error_message": self.error_message,
        }

    @classmethod
    def from_item(cls, item: dict[str, Any]) -> BackfillChunk:
        return cls(
            sequence=int(item["sequence"]),
            window_start=date.fromisoformat(str(item["window_start"])),
            window_end=date.fromisoformat(str(item["window_end"])),
            state=ChunkState(str(item.get("state", "pending"))),
            rows_processed=int(item.get("rows_processed", 0)),
            attempts=int(item.get("attempts", 0)),
            partition_prefix=str(item.get("partition_prefix", "")),
            error_message=str(item.get("error_message", "")),
        )


@dataclass
class BackfillJob:
    """A bounded historical load or reprocess, as a resumable chunk sequence."""

    tenant_code: str
    entity_id: str
    job_id: str
    window_start: date
    window_end: date
    chunks: list[BackfillChunk]
    state: JobState = JobState.PLANNED
    source_id: str = ""
    connection_id: str | None = None
    reprocess_reason: str = ""
    reprocess_capability: ConfigCapability | None = None
    pinned_config_version: str = ""
    observed_rows_per_second: float = DEFAULT_ASSUMED_ROWS_PER_SECOND
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def __post_init__(self) -> None:
        validate_tenant_code(self.tenant_code)
        if self.is_reprocess and not self.pinned_config_version:
            raise ValueError(
                f"job {self.job_id!r}: a reprocess must be pinned to the new configuration "
                "version for the whole job, or its output cannot be attributed (DL-CFG-11)."
            )

    @property
    def sort_key(self) -> str:
        return f"{self.entity_id}#{self.job_id}"

    @property
    def is_reprocess(self) -> bool:
        return bool(self.reprocess_reason or self.reprocess_capability)

    @property
    def resume_pointer(self) -> int | None:
        """Sequence of the first chunk not yet completed, or None when the job is done."""
        for chunk in self.chunks:
            if chunk.state in (ChunkState.PENDING, ChunkState.RUNNING, ChunkState.FAILED):
                return chunk.sequence
        return None

    @property
    def rows_processed(self) -> int:
        return sum(chunk.rows_processed for chunk in self.chunks)

    @property
    def completed_chunks(self) -> int:
        return sum(1 for chunk in self.chunks if chunk.state is ChunkState.COMPLETED)

    @property
    def failed_chunks(self) -> int:
        return sum(1 for chunk in self.chunks if chunk.state is ChunkState.FAILED)

    def chunk(self, sequence: int) -> BackfillChunk:
        for candidate in self.chunks:
            if candidate.sequence == sequence:
                return candidate
        raise KeyError(f"job {self.job_id!r} has no chunk {sequence}.")


def derive_chunk_days(rows_per_second: float, estimated_rows_per_day: float) -> int:
    """
    Chunk size from observed throughput, not a fixed constant.

    A slow source gets smaller chunks so each still fits inside one Lambda invocation; a
    fast one gets larger chunks so a long history does not become thousands of executions.
    """
    if rows_per_second <= 0:
        raise ValueError("rows_per_second must be positive.")
    if estimated_rows_per_day <= 0:
        return MAX_CHUNK_DAYS
    rows_per_chunk = rows_per_second * TARGET_CHUNK_SECONDS
    days = int(rows_per_chunk // estimated_rows_per_day)
    return max(MIN_CHUNK_DAYS, min(MAX_CHUNK_DAYS, days))


def plan_chunks(window_start: date, window_end: date, chunk_days: int) -> list[BackfillChunk]:
    """Split a window into contiguous, non-overlapping chunks; the last one may be short."""
    if window_end < window_start:
        raise ValueError("window_end must not precede window_start.")
    if chunk_days < MIN_CHUNK_DAYS:
        raise ValueError(f"chunk_days must be at least {MIN_CHUNK_DAYS}.")
    chunks: list[BackfillChunk] = []
    cursor = window_start
    sequence = 0
    while cursor <= window_end:
        chunk_end = min(cursor + timedelta(days=chunk_days - 1), window_end)
        chunks.append(BackfillChunk(sequence=sequence, window_start=cursor, window_end=chunk_end))
        cursor = chunk_end + timedelta(days=1)
        sequence += 1
        if sequence > MAX_CHUNKS_PER_JOB:
            raise ValueError(
                f"A backfill of {window_start}..{window_end} at {chunk_days}-day chunks would "
                f"exceed {MAX_CHUNKS_PER_JOB} chunks. Widen the chunk size or narrow the window "
                "— an unbounded job is the failure mode DL-DQ-01 exists to prevent."
            )
    return chunks


class BackfillOrchestrator:
    """Plans, persists, advances, and compensates backfill and reprocess jobs."""

    def __init__(
        self,
        environment: str,
        region_name: str,
        s3_client: Any | None = None,
        raw_s3_bucket: str | None = None,
    ) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        self._environment = environment
        table_name = os.environ.get("BACKFILL_JOB_TABLE") or _TABLE_NAME
        self._table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)
        self._s3 = s3_client
        self._raw_bucket = raw_s3_bucket

    # ── Planning ──────────────────────────────────────────────────────────────

    def plan_job(
        self,
        tenant_code: str,
        entity_id: str,
        window_start: date,
        window_end: date,
        *,
        source_id: str = "",
        connection_id: str | None = None,
        estimated_rows_per_day: float = 0.0,
        observed_rows_per_second: float = DEFAULT_ASSUMED_ROWS_PER_SECOND,
        reprocess_reason: str = "",
        reprocess_capability: ConfigCapability | None = None,
        pinned_config_version: str = "",
    ) -> BackfillJob:
        chunk_days = derive_chunk_days(observed_rows_per_second, estimated_rows_per_day)
        job = BackfillJob(
            tenant_code=validate_tenant_code(tenant_code),
            entity_id=entity_id,
            job_id=f"bfj-{uuid.uuid4().hex[:12]}",
            window_start=window_start,
            window_end=window_end,
            chunks=plan_chunks(window_start, window_end, chunk_days),
            source_id=source_id,
            connection_id=connection_id,
            observed_rows_per_second=observed_rows_per_second,
            reprocess_reason=reprocess_reason,
            reprocess_capability=reprocess_capability,
            pinned_config_version=pinned_config_version,
        )
        self._save(job)
        if job.is_reprocess:
            record_platform_metric(PlatformMetric.REPROCESS_JOBS_STARTED, 1.0, EntityId=entity_id)
        _logger.info(
            "backfill_job_planned",
            tenant_code=job.tenant_code,
            entity_id=entity_id,
            job_id=job.job_id,
            chunk_count=len(job.chunks),
            chunk_days=chunk_days,
            is_reprocess=job.is_reprocess,
        )
        return job

    # ── Execution ─────────────────────────────────────────────────────────────

    def next_chunk(self, tenant_code: str, entity_id: str, job_id: str) -> BackfillChunk | None:
        """
        The chunk to run next — the resume pointer, so a replay never restarts from zero.

        Returns None when every chunk has completed.
        """
        job = self.load_job(tenant_code, entity_id, job_id)
        if job.state is JobState.CANCELLED:
            raise BackfillCancelledError(f"Backfill job {job_id!r} was cancelled.")
        pointer = job.resume_pointer
        return None if pointer is None else job.chunk(pointer)

    def mark_chunk_running(
        self, tenant_code: str, entity_id: str, job_id: str, sequence: int
    ) -> BackfillJob:
        job = self.load_job(tenant_code, entity_id, job_id)
        chunk = job.chunk(sequence)
        chunk.state = ChunkState.RUNNING
        chunk.attempts += 1
        job.state = JobState.RUNNING
        self._save(job)
        return job

    def complete_chunk(
        self,
        tenant_code: str,
        entity_id: str,
        job_id: str,
        sequence: int,
        rows_processed: int,
        partition_prefix: str = "",
    ) -> BackfillJob:
        """
        Record a chunk as complete and idempotently absorb a replay of the same chunk.

        Idempotent on replay: re-completing an already-complete chunk does not double-count
        rows, which is what makes a Step Functions retry safe.
        """
        job = self.load_job(tenant_code, entity_id, job_id)
        chunk = job.chunk(sequence)
        if chunk.state is ChunkState.COMPLETED:
            return job
        chunk.state = ChunkState.COMPLETED
        chunk.rows_processed = rows_processed
        record_platform_metric(PlatformMetric.BACKFILL_CHUNKS_COMPLETED, 1.0, EntityId=entity_id)
        if job.is_reprocess:
            record_platform_metric(
                PlatformMetric.REPROCESS_ROWS_RECOMPUTED, rows_processed, EntityId=entity_id
            )
        chunk.partition_prefix = partition_prefix
        chunk.error_message = ""
        if job.resume_pointer is None:
            job.state = JobState.COMPLETED
            if job.is_reprocess:
                record_platform_metric(
                    PlatformMetric.REPROCESS_JOBS_COMPLETED, 1.0, EntityId=entity_id
                )
        self._save(job)
        return job

    def fail_chunk(
        self,
        tenant_code: str,
        entity_id: str,
        job_id: str,
        sequence: int,
        error_message: str,
    ) -> BackfillJob:
        job = self.load_job(tenant_code, entity_id, job_id)
        chunk = job.chunk(sequence)
        chunk.state = ChunkState.FAILED
        chunk.error_message = error_message
        record_platform_metric(PlatformMetric.BACKFILL_CHUNKS_FAILED, 1.0, EntityId=entity_id)
        if job.is_reprocess:
            record_platform_metric(PlatformMetric.REPROCESS_JOBS_FAILED, 1.0, EntityId=entity_id)
        self._save(job)
        _logger.warning(
            "backfill_chunk_failed",
            tenant_code=tenant_code,
            entity_id=entity_id,
            job_id=job_id,
            sequence=sequence,
        )
        return job

    def compensate_chunk(
        self, tenant_code: str, entity_id: str, job_id: str, sequence: int
    ) -> BackfillJob:
        """
        Delete a failed chunk's partition so no partial state survives the failure.

        The compensating action of the saga: without it a retried chunk would union with the
        rows its failed attempt already wrote.
        """
        job = self.load_job(tenant_code, entity_id, job_id)
        chunk = job.chunk(sequence)
        if chunk.partition_prefix and self._s3 is not None and self._raw_bucket:
            self._delete_prefix(chunk.partition_prefix)
        chunk.state = ChunkState.COMPENSATED
        chunk.rows_processed = 0
        chunk.partition_prefix = ""
        self._save(job)
        return job

    def cancel_job(self, tenant_code: str, entity_id: str, job_id: str) -> BackfillJob:
        job = self.load_job(tenant_code, entity_id, job_id)
        for chunk in job.chunks:
            if chunk.state in (ChunkState.PENDING, ChunkState.RUNNING):
                chunk.state = ChunkState.CANCELLED
        job.state = JobState.CANCELLED
        self._save(job)
        return job

    def record_throughput(
        self, tenant_code: str, entity_id: str, job_id: str, rows_per_second: float
    ) -> None:
        """Feed measured throughput back so later jobs size their chunks from evidence."""
        job = self.load_job(tenant_code, entity_id, job_id)
        job.observed_rows_per_second = max(1.0, rows_per_second)
        record_platform_metric(
            PlatformMetric.BACKFILL_ROWS_PER_SECOND, rows_per_second, EntityId=entity_id
        )
        self._save(job)

    # ── Persistence ───────────────────────────────────────────────────────────

    def load_job(self, tenant_code: str, entity_id: str, job_id: str) -> BackfillJob:
        tenant_code = validate_tenant_code(tenant_code)
        response = self._table.get_item(
            Key={"tenant_code": tenant_code, "job_key": f"{entity_id}#{job_id}"},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if not item:
            raise BackfillJobNotFoundError(
                f"No backfill job {job_id!r} for tenant {tenant_code!r} entity {entity_id!r}."
            )
        capability = item.get("reprocess_capability")
        return BackfillJob(
            tenant_code=tenant_code,
            entity_id=entity_id,
            job_id=job_id,
            window_start=date.fromisoformat(str(item["window_start"])),
            window_end=date.fromisoformat(str(item["window_end"])),
            chunks=[
                BackfillChunk.from_item(dict(chunk)) for chunk in _as_item_list(item.get("chunks"))
            ],
            state=JobState(str(item.get("state", "planned"))),
            source_id=str(item.get("source_id", "")),
            connection_id=_optional_str(item.get("connection_id")),
            reprocess_reason=str(item.get("reprocess_reason", "")),
            reprocess_capability=ConfigCapability(str(capability)) if capability else None,
            pinned_config_version=str(item.get("pinned_config_version", "")),
            observed_rows_per_second=float(str(item.get("observed_rows_per_second") or 1)),
            created_at=str(item.get("created_at", "")),
        )

    def list_jobs(self, tenant_code: str, entity_id: str | None = None) -> list[BackfillJob]:
        tenant_code = validate_tenant_code(tenant_code)
        query_kwargs: dict[str, Any] = {
            "KeyConditionExpression": "tenant_code = :tc",
            "ExpressionAttributeValues": {":tc": tenant_code},
        }
        if entity_id:
            query_kwargs["KeyConditionExpression"] = (
                "tenant_code = :tc AND begins_with(job_key, :entity)"
            )
            query_kwargs["ExpressionAttributeValues"][":entity"] = f"{entity_id}#"
        response = self._table.query(**query_kwargs)
        jobs: list[BackfillJob] = []
        for item in response.get("Items", []):
            key = str(item["job_key"])
            item_entity_id, _, job_id = key.partition("#")
            jobs.append(self.load_job(tenant_code, item_entity_id, job_id))
        return jobs

    def _save(self, job: BackfillJob) -> None:
        self._table.put_item(
            Item={
                "tenant_code": job.tenant_code,
                "job_key": job.sort_key,
                "window_start": job.window_start.isoformat(),
                "window_end": job.window_end.isoformat(),
                "state": job.state.value,
                "chunks": [chunk.to_item() for chunk in job.chunks],
                "source_id": job.source_id,
                "connection_id": job.connection_id,
                "reprocess_reason": job.reprocess_reason,
                "reprocess_capability": (
                    job.reprocess_capability.value if job.reprocess_capability else None
                ),
                "pinned_config_version": job.pinned_config_version,
                "observed_rows_per_second": int(job.observed_rows_per_second),
                "created_at": job.created_at,
                "environment": self._environment,
            }
        )

    def _delete_prefix(self, prefix: str) -> None:
        if self._s3 is None or not self._raw_bucket:
            # Compensation without an S3 client would silently leave the partition in place,
            # which is worse than refusing: the caller believes the chunk was cleaned.
            raise ValueError(
                "Chunk compensation requires both an S3 client and a raw bucket; without them "
                "a failed chunk's partition would silently survive."
            )
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._raw_bucket, Prefix=prefix):
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if keys:
                self._s3.delete_objects(Bucket=self._raw_bucket, Delete={"Objects": keys})
