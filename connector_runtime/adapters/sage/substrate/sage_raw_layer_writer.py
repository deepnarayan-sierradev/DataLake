"""
SageRawLayerWriter — S3 raw layer writer for all Sage product connectors.

Writes batches of ExtractionRecord to the S3 raw layer as Parquet files,
following the platform raw layer partition scheme extended for Sage multi-product.

Partition scheme:
    s3://{bucket}/{tenant_code}/sage-{product_name}/{entity_id}/
        extraction_date={YYYY-MM-DD}/
        run_id={run_id}/
            data.parquet
            metadata.json

Design:
  - product_name is folded into a single hyphenated source segment
    ("sage-intacct", "sage-x3" — matching the one-segment, source-id-style
    convention every other adapter uses) rather than two nested path
    segments, so that two different Sage products that share an entity_id
    never collide (e.g. intacct and x3 both have a "customer" concept but
    their records are structurally different) without doubling the "sage"
    segment the way the pre-RAW-1 layout did.
  - Append-only writes — each run produces a unique partition path via run_id.
  - write_partition_streaming() keeps peak memory at O(chunk_size) for large datasets.

Security (OWASP A05, A08, A09):
  - S3 keys constructed from validated IDs only (STABLE_ID_PATTERN + product whitelist).
  - product_name validated against SUPPORTED_SAGE_PRODUCTS before path interpolation.
  - Record payloads written as-is; no values are logged.
  - IAM credentials via implicit boto3 credential chain (Lambda execution role).

DUP-1: write_partition() and _write_parquet_part() are inherited unchanged
from connector_runtime.raw_layer_writer.RawLayerWriter — that logic was
byte-for-byte identical to Salesforce/NetSuite/MySQL RDS's raw layer writers
modulo the source name. write_partition_streaming() is overridden here
because Sage's zero-record handling (raises) and chunk file naming/metadata
fields genuinely differ from the other three adapters (which warn and return
(prefix, 0)) — see RawLayerWriter.write_partition_streaming's docstring for
the shared variant this deliberately diverges from.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from connector_runtime.interfaces.connector_interface import ExtractionRecord
from connector_runtime.raw_layer_writer import (
    DEFAULT_STREAMING_CHUNK_SIZE,
    RawLayerWriter,
    RawLayerWriterError,
    build_parquet_bytes,
)
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)


class SageRawLayerWriterError(RawLayerWriterError):
    """Raised when raw layer writing fails in a way that aborts the extraction run."""


class SageRawLayerWriter(RawLayerWriter):
    """
    Writes a batch of ExtractionRecord objects to the S3 raw layer as Parquet.

    One instance per extraction run.  product_name is embedded in the S3 path
    so records from different Sage products are partitioned independently.

    Usage::

        writer = SageRawLayerWriter(
            s3_bucket="edl-raw-087972550871",
            sage_product="intacct",
            region_name="us-east-1",
            tenant_code="demo",
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

    error_cls = SageRawLayerWriterError
    log_prefix = "sage"

    def __init__(
        self,
        s3_bucket: str,
        sage_product: str,
        region_name: str,
        tenant_code: str,
    ) -> None:
        if not sage_product:
            raise ValueError("sage_product must not be empty.")
        self._sage_product = sage_product
        super().__init__(
            s3_bucket=s3_bucket,
            path_segments=[f"sage-{sage_product}"],
            region_name=region_name,
            tenant_code=tenant_code,
        )

    def _extra_metadata_fields(self) -> dict[str, Any]:
        return {"sage_product": self._sage_product}

    def _extra_log_fields(self) -> dict[str, Any]:
        return {"sage_product": self._sage_product}

    # ── Overridden: Sage's streaming zero-record / naming semantics diverge ──

    def write_partition_streaming(
        self,
        record_iter: Iterator[ExtractionRecord],
        source_id: str,
        entity_id: str,
        run_id: str,
        schema_fingerprint: str,
        extraction_date: str,
        chunk_size: int = DEFAULT_STREAMING_CHUNK_SIZE,
    ) -> tuple[str, int]:
        """
        Write records from an iterator in memory-bounded chunks to S3.

        Peak memory is O(chunk_size) regardless of total record volume.
        Each chunk is written as a separate Parquet file under the same
        partition prefix, with a sequential suffix.

        Unlike Salesforce/NetSuite/MySQL RDS's write_partition_streaming
        (RawLayerWriter's shared implementation), Sage treats zero records
        as a hard failure rather than a logged warning + (prefix, 0) — this
        is a pre-existing, deliberate behavioural difference (DUP-1), not
        something this consolidation should paper over.

        Returns:
            Tuple of (partition_prefix, total_record_count).

        Raises:
            SageRawLayerWriterError: on zero records, S3 write failure, or
                invalid inputs.
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
            raise self.error_cls(
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

    def _write_chunk(
        self,
        chunk: list[ExtractionRecord],
        partition_prefix: str,
        chunk_index: int,
    ) -> None:
        """
        Serialise and upload one streaming chunk. Uses the shared
        _write_parquet_part() upload path (RawLayerWriter) but keeps Sage's
        own data_NNNN.parquet naming convention rather than the
        part-NNNNN.parquet naming Salesforce/NetSuite/MySQL RDS use.
        """
        data_key = f"{partition_prefix}/data_{chunk_index:04d}.parquet"
        self._write_parquet_part(chunk, data_key)


# ---------------------------------------------------------------------------
# Module-level Parquet conversion helper
# ---------------------------------------------------------------------------


def _records_to_parquet(records: list[ExtractionRecord]) -> bytes:
    """
    Convert a non-empty list of ExtractionRecord to a Snappy-compressed Parquet buffer.

    All field values are normalised to strings (or None) — the raw layer stores
    source values as-is without type coercion, matching the platform convention
    established across all raw layer writers.

    Kept as a standalone, importable function (rather than folded fully into
    RawLayerWriter) because it is unit-tested directly; delegates the actual
    serialization to the shared connector_runtime.raw_layer_writer.build_parquet_bytes
    (DUP-1) instead of reimplementing it.

    Raises:
        SageRawLayerWriterError: if records is empty.
    """
    return build_parquet_bytes(records, SageRawLayerWriterError)
