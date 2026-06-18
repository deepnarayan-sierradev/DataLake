"""
Salesforce raw layer writer — Phase 3 deliverable §3.5.

Writes batches of ExtractionRecord to the S3 raw layer as Parquet files.

Partition scheme (from spec §3.5):
    s3://{bucket}/{prefix}/salesforce/{entity_id}/
        extraction_date={YYYY-MM-DD}/
        run_id={run_id}/
            data.parquet
            metadata.json

Extraction metadata written alongside payload:
    run_id, source_id, entity_id, extraction_timestamp, schema_version, record_count

Design requirements:
  - Append-only writes — each run produces a unique partition path via run_id.
  - No overwrites of prior raw files — the run_id partition guarantees uniqueness.
  - Raw records match source payload structure with no transformation artifacts.
  - Parquet format chosen for columnar compression and downstream Athena/Glue
    compatibility.

Security (OWASP A05, A09):
  - S3 keys constructed from validated entity_id (stable-id pattern check) to
    prevent path traversal.
  - Record payloads are written as-is — no values are logged.
  - IAM credentials come from the implicit boto3 credential chain (IAM role).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from io import BytesIO
from typing import Any, Final

import boto3
import pyarrow as pa
import pyarrow.parquet as pq

from connector_runtime.interfaces.connector_interface import ExtractionRecord
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# Matches the stable ID format enforced on source_id and entity_id.
# Re-validated here because S3 key construction uses both values directly.
_STABLE_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9\-]{1,63}$")

_SOURCE_NAME: Final[str] = "salesforce"


class SalesforceRawLayerWriterError(Exception):
    """Raised when raw layer writing fails in a way that aborts the extraction run."""


class SalesforceRawLayerWriter:
    """
    Writes a batch of ExtractionRecord objects to the S3 raw layer as Parquet.

    One instance per extraction run.  The writer is responsible only for
    persistence — it does not perform any transformation or field filtering.

    Usage::

        writer = SalesforceRawLayerWriter(
            s3_bucket="prod-raw-layer",
            s3_prefix="raw",
            region_name="us-east-1",
        )
        data_key = writer.write_partition(
            records=records,
            source_id="salesforce",
            entity_id="salesforce-account",
            run_id="run-20260612-120000000000-ab12cd34",
            schema_fingerprint="a1b2c3d4...",
            extraction_date="2026-06-12",
        )
    """

    def __init__(
        self,
        s3_bucket: str,
        s3_prefix: str,
        region_name: str,
    ) -> None:
        if not s3_bucket:
            raise ValueError("s3_bucket must not be empty.")
        self._bucket = s3_bucket
        # Normalise prefix: strip leading/trailing slashes for consistent key building.
        self._prefix = s3_prefix.strip("/")
        self._s3 = boto3.client("s3", region_name=region_name)

    def write_partition(
        self,
        records: list[ExtractionRecord],
        source_id: str,
        entity_id: str,
        run_id: str,
        schema_fingerprint: str,
        extraction_date: str,  # YYYY-MM-DD
    ) -> str:
        """
        Write records as Parquet and a metadata JSON to the S3 raw layer.

        Both files are written to the same partition path derived from the
        entity_id and run_id.  The path is append-only — each unique run_id
        produces a distinct partition.

        Args:
            records: The raw ExtractionRecord objects yielded by the connector.
            source_id: Stable source identifier (validated against stable-id pattern).
            entity_id: Stable entity identifier (validated against stable-id pattern).
            run_id: Unique run identifier; used as a partition component.
            schema_fingerprint: SHA-256 fingerprint of the schema at discovery time.
            extraction_date: ISO date string (YYYY-MM-DD) for the extraction_date partition.

        Returns:
            The S3 key of the written Parquet file.

        Raises:
            SalesforceRawLayerWriterError: on S3 write failure or invalid inputs.
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
                # Server-side encryption is enforced by the S3 bucket policy
                # (SSE-KMS, set in the storage Terraform module).
            )
        except Exception as exc:
            raise SalesforceRawLayerWriterError(
                f"Failed to write Parquet file to s3://{self._bucket}/{data_key}: "
                f"{type(exc).__name__}"
            ) from exc

        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=metadata_key,
                Body=json.dumps(metadata, indent=2).encode(),
                ContentType="application/json",
            )
        except Exception as exc:
            # Metadata write failure is logged but not fatal — the Parquet data
            # is already persisted.  Metadata can be reconstructed from the run
            # audit log table.
            _logger.warning(
                "salesforce_raw_metadata_write_failed",
                data_key=data_key,
                metadata_key=metadata_key,
                error=type(exc).__name__,
            )

        _logger.info(
            "salesforce_raw_partition_written",
            bucket=self._bucket,
            data_key=data_key,
            metadata_key=metadata_key,
            record_count=record_count,
            entity_id=entity_id,
            run_id=run_id,
        )

        return data_key

    # ── Private helpers ────────────────────────────────────────────────────────

    def _partition_path(self, entity_id: str, extraction_date: str, run_id: str) -> str:
        """
        Build the S3 partition prefix.

        Format: {prefix}/salesforce/{entity_id}/extraction_date={date}/run_id={run_id}
        """
        parts = [self._prefix] if self._prefix else []
        parts.extend(
            [
                _SOURCE_NAME,
                entity_id,
                f"extraction_date={extraction_date}",
                f"run_id={run_id}",
            ]
        )
        return "/".join(parts)

    @staticmethod
    def _records_to_parquet(records: list[ExtractionRecord]) -> bytes:
        """
        Serialise a list of ExtractionRecord payloads to Parquet bytes.

        All field values are preserved as-is from the source (string columns
        from Salesforce CSV output).  No type coercion is applied here — that
        is the responsibility of the curated layer transformation in Phase 6.

        Returns:
            Parquet file contents as bytes.

        Raises:
            SalesforceRawLayerWriterError: if the record list is empty or the
                payloads have incompatible schemas.
        """
        if not records:
            raise SalesforceRawLayerWriterError(
                "Cannot write empty record batch — at least one record is required."
            )

        # Build a unified column set from all records (some rows may omit fields).
        all_keys: list[str] = list(dict.fromkeys(k for r in records for k in r.payload.keys()))

        # Construct a pyarrow Table with all-string columns.
        # Bulk API 2.0 always returns strings in CSV; the curated layer handles casting.
        arrays = [
            pa.array(
                [
                    str(r.payload[k]) if k in r.payload and r.payload[k] is not None else None
                    for r in records
                ]
            )
            for k in all_keys
        ]
        schema = pa.schema([pa.field(k, pa.large_utf8()) for k in all_keys])
        table = pa.table(dict(zip(all_keys, arrays, strict=True)), schema=schema)

        buf = BytesIO()
        pq.write_table(table, buf, compression="snappy")  # type: ignore[no-untyped-call]
        return buf.getvalue()

    @staticmethod
    def _validate_stable_id(field_name: str, value: str) -> None:
        """
        Raise SalesforceRawLayerWriterError when value fails the stable-id pattern.

        Prevents S3 path traversal by ensuring entity_id and source_id contain
        only lowercase alphanumeric characters and hyphens.
        """
        if not _STABLE_ID_PATTERN.match(value):
            raise SalesforceRawLayerWriterError(
                f"{field_name} {value!r} does not match the stable ID pattern "
                r"^[a-z][a-z0-9\-]{1,63}$. Path construction aborted."
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

        Never buffers more than ``chunk_size`` records in heap simultaneously.
        Each chunk is serialised to Parquet and uploaded as a separate part
        file, so peak memory is O(chunk_size) regardless of total record volume.

        Returns:
            Tuple of (partition_prefix, total_record_count).

        Raises:
            SalesforceRawLayerWriterError: on S3 write failure or invalid inputs.
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

        # Flush remaining records (the last partial chunk)
        if chunk:
            part_key = f"{partition_prefix}/part-{part_index:05d}.parquet"
            self._write_parquet_part(chunk, part_key)
            part_index += 1

        if total_count == 0:
            _logger.warning(
                "salesforce_streaming_write_zero_records",
                entity_id=entity_id,
                run_id=run_id,
            )
            return partition_prefix, 0

        # Write metadata sidecar once all parts are written
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
                "salesforce_streaming_metadata_write_failed",
                partition_prefix=partition_prefix,
                error=type(exc).__name__,
            )

        _logger.info(
            "salesforce_streaming_partition_written",
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
            raise SalesforceRawLayerWriterError(
                f"Failed to write Parquet part to s3://{self._bucket}/{s3_key}: "
                f"{type(exc).__name__}"
            ) from exc
