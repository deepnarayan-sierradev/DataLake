"""
Shared S3 raw-layer Parquet writer base (DUP-1).

SalesforceRawLayerWriter, NetSuiteRawLayerWriter, MySqlRdsRawLayerWriter, and
SageRawLayerWriter were near byte-for-byte identical: same write_partition,
write_partition_streaming, _records_to_parquet, _partition_path, and
_validate_stable_id logic, differing only by the source-name segment(s) in
the S3 partition path (and, for Sage, the product name). This module
extracts that shared logic into RawLayerWriter, built on top of
observability.s3_writer.S3ParquetWriter for the actual Parquet
serialization + S3 upload (automatic single-PUT vs multipart selection by
file size) instead of every adapter reimplementing that primitive from
scratch.

Partition scheme (shared across all sources) — the canonical, final layout:
    s3://{bucket}/{tenant_code}/{source}/{entity_id}/
        extraction_date={YYYY-MM-DD}/
        run_id={run_id}/
            data.parquet
            metadata.json

``{source}`` is exactly ONE hyphenated, source-id-style path segment (e.g.
"salesforce", "netsuite", "mysql-rds", "sage-intacct", "sage-x3") — supplied
by each subclass's ``path_segments`` argument to ``RawLayerWriter.__init__``.
Prior to the RAW-1 fix (2026-07-08), connectors additionally passed an
``s3_prefix`` equal to the source name (e.g. ``s3_prefix="salesforce"``) on
top of the writer's own ``path_segments=["salesforce"]``, doubling the source
segment in every production raw key (``{tenant}/salesforce/salesforce/...``)
and, for MySQL RDS, mixing underscore (``mysql_rds``) and hyphen
(``mysql-rds``) spelling between the two occurrences. RawLayerWriter no
longer accepts an ``s3_prefix`` — ``path_segments`` is the only mechanism for
composing the source portion of the key, and it stays a list (rather than a
single str) so multi-part sources remain representable without inventing a
second parameter.

Design requirements (unchanged from the pre-consolidation writers):
  - Append-only writes — each run produces a unique partition path via run_id.
  - Raw records match source payload structure with no transformation
    artifacts — every field value is normalised to a string (or null); type
    coercion is the curated layer's responsibility (Phase 6).
  - write_partition_streaming() keeps peak memory at O(chunk_size).

Behavioural note (DUP-1): write_partition() and _write_parquet_part() are
fully shared across all four adapters — that part of the original code was
identical modulo the source name. write_partition_streaming()'s zero-record
handling is NOT identical: Salesforce/NetSuite/MySQL RDS log a warning and
return (prefix, 0), while Sage raises SageRawLayerWriterError. This base
implements the warn-and-return-zero variant; SageRawLayerWriter overrides
write_partition_streaming() to preserve its own (pre-existing) behaviour
rather than forcing every adapter into one convention.

Security (OWASP A05, A09):
  - S3 keys constructed from validated entity_id/source_id (stable-id
    pattern check) to prevent path traversal.
  - Record payloads are written as-is — no values are logged.
  - IAM credentials come from the implicit boto3 credential chain (IAM role).
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, Final

import boto3
import pyarrow as pa
import pyarrow.parquet as pq

from connector_runtime.interfaces.connector_interface import ExtractionRecord
from contracts.identifier_policy import STABLE_ID_PATTERN as _STABLE_ID_PATTERN
from contracts.identifier_policy import validate_tenant_code
from observability.s3_writer import S3ParquetWriter
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# Default chunk size for write_partition_streaming — matches the convention
# every raw layer writer used prior to consolidation.
DEFAULT_STREAMING_CHUNK_SIZE: Final[int] = 50_000


class RawLayerWriterError(Exception):
    """Shared base for raw-layer writer failures across all source adapters."""


def _schema_for_records(
    records: list[ExtractionRecord], error_cls: type[Exception]
) -> tuple[list[str], pa.Schema]:
    """
    Compute the ordered, deduplicated column list and an all-large_utf8
    PyArrow schema for a non-empty batch of ExtractionRecord.

    Raises:
        error_cls: if records is empty — at least one record is required to
            infer a raw-layer schema.
    """
    if not records:
        raise error_cls("Cannot write empty record batch — at least one record is required.")

    seen: set[str] = set()
    all_keys: list[str] = []
    for record in records:
        for key in record.payload:
            if key not in seen:
                seen.add(key)
                all_keys.append(key)

    schema = pa.schema([(key, pa.large_utf8()) for key in all_keys])
    return all_keys, schema


def _stringify_payload(record: ExtractionRecord, all_keys: list[str]) -> dict[str, Any]:
    """
    Project one ExtractionRecord's payload onto the full column set,
    normalising every present value to a string and every absent/None value
    to None. The raw layer stores source values as-is (as strings) — no
    type coercion is applied here.
    """
    return {
        key: (None if record.payload.get(key) is None else str(record.payload[key]))
        for key in all_keys
    }


def build_parquet_bytes(records: list[ExtractionRecord], error_cls: type[Exception]) -> bytes:
    """
    Serialise a non-empty list of ExtractionRecord payloads to a single
    Snappy-compressed Parquet byte string with all-large_utf8 columns.

    This is the standalone (no S3 I/O) byte-producing primitive — used
    directly where a caller needs raw Parquet bytes without an upload (e.g.
    SageRawLayerWriter's module-level _records_to_parquet() convenience
    function, kept for backward compatibility since it is unit-tested
    directly). The S3-integrated write path (RawLayerWriter.write_partition
    et al.) goes through S3ParquetWriter instead, to get automatic
    single-PUT/multipart selection for free.

    Raises:
        error_cls: if records is empty.
    """
    all_keys, schema = _schema_for_records(records, error_cls)

    columns: dict[str, list[Any]] = {key: [] for key in all_keys}
    for record in records:
        payload = _stringify_payload(record, all_keys)
        for key in all_keys:
            columns[key].append(payload[key])

    arrays = [pa.array(columns[key], type=pa.large_utf8()) for key in all_keys]
    table = pa.table(dict(zip(all_keys, arrays, strict=True)), schema=schema)

    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    return buf.getvalue()


class RawLayerWriter:
    """
    Writes ExtractionRecord batches to the S3 raw layer as Parquet.

    Shared by every source adapter's raw layer writer. Subclasses set the
    class attributes `error_cls` (their existing, publicly-imported
    exception type) and `log_prefix` (structured-log event name / metadata
    prefix — e.g. "salesforce", "netsuite"), and pass their source-specific
    S3 partition-path segments to __init__ (e.g. ["salesforce"],
    ["mysql-rds"], [f"sage-{sage_product}"]). Connectors do not pass an
    S3 prefix separately — the writer's `path_segments` is the single source
    of truth for the source portion of the key (see module docstring).

    One instance per extraction run. Responsible only for persistence — no
    transformation or field filtering is applied.
    """

    #: Exception type raised on write failure — override per adapter to keep
    #: each connector's existing, publicly-imported error type.
    error_cls: type[Exception] = RawLayerWriterError

    #: Structured-log event name / metadata-warning prefix (e.g. "salesforce").
    log_prefix: str = "raw_layer"

    def __init__(
        self,
        s3_bucket: str,
        path_segments: list[str],
        region_name: str,
        tenant_code: str,
        connection_id: str | None = None,
    ) -> None:
        if not s3_bucket:
            raise ValueError("s3_bucket must not be empty.")
        if not path_segments:
            raise ValueError("path_segments must not be empty.")
        self._bucket = s3_bucket
        # DL-SCOPE-04: a non-default connection gets its own prefix under the source
        # segment so two franchisees on one connector type never interleave rows.
        self._path_segments = list(path_segments)
        if connection_id and connection_id not in self._path_segments:
            self._path_segments.append(connection_id)
        self._connection_id = connection_id
        self._tenant_code = validate_tenant_code(tenant_code)
        self._s3 = boto3.client("s3", region_name=region_name)
        self._parquet_writer = S3ParquetWriter(self._s3)

    # ── Public API (RawLayerWriterProtocol) ──────────────────────────────────

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
        entity_id and run_id. The path is append-only — each unique run_id
        produces a distinct partition.

        Returns:
            The S3 key of the written Parquet file.

        Raises:
            self.error_cls: on S3 write failure or invalid inputs.
        """
        self._validate_stable_id("source_id", source_id)
        self._validate_stable_id("entity_id", entity_id)

        extraction_timestamp = datetime.now(UTC)
        partition_prefix = self._partition_path(entity_id, extraction_date, run_id)
        data_key = f"{partition_prefix}/data.parquet"
        metadata_key = f"{partition_prefix}/metadata.json"

        record_count = len(records)
        self._write_data_parquet(records, data_key)

        metadata: dict[str, Any] = {
            "run_id": run_id,
            "source_id": source_id,
            **self._extra_metadata_fields(),
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
                Key=metadata_key,
                Body=json.dumps(metadata, indent=2).encode(),
                ContentType="application/json",
            )
        except Exception as exc:
            # Metadata write failure is logged but not fatal — the Parquet
            # data is already persisted. Metadata can be reconstructed from
            # the run audit log table.
            _logger.warning(
                f"{self.log_prefix}_raw_metadata_write_failed",
                data_key=data_key,
                metadata_key=metadata_key,
                error=type(exc).__name__,
            )

        _logger.info(
            f"{self.log_prefix}_raw_partition_written",
            bucket=self._bucket,
            data_key=data_key,
            record_count=record_count,
            entity_id=entity_id,
            run_id=run_id,
            **self._extra_log_fields(),
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
        chunk_size: int = DEFAULT_STREAMING_CHUNK_SIZE,
    ) -> tuple[str, int]:
        """
        Write records from an iterator in memory-bounded chunks to S3.

        Never buffers more than ``chunk_size`` records in heap simultaneously.
        Each chunk is serialised to Parquet and uploaded as a separate part
        file, so peak memory is O(chunk_size) regardless of total record volume.

        Zero records: logs a warning and returns (partition_prefix, 0)
        without writing any part files or metadata. (SageRawLayerWriter
        overrides this method — it raises on zero records instead; see that
        class for why.)

        Returns:
            Tuple of (partition_prefix, total_record_count).

        Raises:
            self.error_cls: on S3 write failure or invalid inputs.
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

        # Flush remaining records (the last partial chunk).
        if chunk:
            part_key = f"{partition_prefix}/part-{part_index:05d}.parquet"
            self._write_parquet_part(chunk, part_key)
            part_index += 1

        if total_count == 0:
            _logger.warning(
                f"{self.log_prefix}_streaming_write_zero_records",
                entity_id=entity_id,
                run_id=run_id,
            )
            return partition_prefix, 0

        # Write metadata sidecar once all parts are written.
        metadata_key = f"{partition_prefix}/metadata.json"
        metadata: dict[str, Any] = {
            "run_id": run_id,
            "source_id": source_id,
            **self._extra_metadata_fields(),
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
                f"{self.log_prefix}_streaming_metadata_write_failed",
                partition_prefix=partition_prefix,
                error=type(exc).__name__,
            )

        _logger.info(
            f"{self.log_prefix}_streaming_partition_written",
            bucket=self._bucket,
            partition_prefix=partition_prefix,
            total_record_count=total_count,
            part_count=part_index,
            entity_id=entity_id,
            run_id=run_id,
            **self._extra_log_fields(),
        )
        return partition_prefix, total_count

    # ── Extension hooks ───────────────────────────────────────────────────────

    def _extra_metadata_fields(self) -> dict[str, Any]:
        """
        Additional fields merged into every metadata.json sidecar.
        Override for source-specific fields (e.g. Sage's sage_product).
        """
        return {}

    def _extra_log_fields(self) -> dict[str, Any]:
        """
        Additional structured-log kwargs for partition-written events.
        Override for source-specific fields.
        """
        return {}

    # ── Shared helpers ────────────────────────────────────────────────────────

    def _partition_path(self, entity_id: str, extraction_date: str, run_id: str) -> str:
        """
        Build the S3 partition prefix.

        Format: {tenant_code}/{path_segments...}/{entity_id}/
                extraction_date={date}/run_id={run_id}

        The tenant_code root segment (ARCH-1) matches the convention already
        used by the curated and schema-snapshot layers — without it, two
        tenants' raw data for the same source/entity is indistinguishable to
        any downstream reader scanning "all raw data for this entity."
        """
        parts = [
            self._tenant_code,
            *self._path_segments,
            entity_id,
            f"extraction_date={extraction_date}",
            f"run_id={run_id}",
        ]
        return "/".join(parts)

    def _validate_stable_id(self, field_name: str, value: str) -> None:
        """
        Raise self.error_cls when value fails the stable-id pattern.

        Prevents S3 path traversal by ensuring entity_id and source_id contain
        only lowercase alphanumeric characters and hyphens.
        """
        if not _STABLE_ID_PATTERN.match(value):
            raise self.error_cls(
                f"{field_name}={value!r} does not match the stable ID pattern "
                f"{_STABLE_ID_PATTERN.pattern!r}. "
                "Path traversal characters and uppercase are not permitted."
            )

    def _write_data_parquet(self, records: list[ExtractionRecord], key: str) -> None:
        """Serialise the full record batch to Parquet and upload it as the partition's data file."""
        self._upload_records(records, key, part_label="")

    def _write_parquet_part(self, records: list[ExtractionRecord], s3_key: str) -> None:
        """Serialise one streaming chunk of records to Parquet and upload it as a part file."""
        self._upload_records(records, s3_key, part_label=" part")

    def _upload_records(
        self,
        records: list[ExtractionRecord],
        key: str,
        part_label: str,
    ) -> None:
        """
        Shared upload path for both write_partition (part_label="") and
        write_partition_streaming (part_label=" part") — only the error
        message differs, matching each pre-consolidation writer's original
        wording exactly.

        Delegates the actual serialization + S3 upload to S3ParquetWriter,
        which automatically selects single-PUT vs multipart upload by size
        (DUP-1) — none of these writers previously got that for free.
        """
        all_keys, schema = _schema_for_records(records, self.error_cls)
        dict_iter = (_stringify_payload(record, all_keys) for record in records)
        try:
            self._parquet_writer.write(
                dict_iter,
                bucket=self._bucket,
                key=key,
                schema=schema,
            )
        except Exception as exc:
            raise self.error_cls(
                f"Failed to write Parquet{part_label} to s3://{self._bucket}/{key}: "
                f"{type(exc).__name__}"
            ) from exc
