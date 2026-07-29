"""
Shared S3 Parquet writer utility for the Enterprise Data Lake platform.

Provides a single, reusable implementation that:
  - Uses a single PUT for small files (< 8 MB)
  - Automatically switches to S3 multipart upload for larger files
  - Streams records in batches — never materialises the full record set in RAM
  - Returns the total record count written

All pipeline stages that write Parquet to S3 should use this class:
  - CuratedLayerWriter (transformation)
  - GoldenRecordPublisher (entity resolution)
  - CanonicalRecordPublisher (entity resolution)
  - AnalyticsPublisherHandler (analytics)

Security (OWASP A03, A05):
  - Bucket name and S3 key are caller-provided; callers must validate them.
  - No credentials flow through this module.

Performance:
  - Peak memory per write: O(batch_size x avg_record_bytes) = ~20 MB at 50K rows.
  - Multipart parts are 64 MB each (AWS minimum is 5 MB; 64 MB is optimal).
  - PyArrow RecordBatchWriter streams directly without full-table materialisation.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from typing import Any, Final

import pyarrow as pa
import pyarrow.parquet as pq

from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# Threshold above which multipart upload is used instead of single PUT.
# Files below this size (e.g. small incremental deltas) avoid the multipart
# overhead (API calls for CreateMultipartUpload + CompleteMultipartUpload).
_MULTIPART_THRESHOLD_BYTES: Final[int] = 8 * 1024 * 1024  # 8 MB

# Each S3 part is 64 MB. AWS minimum part size is 5 MB; 64 MB is a good
# balance between API call count and per-part upload latency.
_PART_SIZE_BYTES: Final[int] = 64 * 1024 * 1024  # 64 MB

# Records are batched into Arrow RecordBatches of this size before writing.
# Peak RAM per batch: 50_000 rows x ~400 bytes = ~20 MB.
_WRITE_BATCH_SIZE: Final[int] = 50_000


class S3ParquetWriter:
    """
    Write an iterator of record dicts to S3 as a single compressed Parquet file.

    Automatically selects single-PUT or multipart upload based on file size.
    All internal buffering is bounded by _PART_SIZE_BYTES (64 MB).

    Thread safety: one instance per write call. Do not share across threads.
    """

    def __init__(self, s3_client: Any) -> None:
        self._s3 = s3_client
        # PERF-3: the schema used for the most recent write() call — either
        # the caller-supplied `schema` argument, or the schema inferred from
        # the first batch of records. Callers that need a PyArrow schema for
        # a downstream purpose (e.g. Glue Data Catalog column registration)
        # can read this instead of re-materialising the full record set into
        # a second pa.Table just to recompute a schema write() already
        # derived. None until the first write() call, and reset to None when
        # a write() call receives zero records (nothing was written).
        self.last_written_schema: pa.Schema | None = None

    def write(
        self,
        records_iter: Iterator[dict[str, Any]],
        bucket: str,
        key: str,
        schema: pa.Schema | None = None,
        compression: str = "snappy",
    ) -> int:
        """
        Write records to S3 Parquet. Returns total record count written.

        Args:
            records_iter: Iterator of record dicts to write. Consumed once.
            bucket:       S3 bucket name.
            key:          S3 object key.
            schema:       Optional PyArrow schema. Inferred from first batch if None.
            compression:  Parquet compression codec (default: snappy).

        Returns:
            Total number of records written.

        Side effect:
            Sets self.last_written_schema to the schema used for this write
            (caller-supplied or inferred), or None if zero records were written.
        """
        # Buffer the first batch to infer schema if not provided.
        first_batch: list[dict[str, Any]] = []
        remaining_iter = records_iter

        if schema is None:
            for rec in remaining_iter:
                first_batch.append(rec)
                if len(first_batch) >= _WRITE_BATCH_SIZE:
                    break
            if not first_batch:
                self.last_written_schema = None
                return 0
            inferred_schema = pa.Table.from_pylist(first_batch).schema
        else:
            inferred_schema = schema

        self.last_written_schema = inferred_schema

        # Reconstruct a single iterator: first_batch + rest of original iter.
        def _combined() -> Iterator[dict[str, Any]]:
            yield from first_batch
            yield from remaining_iter

        return self._write_multipart_or_single(
            _combined(), bucket, key, inferred_schema, compression
        )

    def _write_multipart_or_single(
        self,
        records_iter: Iterator[dict[str, Any]],
        bucket: str,
        key: str,
        schema: pa.Schema,
        compression: str,
    ) -> int:
        """Buffer Parquet output; choose single-PUT or multipart based on size."""
        buf = io.BytesIO()
        writer = pq.ParquetWriter(buf, schema, compression=compression)
        total_records = 0

        # Accumulate current batch
        batch_records: list[dict[str, Any]] = []

        def _flush_batch() -> None:
            nonlocal total_records
            if not batch_records:
                return
            arrow_table = pa.Table.from_pylist(batch_records, schema=schema)
            writer.write_table(arrow_table)
            total_records += len(batch_records)
            batch_records.clear()

        for rec in records_iter:
            batch_records.append(rec)
            if len(batch_records) >= _WRITE_BATCH_SIZE:
                _flush_batch()

        _flush_batch()
        writer.close()

        parquet_bytes = buf.getvalue()
        buf.close()

        if not parquet_bytes:
            return 0

        if len(parquet_bytes) < _MULTIPART_THRESHOLD_BYTES:
            # Small file — single PUT
            self._s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=parquet_bytes,
                ContentType="application/octet-stream",
            )
            _logger.info(
                "s3_parquet_writer_single_put",
                bucket=bucket,
                key=key,
                size_bytes=len(parquet_bytes),
                record_count=total_records,
            )
        else:
            # Large file — multipart upload
            self._multipart_upload(bucket, key, parquet_bytes)
            _logger.info(
                "s3_parquet_writer_multipart_upload",
                bucket=bucket,
                key=key,
                size_bytes=len(parquet_bytes),
                record_count=total_records,
            )

        return total_records

    def _multipart_upload(self, bucket: str, key: str, data: bytes) -> None:
        """Upload `data` to S3 using multipart upload (64 MB parts)."""
        mpu = self._s3.create_multipart_upload(
            Bucket=bucket,
            Key=key,
            ContentType="application/octet-stream",
        )
        upload_id = mpu["UploadId"]
        parts: list[dict[str, Any]] = []

        try:
            part_number = 1
            for start in range(0, len(data), _PART_SIZE_BYTES):
                chunk = data[start : start + _PART_SIZE_BYTES]
                resp = self._s3.upload_part(
                    Bucket=bucket,
                    Key=key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=chunk,
                )
                parts.append({"PartNumber": part_number, "ETag": resp["ETag"]})
                part_number += 1

            self._s3.complete_multipart_upload(
                Bucket=bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except Exception:
            # Abort multipart upload on any failure to avoid orphaned parts
            # (S3 charges for incomplete multipart uploads).
            self._s3.abort_multipart_upload(
                Bucket=bucket,
                Key=key,
                UploadId=upload_id,
            )
            raise
