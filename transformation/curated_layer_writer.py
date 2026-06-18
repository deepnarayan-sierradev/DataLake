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
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import boto3
import pyarrow as pa
import pyarrow.parquet as pq

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

    def write(
        self,
        records: list[dict[str, Any]],
        domain: str,
        entity_id: str,
        run_id: str,
        curated_date: date | None = None,
    ) -> CuratedWriteResult:
        """
        Write canonical records to the curated layer.

        Args:
            records:      Canonical records after field mapping + quality check.
            domain:       Business domain (e.g., "customer", "finance").
            entity_id:    Stable entity identifier.
            run_id:       Extraction run_id for traceability and partition isolation.
            curated_date: Partition date; defaults to today UTC.

        Returns:
            CuratedWriteResult with the S3 location and record count.

        Raises:
            CuratedWriteError if records is empty or S3 write fails.
        """
        if not records:
            raise CuratedWriteError("Cannot write zero records to curated layer")

        partition_date = curated_date or datetime.now(UTC).date()
        prefix = (
            f"curated/{domain}/{entity_id}"
            f"/curated_date={partition_date.isoformat()}"
            f"/run_id={run_id}/"
        )
        key = f"{prefix}data.parquet"

        parquet_bytes = _serialise_to_parquet(records)

        try:
            self._s3.put_object(
                Bucket=self._s3_bucket,
                Key=key,
                Body=parquet_bytes,
                ContentType="application/octet-stream",
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
            record_count=len(records),
        )

        return CuratedWriteResult(
            s3_prefix=prefix,
            s3_key=key,
            record_count=len(records),
            written_at=written_at,
        )


def _serialise_to_parquet(records: list[dict[str, Any]]) -> bytes:
    """Convert a list of record dicts to Parquet bytes (Snappy compression)."""
    table = pa.Table.from_pylist(records)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")  # type: ignore[no-untyped-call]
    return buf.getvalue()


class CuratedWriteError(Exception):
    """Raised when a curated layer write operation fails."""
