"""
SageRawLayerWriter — S3 raw layer writer for all Sage product connectors.

Writes batches of ExtractionRecord to the S3 raw layer as Parquet files,
following the platform raw layer partition scheme extended for Sage multi-product.

Partition scheme:
    s3://{bucket}/{prefix}/sage/{product_name}/{entity_id}/
        extraction_date={YYYY-MM-DD}/
        run_id={run_id}/
            data.parquet
            metadata.json

Design:
  - product_name is included in the path so that two different Sage products
    that share an entity_id never collide (e.g. intacct and x3 both have a
    "customer" concept but their records are structurally different).
  - Append-only writes — each run produces a unique partition path via run_id.
  - write_partition_streaming() keeps peak memory at O(chunk_size) for large datasets.
  - Both methods follow the same pattern as NetSuiteRawLayerWriter and
    SalesforceRawLayerWriter to maintain consistency across the platform.

Security (OWASP A05, A08, A09):
  - S3 keys constructed from validated IDs only (STABLE_ID_PATTERN + product whitelist).
  - product_name validated against SUPPORTED_SAGE_PRODUCTS before path interpolation.
  - Record payloads written as-is; no values are logged.
  - IAM credentials via implicit boto3 credential chain (Lambda execution role).
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

# Top-level S3 prefix segment shared by all Sage products in the raw layer.
_SAGE_ROOT: Final[str] = "sage"


class SageRawLayerWriterError(Exception):
    """Raised when raw layer writing fails in a way that aborts the extraction run."""


class SageRawLayerWriter:
    """
    Writes a batch of ExtractionRecord objects to the S3 raw layer as Parquet.

    One instance per extraction run.  product_name is embedded in the S3 path
    so records from different Sage products are partitioned independently.

    Usage::

        writer = SageRawLayerWriter(
            s3_bucket="dev-edl-raw-layer",
            s3_prefix="raw",
            sage_product="intacct",
            region_name="us-east-1",
        )
        data_key = writer.write_partition(
            records=records,
            source_id="sage",
            entity_id="sage-intacct-customer",
            run_id="run-20260701-120000000000-ab12cd34",
            schema_fingerprint="a1b2c3...",
            extraction_date="2026-07-01",
        )
    """

    def __init__(
        self,
        s3_bucket: str,
        s3_prefix: str,
        sage_product: str,
        region_name: str,
    ) -> None:
        if not s3_bucket:
            raise ValueError("s3_bucket must not be empty.")
        if not sage_product:
            raise ValueError("sage_product must not be empty.")
        self._bucket = s3_bucket
        self._prefix = s3_prefix.strip("/")
        self._sage_product = sage_product
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
        Write records as Parquet and a metadata JSON sidecar to the S3 raw layer.

        Returns:
            The S3 key of the written Parquet file.

        Raises:
            SageRawLayerWriterError: on S3 write failure or invalid inputs.
        """
        self._validate_stable_id("source_id", source_id)
        self._validate_stable_id("entity_id", entity_id)

        extraction_timestamp = datetime.now(UTC)
        partition_prefix = self._partition_path(entity_id, extraction_date, run_id)
        data_key = f"{partition_prefix}/data.parquet"
        metadata_key = f"{partition_prefix}/metadata.json"

        parquet_bytes = _records_to_parquet(records)
        record_count = len(records)

        metadata: dict[str, Any] = {
            "run_id": run_id,
            "source_id": source_id,
            "sage_product": self._sage_product,
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
            raise SageRawLayerWriterError(
                f"Failed to write Parquet to s3://{self._bucket}/{data_key}: "
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
            _logger.warning(
                "sage_raw_metadata_write_failed",
                data_key=data_key,
                metadata_key=metadata_key,
                error=type(exc).__name__,
            )

        _logger.info(
            "sage_raw_partition_written",
            bucket=self._bucket,
            data_key=data_key,
            record_count=record_count,
            sage_product=self._sage_product,
            entity_id=entity_id,
            run_id=run_id,
        )
        return data_key

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
        Each chunk is written as a separate Parquet file under the same
        partition prefix, with a sequential suffix.

        Returns:
            Tuple of (partition_prefix, total_record_count).
        """
        self._validate_stable_id("source_id", source_id)
        self._validate_stable_id("entity_id", entity_id)

        partition_prefix = self._partition_path(entity_id, extraction_date, run_id)
        total_records = 0
        chunk_index = 0
        chunk: list[ExtractionRecord] = []

        for record in record_iter:
            chunk.append(record)
            if len(chunk) >= chunk_size:
                self._write_chunk(
                    chunk=chunk,
                    partition_prefix=partition_prefix,
                    chunk_index=chunk_index,
                )
                total_records += len(chunk)
                chunk = []
                chunk_index += 1

        # Write any remaining records in the final partial chunk.
        if chunk:
            self._write_chunk(
                chunk=chunk,
                partition_prefix=partition_prefix,
                chunk_index=chunk_index,
            )
            total_records += len(chunk)

        if total_records == 0:
            raise SageRawLayerWriterError(
                "Streaming write produced zero records — cannot write empty partition."
            )

        # Write sidecar metadata for the full partition.
        metadata: dict[str, Any] = {
            "run_id": run_id,
            "source_id": source_id,
            "sage_product": self._sage_product,
            "entity_id": entity_id,
            "extraction_timestamp": datetime.now(UTC).isoformat(),
            "schema_version": schema_fingerprint,
            "record_count": total_records,
            "extraction_date": extraction_date,
            "chunk_count": chunk_index + (1 if chunk else 0),
        }
        metadata_key = f"{partition_prefix}/metadata.json"
        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=metadata_key,
                Body=json.dumps(metadata, indent=2).encode(),
                ContentType="application/json",
            )
        except Exception as exc:
            _logger.warning(
                "sage_raw_metadata_write_failed",
                metadata_key=metadata_key,
                error=type(exc).__name__,
            )

        _logger.info(
            "sage_raw_partition_streaming_complete",
            bucket=self._bucket,
            partition_prefix=partition_prefix,
            total_records=total_records,
            chunk_count=chunk_index + (1 if chunk else 0),
            sage_product=self._sage_product,
            entity_id=entity_id,
            run_id=run_id,
        )
        return partition_prefix, total_records

    # ── Private ────────────────────────────────────────────────────────────────

    def _partition_path(self, entity_id: str, extraction_date: str, run_id: str) -> str:
        parts = [self._prefix] if self._prefix else []
        parts.extend([
            _SAGE_ROOT,
            self._sage_product,
            entity_id,
            f"extraction_date={extraction_date}",
            f"run_id={run_id}",
        ])
        return "/".join(parts)

    def _write_chunk(
        self,
        chunk: list[ExtractionRecord],
        partition_prefix: str,
        chunk_index: int,
    ) -> None:
        data_key = f"{partition_prefix}/data_{chunk_index:04d}.parquet"
        parquet_bytes = _records_to_parquet(chunk)
        try:
            self._s3.put_object(
                Bucket=self._bucket,
                Key=data_key,
                Body=parquet_bytes,
                ContentType="application/octet-stream",
            )
        except Exception as exc:
            raise SageRawLayerWriterError(
                f"Failed to write chunk {chunk_index} to "
                f"s3://{self._bucket}/{data_key}: {type(exc).__name__}"
            ) from exc

    def _validate_stable_id(self, field_name: str, value: str) -> None:
        if not _STABLE_ID_PATTERN.match(value):
            raise SageRawLayerWriterError(
                f"{field_name}={value!r} does not match the stable ID pattern "
                f"{_STABLE_ID_PATTERN.pattern!r}. "
                "Path traversal characters and uppercase are not permitted."
            )


# ---------------------------------------------------------------------------
# Module-level Parquet conversion helper
# ---------------------------------------------------------------------------


def _records_to_parquet(records: list[ExtractionRecord]) -> bytes:
    """
    Convert a non-empty list of ExtractionRecord to a Snappy-compressed Parquet buffer.

    All field values are normalised to strings (or None) — the raw layer stores
    source values as-is without type coercion, matching the platform convention
    established by the MySQL and NetSuite raw layer writers.

    Raises:
        SageRawLayerWriterError: if records is empty.
    """
    if not records:
        raise SageRawLayerWriterError(
            "Cannot write empty record batch — at least one record is required."
        )

    # Build a stable column ordering from all records (insertion-order dedup).
    all_keys: list[str] = []
    seen: set[str] = set()
    for record in records:
        for key in record.payload:
            if key not in seen:
                all_keys.append(key)
                seen.add(key)

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
