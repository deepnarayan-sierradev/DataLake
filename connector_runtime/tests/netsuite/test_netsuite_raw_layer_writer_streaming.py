"""
Tests for NetSuiteRawLayerWriter.write_partition_streaming() and non-fatal metadata path.
"""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock

import boto3
import pyarrow.parquet as pq
import pytest
from moto import mock_aws

from connector_runtime.adapters.netsuite.netsuite_raw_layer_writer import (
    NetSuiteRawLayerWriter,
    NetSuiteRawLayerWriterError,
)
from connector_runtime.interfaces.connector_interface import ExtractionRecord

_REGION = "us-east-1"
_BUCKET = "test-netsuite-streaming"
_PREFIX = "raw"
_SOURCE_ID = "netsuite"
_ENTITY_ID = "netsuite-customer"
_RUN_ID = "run-20260612-120000000000-ab12cd34"
_SCHEMA_FP = "b" * 64
_DATE = "2026-06-12"


def _make_writer() -> NetSuiteRawLayerWriter:
    return NetSuiteRawLayerWriter(s3_bucket=_BUCKET, s3_prefix=_PREFIX, region_name=_REGION)


def _records(n: int) -> list[ExtractionRecord]:
    return [ExtractionRecord(payload={"InternalId": str(i), "Name": f"Cust-{i}"}) for i in range(n)]


@mock_aws
def _setup_bucket() -> None:
    boto3.client("s3", region_name=_REGION).create_bucket(Bucket=_BUCKET)


class TestNetSuiteStreamingWrite:
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
    def test_metadata_sidecar_present(self) -> None:
        _setup_bucket()
        writer = _make_writer()
        prefix, _ = writer.write_partition_streaming(
            record_iter=iter(_records(2)),
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
        assert meta["record_count"] == 2
        assert meta["entity_id"] == _ENTITY_ID

    @mock_aws
    def test_multi_chunk_writes_multiple_parts(self) -> None:
        _setup_bucket()
        writer = _make_writer()
        prefix, count = writer.write_partition_streaming(
            record_iter=iter(_records(5)),
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
            chunk_size=2,
        )
        assert count == 5
        s3 = boto3.client("s3", region_name=_REGION)
        objs = s3.list_objects_v2(Bucket=_BUCKET, Prefix=prefix)["Contents"]
        parts = [o for o in objs if "part-" in o["Key"]]
        assert len(parts) == 3  # ceil(5/2) = 3

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
        with pytest.raises(NetSuiteRawLayerWriterError):
            writer.write_partition_streaming(
                record_iter=iter(_records(1)),
                source_id=_SOURCE_ID,
                entity_id="BAD/entity",
                run_id=_RUN_ID,
                schema_fingerprint=_SCHEMA_FP,
                extraction_date=_DATE,
            )

    @mock_aws
    def test_metadata_failure_is_non_fatal(self) -> None:
        _setup_bucket()
        writer = _make_writer()
        original_put = writer._s3.put_object  # type: ignore[attr-defined]

        def failing_meta(**kwargs):  # type: ignore[no-untyped-def]
            if kwargs.get("Key", "").endswith("metadata.json"):
                raise OSError("metadata failure")
            return original_put(**kwargs)

        writer._s3.put_object = failing_meta  # type: ignore[method-assign]
        _, count = writer.write_partition_streaming(
            record_iter=iter(_records(2)),
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
        )
        assert count == 2

    @mock_aws
    def test_write_parquet_part_s3_error_raises(self) -> None:
        _setup_bucket()
        writer = _make_writer()
        writer._s3.put_object = MagicMock(side_effect=OSError("s3 down"))  # type: ignore[method-assign]
        with pytest.raises(NetSuiteRawLayerWriterError, match="Failed to write Parquet part"):
            writer._write_parquet_part(_records(1), "some/key")  # type: ignore[attr-defined]

    @mock_aws
    def test_part_is_valid_parquet(self) -> None:
        _setup_bucket()
        writer = _make_writer()
        prefix, _ = writer.write_partition_streaming(
            record_iter=iter(_records(3)),
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
            chunk_size=3,
        )
        s3 = boto3.client("s3", region_name=_REGION)
        body = s3.get_object(Bucket=_BUCKET, Key=f"{prefix}/part-00000.parquet")["Body"].read()
        table = pq.read_table(BytesIO(body))
        assert table.num_rows == 3
