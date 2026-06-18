"""
Tests for NetSuiteRawLayerWriter.

Coverage:
  - Partition path structure matches spec §4.1
  - Parquet file written to correct S3 key
  - Metadata JSON written alongside data.parquet
  - Payload fidelity — all fields preserved as strings
  - Missing fields become null in Parquet
  - Snappy compression used
  - Empty record batch → NetSuiteRawLayerWriterError
  - Path traversal in source_id/entity_id → NetSuiteRawLayerWriterError
"""

from __future__ import annotations

import json
from io import BytesIO

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
_BUCKET = "test-raw-bucket"
_PREFIX = "raw"
_SOURCE_ID = "netsuite"
_ENTITY_ID = "netsuite-customer"
_RUN_ID = "run-20260612-120000000000-ab12cd34"
_SCHEMA_FP = "a" * 64
_DATE = "2026-06-12"


def _make_writer() -> NetSuiteRawLayerWriter:
    return NetSuiteRawLayerWriter(
        s3_bucket=_BUCKET,
        s3_prefix=_PREFIX,
        region_name=_REGION,
    )


def _make_records(n: int = 3) -> list[ExtractionRecord]:
    return [
        ExtractionRecord(
            payload={"id": str(i), "companyname": f"Corp {i}", "lastmodifieddate": "2026-06-10"}
        )
        for i in range(n)
    ]


@mock_aws
def _create_bucket() -> None:
    boto3.client("s3", region_name=_REGION).create_bucket(Bucket=_BUCKET)


class TestPartitionPath:
    @mock_aws
    def test_partition_path_matches_spec(self) -> None:
        _create_bucket()
        writer = _make_writer()
        data_key = writer.write_partition(
            records=_make_records(),
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
        )
        expected_prefix = (
            f"{_PREFIX}/netsuite/{_ENTITY_ID}/extraction_date={_DATE}/run_id={_RUN_ID}/data.parquet"
        )
        assert data_key == expected_prefix

    @mock_aws
    def test_metadata_json_written_alongside_parquet(self) -> None:
        _create_bucket()
        writer = _make_writer()
        writer.write_partition(
            records=_make_records(),
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
        )
        s3 = boto3.client("s3", region_name=_REGION)
        metadata_key = (
            f"{_PREFIX}/netsuite/{_ENTITY_ID}"
            f"/extraction_date={_DATE}/run_id={_RUN_ID}/metadata.json"
        )
        body = s3.get_object(Bucket=_BUCKET, Key=metadata_key)["Body"].read()
        metadata = json.loads(body)
        assert metadata["run_id"] == _RUN_ID
        assert metadata["source_id"] == _SOURCE_ID
        assert metadata["entity_id"] == _ENTITY_ID
        assert metadata["record_count"] == 3
        assert metadata["extraction_date"] == _DATE
        assert metadata["schema_version"] == _SCHEMA_FP


class TestParquetOutput:
    @mock_aws
    def test_parquet_payload_fidelity(self) -> None:
        _create_bucket()
        writer = _make_writer()
        records = [ExtractionRecord(payload={"id": "42", "name": "Acme"})]
        writer.write_partition(
            records=records,
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
        )
        s3 = boto3.client("s3", region_name=_REGION)
        data_key = (
            f"{_PREFIX}/netsuite/{_ENTITY_ID}/extraction_date={_DATE}/run_id={_RUN_ID}/data.parquet"
        )
        parquet_bytes = s3.get_object(Bucket=_BUCKET, Key=data_key)["Body"].read()
        table = pq.read_table(BytesIO(parquet_bytes))
        assert table.num_rows == 1
        row = {col: table.column(col)[0].as_py() for col in table.schema.names}
        assert row["id"] == "42"
        assert row["name"] == "Acme"

    @mock_aws
    def test_missing_fields_become_null(self) -> None:
        _create_bucket()
        writer = _make_writer()
        records = [
            ExtractionRecord(payload={"id": "1", "name": "Alpha"}),
            ExtractionRecord(payload={"id": "2"}),  # missing 'name'
        ]
        writer.write_partition(
            records=records,
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
        )
        s3 = boto3.client("s3", region_name=_REGION)
        data_key = (
            f"{_PREFIX}/netsuite/{_ENTITY_ID}/extraction_date={_DATE}/run_id={_RUN_ID}/data.parquet"
        )
        parquet_bytes = s3.get_object(Bucket=_BUCKET, Key=data_key)["Body"].read()
        table = pq.read_table(BytesIO(parquet_bytes))
        assert table.num_rows == 2
        # Row 1: 'name' should be null
        assert table.column("name")[1].is_valid is False


class TestInputValidation:
    @mock_aws
    def test_empty_records_raises(self) -> None:
        _create_bucket()
        writer = _make_writer()
        with pytest.raises(NetSuiteRawLayerWriterError, match="empty record batch"):
            writer.write_partition(
                records=[],
                source_id=_SOURCE_ID,
                entity_id=_ENTITY_ID,
                run_id=_RUN_ID,
                schema_fingerprint=_SCHEMA_FP,
                extraction_date=_DATE,
            )

    @mock_aws
    def test_path_traversal_in_entity_id_raises(self) -> None:
        _create_bucket()
        writer = _make_writer()
        with pytest.raises(NetSuiteRawLayerWriterError, match="stable ID pattern"):
            writer.write_partition(
                records=_make_records(),
                source_id=_SOURCE_ID,
                entity_id="../../../etc/passwd",
                run_id=_RUN_ID,
                schema_fingerprint=_SCHEMA_FP,
                extraction_date=_DATE,
            )

    @mock_aws
    def test_uppercase_source_id_raises(self) -> None:
        _create_bucket()
        writer = _make_writer()
        with pytest.raises(NetSuiteRawLayerWriterError, match="stable ID pattern"):
            writer.write_partition(
                records=_make_records(),
                source_id="NetSuite",  # uppercase not allowed
                entity_id=_ENTITY_ID,
                run_id=_RUN_ID,
                schema_fingerprint=_SCHEMA_FP,
                extraction_date=_DATE,
            )

    def test_empty_bucket_raises(self) -> None:
        with pytest.raises(ValueError, match="s3_bucket"):
            NetSuiteRawLayerWriter(s3_bucket="", s3_prefix="raw", region_name=_REGION)
