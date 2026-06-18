"""
Tests for MySqlRdsRawLayerWriter.write_partition_streaming() and non-fatal metadata path.
"""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock

import boto3
import pyarrow.parquet as pq
import pytest
from moto import mock_aws

from connector_runtime.adapters.mysql_rds.mysql_rds_raw_layer_writer import (
    MySqlRdsRawLayerWriter,
    MySqlRdsRawLayerWriterError,
)
from connector_runtime.interfaces.connector_interface import ExtractionRecord

_REGION = "us-east-1"
_BUCKET = "test-mysql-streaming"
_PREFIX = "raw"
_SOURCE_ID = "mysql-rds"
_ENTITY_ID = "mysql-rds-orders"
_RUN_ID = "run-20260612-120000000000-ab12cd34"
_SCHEMA_FP = "c" * 64
_DATE = "2026-06-12"


def _make_writer() -> MySqlRdsRawLayerWriter:
    return MySqlRdsRawLayerWriter(s3_bucket=_BUCKET, s3_prefix=_PREFIX, region_name=_REGION)


def _records(n: int) -> list[ExtractionRecord]:
    return [ExtractionRecord(payload={"id": str(i), "amount": f"{i * 10:.2f}"}) for i in range(n)]


@mock_aws
def _setup_bucket() -> None:
    boto3.client("s3", region_name=_REGION).create_bucket(Bucket=_BUCKET)


class TestMySqlStreamingWrite:
    @mock_aws
    def test_returns_prefix_and_count(self) -> None:
        _setup_bucket()
        writer = _make_writer()
        prefix, count = writer.write_partition_streaming(
            record_iter=iter(_records(4)),
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
        )
        assert count == 4
        assert _ENTITY_ID in prefix

    @mock_aws
    def test_metadata_sidecar_contains_correct_fields(self) -> None:
        _setup_bucket()
        writer = _make_writer()
        prefix, _ = writer.write_partition_streaming(
            record_iter=iter(_records(6)),
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
        )
        s3 = boto3.client("s3", region_name=_REGION)
        meta = json.loads(
            s3.get_object(Bucket=_BUCKET, Key=f"{prefix}/metadata.json")["Body"].read()
        )
        assert meta["record_count"] == 6
        assert meta["source_id"] == _SOURCE_ID

    @mock_aws
    def test_chunk_splitting(self) -> None:
        _setup_bucket()
        writer = _make_writer()
        _, count = writer.write_partition_streaming(
            record_iter=iter(_records(10)),
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
            chunk_size=4,
        )
        assert count == 10

    @mock_aws
    def test_zero_records_returns_zero(self) -> None:
        _setup_bucket()
        writer = _make_writer()
        _, count = writer.write_partition_streaming(
            record_iter=iter([]),
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
        )
        assert count == 0

    @mock_aws
    def test_invalid_entity_id_raises(self) -> None:
        _setup_bucket()
        writer = _make_writer()
        with pytest.raises(MySqlRdsRawLayerWriterError):
            writer.write_partition_streaming(
                record_iter=iter(_records(1)),
                source_id=_SOURCE_ID,
                entity_id="BAD ENTITY",
                run_id=_RUN_ID,
                schema_fingerprint=_SCHEMA_FP,
                extraction_date=_DATE,
            )

    @mock_aws
    def test_metadata_failure_non_fatal(self) -> None:
        _setup_bucket()
        writer = _make_writer()
        original_put = writer._s3.put_object  # type: ignore[attr-defined]

        def failing_meta(**kwargs):  # type: ignore[no-untyped-def]
            if kwargs.get("Key", "").endswith("metadata.json"):
                raise OSError("boom")
            return original_put(**kwargs)

        writer._s3.put_object = failing_meta  # type: ignore[method-assign]
        _, count = writer.write_partition_streaming(
            record_iter=iter(_records(3)),
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
        )
        assert count == 3

    @mock_aws
    def test_write_parquet_part_s3_error_raises(self) -> None:
        _setup_bucket()
        writer = _make_writer()
        writer._s3.put_object = MagicMock(side_effect=OSError("s3 fail"))  # type: ignore[method-assign]
        with pytest.raises(MySqlRdsRawLayerWriterError, match="Failed to write Parquet part"):
            writer._write_parquet_part(_records(1), "x/key")  # type: ignore[attr-defined]

    @mock_aws
    def test_part_is_valid_parquet(self) -> None:
        _setup_bucket()
        writer = _make_writer()
        prefix, _ = writer.write_partition_streaming(
            record_iter=iter(_records(2)),
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
            chunk_size=2,
        )
        s3 = boto3.client("s3", region_name=_REGION)
        body = s3.get_object(Bucket=_BUCKET, Key=f"{prefix}/part-00000.parquet")["Body"].read()
        table = pq.read_table(BytesIO(body))
        assert table.num_rows == 2
