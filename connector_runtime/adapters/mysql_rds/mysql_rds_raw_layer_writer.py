"""
MySQL RDS raw layer writer.

Writes batches of ExtractionRecord to the S3 raw layer as Parquet files,
following the platform raw layer partition scheme for MySQL RDS.

Partition scheme (from spec §4.2):
    s3://{bucket}/{prefix}/mysql_rds/{entity_id}/
        extraction_date={YYYY-MM-DD}/
        run_id={run_id}/
            data.parquet
            metadata.json

Security (OWASP A05, A09):
  - S3 keys constructed from validated entity_id (stable-id pattern check).
  - Record payloads are written as-is — no values are logged.
  - IAM credentials come from the implicit boto3 credential chain (IAM role).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from io import BytesIO
from typing import Any, Final

import boto3
import pyarrow as pa
import pyarrow.parquet as pq

from connector_runtime.interfaces.connector_interface import ExtractionRecord
from contracts.identifier_policy import STABLE_ID_PATTERN as _STABLE_ID_PATTERN
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

_SOURCE_NAME: Final[str] = "mysql_rds"


class MySqlRdsRawLayerWriterError(Exception):
    """Raised when raw layer writing fails in a way that aborts the extraction run."""


class MySqlRdsRawLayerWriter:
    """
    Writes a batch of ExtractionRecord objects to the S3 raw layer as Parquet.

    One instance per extraction run.  Responsible only for persistence —
    no transformation or field filtering is applied.

    Usage::

        writer = MySqlRdsRawLayerWriter(
            s3_bucket="prod-raw-layer",
            s3_prefix="raw",
            region_name="us-east-1",
        )
        data_key = writer.write_partition(
            records=records,
            source_id="mysql-rds",
            entity_id="mysql-rds-orders",
            run_id="run-20260612-120000000000-ab12cd34",
            schema_fingerprint="a1b2c3d4...",
            extraction_date="2026-06-12",
        )
    """

    def __init__(self, s3_bucket: str, s3_prefix: str, region_name: str) -> None:
        if not s3_bucket:
            raise ValueError("s3_bucket must not be empty.")
        self._bucket = s3_bucket
        self._prefix = s3_prefix.strip("/")
        self._s3 = boto3.client("s3", region_name=region_name)

    def write_partition(
        self,
        records: list[ExtractionRecord],
        source_id: str,
        entity_id: str,
        run_id: str,
        schema_fingerprint: str,
        extraction_date: str,
    ) -> str:
        """
        Write records as Parquet and a metadata JSON to the S3 raw layer.

        Returns:
            The S3 key of the written Parquet file.

        Raises:
            MySqlRdsRawLayerWriterError: on S3 write failure or invalid inputs.
        """
        self._validate_stable_id("source_id", source_id)
        self._validate_stable_id("entity_id", entity_id)

        extraction_timestamp = datetime.now(UTC)
        partition_prefix = self._partition_path(entity_id, extraction_date, run_id)
        data_key = f"{partition_prefix}/data.parquet"
        metadata_key = f"{partition_prefix}/metadata.json"

        parquet_bytes = self._records_to_parquet(records)
        record_count = len(records)

        metadata: dict[str, Any] = {
            "run_id": run_id,
            "source_id": source_id,
            "entity_id": entity_id,
            "extraction_timestamp": extraction_timestamp.isoformat(),
            "schema_version": schema_fingerprint,
            "record_count": record_count,
            "extraction_date": extraction_date,
            "data_key": data_key,
        }

        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=data_key,
                Body=parquet_bytes,
                ContentType="application/octet-stream",
            )
        except Exception as exc:
            raise MySqlRdsRawLayerWriterError(
                f"Failed to write Parquet to s3://{self._bucket}/{data_key}: {type(exc).__name__}"
            ) from exc

        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=metadata_key,
                Body=json.dumps(metadata, indent=2).encode(),
                ContentType="application/json",
            )
        except Exception as exc:
            _logger.warning(
                "mysql_rds_raw_metadata_write_failed",
                data_key=data_key,
                metadata_key=metadata_key,
                error=type(exc).__name__,
            )

        _logger.info(
            "mysql_rds_raw_partition_written",
            bucket=self._bucket,
            data_key=data_key,
            record_count=record_count,
            entity_id=entity_id,
            run_id=run_id,
        )
        return data_key

    # ── Private helpers ────────────────────────────────────────────────────────

    def _partition_path(self, entity_id: str, extraction_date: str, run_id: str) -> str:
        parts = [self._prefix] if self._prefix else []
        parts.extend(
            [_SOURCE_NAME, entity_id, f"extraction_date={extraction_date}", f"run_id={run_id}"]
        )
        return "/".join(parts)

    @staticmethod
    def _records_to_parquet(records: list[ExtractionRecord]) -> bytes:
        if not records:
            raise MySqlRdsRawLayerWriterError(
                "Cannot write empty record batch — at least one record is required."
            )

        all_keys: list[str] = []
        for record in records:
            for key in record.payload:
                if key not in all_keys:
                    all_keys.append(key)

        columns: dict[str, list[str | None]] = {key: [] for key in all_keys}
        for record in records:
            for key in all_keys:
                value = record.payload.get(key)
                columns[key].append(None if value is None else str(value))

        arrays = [pa.array(columns[key], type=pa.large_utf8()) for key in all_keys]
        schema = pa.schema([(key, pa.large_utf8()) for key in all_keys])
        table = pa.table(dict(zip(all_keys, arrays, strict=True)), schema=schema)

        buf = BytesIO()
        pq.write_table(table, buf, compression="snappy")  # type: ignore[no-untyped-call]
        return buf.getvalue()

    def _validate_stable_id(self, field_name: str, value: str) -> None:
        if not _STABLE_ID_PATTERN.match(value):
            raise MySqlRdsRawLayerWriterError(
                f"{field_name}={value!r} does not match the stable ID pattern "
                f"{_STABLE_ID_PATTERN.pattern!r}. "
                "Path traversal characters and uppercase are not permitted."
            )

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
        Write records from an iterator in memory-bounded chunks to S3.

        Peak memory is O(chunk_size) regardless of total record volume.

        Returns:
            Tuple of (partition_prefix, total_record_count).
        """
        self._validate_stable_id("source_id", source_id)
        self._validate_stable_id("entity_id", entity_id)

        partition_prefix = self._partition_path(entity_id, extraction_date, run_id)
        part_index = 0
        total_count = 0
        chunk: list[ExtractionRecord] = []

        for record in record_iter:
            chunk.append(record)
            total_count += 1
            if len(chunk) >= chunk_size:
                part_key = f"{partition_prefix}/part-{part_index:05d}.parquet"
                self._write_parquet_part(chunk, part_key)
                part_index += 1
                chunk = []

        if chunk:
            part_key = f"{partition_prefix}/part-{part_index:05d}.parquet"
            self._write_parquet_part(chunk, part_key)
            part_index += 1

        if total_count == 0:
            _logger.warning(
                "mysql_rds_streaming_write_zero_records",
                entity_id=entity_id,
                run_id=run_id,
            )
            return partition_prefix, 0

        metadata_key = f"{partition_prefix}/metadata.json"
        metadata: dict[str, Any] = {
            "run_id": run_id,
            "source_id": source_id,
            "entity_id": entity_id,
            "extraction_timestamp": datetime.now(UTC).isoformat(),
            "schema_version": schema_fingerprint,
            "record_count": total_count,
            "extraction_date": extraction_date,
            "part_count": part_index,
            "partition_prefix": partition_prefix,
        }
        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=metadata_key,
                Body=json.dumps(metadata, separators=(",", ":")).encode(),
                ContentType="application/json",
            )
        except Exception as exc:
            _logger.warning(
                "mysql_rds_streaming_metadata_write_failed",
                partition_prefix=partition_prefix,
                error=type(exc).__name__,
            )

        _logger.info(
            "mysql_rds_streaming_partition_written",
            bucket=self._bucket,
            partition_prefix=partition_prefix,
            total_record_count=total_count,
            part_count=part_index,
            entity_id=entity_id,
            run_id=run_id,
        )
        return partition_prefix, total_count

    def _write_parquet_part(self, records: list[ExtractionRecord], s3_key: str) -> None:
        """Serialise one chunk of records to Parquet and upload to S3."""
        parquet_bytes = self._records_to_parquet(records)
        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=s3_key,
                Body=parquet_bytes,
                ContentType="application/octet-stream",
            )
        except Exception as exc:
            raise MySqlRdsRawLayerWriterError(
                f"Failed to write Parquet part to s3://{self._bucket}/{s3_key}: "
                f"{type(exc).__name__}"
            ) from exc
