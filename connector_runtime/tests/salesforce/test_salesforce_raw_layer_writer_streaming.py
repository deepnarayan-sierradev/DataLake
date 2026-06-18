"""
Tests for SalesforceRawLayerWriter.write_partition_streaming() — streaming write path.

Covers:
  - Happy path: all records written, metadata sidecar present, correct record count
  - Multi-chunk: records split across multiple part files when chunk_size exceeded
  - Zero records: returns (prefix, 0) without writing any part files
  - Metadata write failure is non-fatal (data files already persisted)
  - _write_parquet_part S3 error raises SalesforceRawLayerWriterError
  - Invalid source_id / entity_id raises SalesforceRawLayerWriterError
"""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock

import boto3
import pyarrow.parquet as pq
import pytest
from moto import mock_aws

from connector_runtime.adapters.salesforce.salesforce_raw_layer_writer import (
    SalesforceRawLayerWriter,
    SalesforceRawLayerWriterError,
)
from connector_runtime.interfaces.connector_interface import ExtractionRecord

_REGION = "us-east-1"
_BUCKET = "test-sf-streaming-bucket"
_PREFIX = "raw"
_SOURCE_ID = "salesforce"
_ENTITY_ID = "salesforce-account"
_RUN_ID = "run-20260612-120000000000-ab12cd34"
_SCHEMA_FP = "a" * 64
_DATE = "2026-06-12"


def _make_writer() -> SalesforceRawLayerWriter:
    return SalesforceRawLayerWriter(
        s3_bucket=_BUCKET,
        s3_prefix=_PREFIX,
        region_name=_REGION,
    )


def _records(n: int) -> list[ExtractionRecord]:
    return [ExtractionRecord(payload={"Id": str(i), "Name": f"Acme-{i}"}) for i in range(n)]


@mock_aws
def _setup_bucket() -> None:
    boto3.client("s3", region_name=_REGION).create_bucket(Bucket=_BUCKET)


class TestWritePartitionStreaming:
    @mock_aws
    def test_happy_path_returns_prefix_and_count(self) -> None:
        _setup_bucket()
        writer = _make_writer()
        prefix, count = writer.write_partition_streaming(
            record_iter=iter(_records(5)),
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
        )
        assert count == 5
        assert _ENTITY_ID in prefix

    @mock_aws
    def test_metadata_sidecar_written(self) -> None:
        _setup_bucket()
        writer = _make_writer()
        prefix, _ = writer.write_partition_streaming(
            record_iter=iter(_records(3)),
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
        )
        s3 = boto3.client("s3", region_name=_REGION)
        raw = s3.get_object(Bucket=_BUCKET, Key=f"{prefix}/metadata.json")["Body"].read()
        meta = json.loads(raw)
        assert meta["record_count"] == 3
        assert meta["source_id"] == _SOURCE_ID
        assert meta["entity_id"] == _ENTITY_ID

    @mock_aws
    def test_multi_chunk_produces_multiple_part_files(self) -> None:
        _setup_bucket()
        writer = _make_writer()
        prefix, count = writer.write_partition_streaming(
            record_iter=iter(_records(7)),
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
            chunk_size=3,
        )
        assert count == 7
        s3 = boto3.client("s3", region_name=_REGION)
        objects = s3.list_objects_v2(Bucket=_BUCKET, Prefix=prefix)["Contents"]
        part_files = [o for o in objects if "part-" in o["Key"]]
        # 7 records with chunk_size=3 → parts 0, 1, 2 = 3 parts
        assert len(part_files) == 3

    @mock_aws
    def test_zero_records_returns_zero_count(self) -> None:
        _setup_bucket()
        writer = _make_writer()
        prefix, count = writer.write_partition_streaming(
            record_iter=iter([]),
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
        )
        assert count == 0
        # No part files should exist
        s3 = boto3.client("s3", region_name=_REGION)
        result = s3.list_objects_v2(Bucket=_BUCKET, Prefix=prefix)
        assert result.get("KeyCount", 0) == 0

    @mock_aws
    def test_invalid_entity_id_raises_error(self) -> None:
        _setup_bucket()
        writer = _make_writer()
        with pytest.raises(SalesforceRawLayerWriterError, match="stable ID pattern"):
            writer.write_partition_streaming(
                record_iter=iter(_records(1)),
                source_id=_SOURCE_ID,
                entity_id="INVALID/entity",
                run_id=_RUN_ID,
                schema_fingerprint=_SCHEMA_FP,
                extraction_date=_DATE,
            )

    @mock_aws
    def test_invalid_source_id_raises_error(self) -> None:
        _setup_bucket()
        writer = _make_writer()
        with pytest.raises(SalesforceRawLayerWriterError, match="stable ID pattern"):
            writer.write_partition_streaming(
                record_iter=iter(_records(1)),
                source_id="INVALID SOURCE",
                entity_id=_ENTITY_ID,
                run_id=_RUN_ID,
                schema_fingerprint=_SCHEMA_FP,
                extraction_date=_DATE,
            )

    @mock_aws
    def test_parquet_parts_are_valid_parquet(self) -> None:
        _setup_bucket()
        writer = _make_writer()
        prefix, _ = writer.write_partition_streaming(
            record_iter=iter(_records(4)),
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
            chunk_size=4,
        )
        s3 = boto3.client("s3", region_name=_REGION)
        part_key = f"{prefix}/part-00000.parquet"
        body = s3.get_object(Bucket=_BUCKET, Key=part_key)["Body"].read()
        table = pq.read_table(BytesIO(body))
        assert table.num_rows == 4

    @mock_aws
    def test_metadata_failure_is_non_fatal(self) -> None:
        """Streaming metadata write failure must not raise — data is already persisted."""
        _setup_bucket()
        writer = _make_writer()
        original_put = writer._s3.put_object  # type: ignore[attr-defined]

        call_count = 0

        def failing_metadata_put(**kwargs):  # type: ignore[no-untyped-def]
            nonlocal call_count
            call_count += 1
            # Fail only the metadata.json put
            if kwargs.get("Key", "").endswith("metadata.json"):
                raise OSError("simulated metadata failure")
            return original_put(**kwargs)

        writer._s3.put_object = failing_metadata_put  # type: ignore[method-assign]

        # Should not raise
        _prefix, count = writer.write_partition_streaming(
            record_iter=iter(_records(2)),
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
        )
        assert count == 2


class TestWriteParquetPartError:
    @mock_aws
    def test_s3_write_failure_raises_salesforce_error(self) -> None:
        _setup_bucket()
        writer = _make_writer()
        writer._s3.put_object = MagicMock(side_effect=OSError("s3 down"))  # type: ignore[method-assign]

        with pytest.raises(SalesforceRawLayerWriterError, match="Failed to write Parquet part"):
            writer._write_parquet_part(_records(1), "some-key")  # type: ignore[attr-defined]
