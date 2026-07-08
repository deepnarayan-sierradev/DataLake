"""
Curated layer S3 writer.

Writes canonical (mapped + quality-checked) records to the S3 curated layer
as Parquet files.

Partition scheme (spec §14):
  s3://{bucket}/curated/{domain}/{entity_id}/curated_date={YYYY-MM-DD}/run_id={run_id}/data.parquet

Rules:
  - Never modifies raw data; reads are always from separate raw bucket.
  - All writes are append-only — unique run_id prevents overwrites.
  - Snappy compression for balance of speed and ratio.
  - Sensitive attribute masking is the responsibility of the transformation
    pipeline (applied before this writer is called).
  - Uses S3ParquetWriter with automatic multipart upload for large files (§3.3).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import boto3

from observability.s3_writer import S3ParquetWriter
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)


@dataclass(frozen=True)
class CuratedWriteResult:
    """Summary of a curated layer write operation."""

    s3_prefix: str
    s3_key: str
    record_count: int
    written_at: str  # ISO-8601 UTC


class CuratedLayerWriter:
    """
    Writes canonical records to the S3 curated layer in Parquet format.

    One instance per transformation run or reused across runs for the same
    environment and region.
    """

    def __init__(self, s3_bucket: str, region_name: str) -> None:
        self._s3_bucket = s3_bucket
        self._region_name = region_name
        self._s3: Any = boto3.client("s3", region_name=region_name)
        self._writer = S3ParquetWriter(self._s3)

    def write(
        self,
        records: list[dict[str, Any]],
        domain: str,
        entity_id: str,
        run_id: str,
        curated_date: date | None = None,
        tenant_code: str = "demo",
    ) -> CuratedWriteResult:
        """
        Write canonical records to the curated layer.

        Uses S3ParquetWriter for automatic multipart upload on large files.
        Path scheme: {tenant_code}/curated/{domain}/{entity_id}/curated_date=.../run_id=.../

        Args:
            records:      Canonical records after field mapping + quality check.
            domain:       Business domain (e.g., "customer", "finance").
            entity_id:    Stable entity identifier.
            run_id:       Extraction run_id for traceability and partition isolation.
            curated_date: Partition date; defaults to today UTC.
            tenant_code:  Tenant slug for S3 path isolation (default: "demo").

        Returns:
            CuratedWriteResult with the S3 location and record count.

        Raises:
            CuratedWriteError if records is empty or S3 write fails.
        """
        if not records:
            raise CuratedWriteError("Cannot write zero records to curated layer")

        partition_date = curated_date or datetime.now(UTC).date()
        prefix = (
            f"{tenant_code}/curated/{domain}/{entity_id}"
            f"/curated_date={partition_date.isoformat()}"
            f"/run_id={run_id}/"
        )
        key = f"{prefix}data.parquet"

        try:
            count = self._writer.write(
                records_iter=iter(records),
                bucket=self._s3_bucket,
                key=key,
                compression="snappy",
            )
        except Exception as exc:
            raise CuratedWriteError(f"S3 write failed for key={key!r}: {exc}") from exc

        written_at = datetime.now(UTC).isoformat()

        _logger.info(
            "curated_layer_write_complete",
            s3_bucket=self._s3_bucket,
            s3_key=key,
            domain=domain,
            entity_id=entity_id,
            run_id=run_id,
            record_count=count,
        )

        return CuratedWriteResult(
            s3_prefix=prefix,
            s3_key=key,
            record_count=count,
            written_at=written_at,
        )

    def write_streaming(
        self,
        records_iter: Iterator[dict[str, Any]],
        domain: str,
        entity_id: str,
        run_id: str,
        curated_date: date | None = None,
        tenant_code: str = "demo",
    ) -> CuratedWriteResult:
        """
        Write records from a lazy iterator using streaming + multipart upload.

        Path scheme: {tenant_code}/curated/{domain}/{entity_id}/curated_date=.../run_id=.../
        Peak memory: O(50K rows x avg_record_bytes) regardless of total count.
        """
        partition_date = curated_date or datetime.now(UTC).date()
        prefix = (
            f"{tenant_code}/curated/{domain}/{entity_id}"
            f"/curated_date={partition_date.isoformat()}"
            f"/run_id={run_id}/"
        )
        key = f"{prefix}data.parquet"

        try:
            count = self._writer.write(
                records_iter=records_iter,
                bucket=self._s3_bucket,
                key=key,
                compression="snappy",
            )
        except Exception as exc:
            raise CuratedWriteError(f"S3 streaming write failed for key={key!r}: {exc}") from exc

        if count == 0:
            return CuratedWriteResult(
                s3_prefix="",
                s3_key=key,
                record_count=0,
                written_at=datetime.now(UTC).isoformat(),
            )

        written_at = datetime.now(UTC).isoformat()
        _logger.info(
            "curated_layer_streaming_write_complete",
            s3_bucket=self._s3_bucket,
            s3_key=key,
            domain=domain,
            entity_id=entity_id,
            run_id=run_id,
            record_count=count,
        )

        return CuratedWriteResult(
            s3_prefix=prefix,
            s3_key=key,
            record_count=count,
            written_at=written_at,
        )


class CuratedWriteError(Exception):
    """Raised when a curated layer write operation fails."""
