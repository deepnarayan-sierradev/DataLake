"""
Tests for MySqlRdsRawLayerWriter.

Coverage:
  - Partition path structure matches spec §4.2 (mysql_rds source name)
  - Parquet file written to correct S3 key
  - Metadata JSON written alongside data.parquet
  - Payload fidelity — all fields preserved as strings
  - Missing fields become null in Parquet
  - Empty record batch → MySqlRdsRawLayerWriterError
  - Path traversal in source_id/entity_id → MySqlRdsRawLayerWriterError
"""

from __future__ import annotations

import json
from io import BytesIO

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
_BUCKET = "test-raw-bucket"
_PREFIX = "raw"
_SOURCE_ID = "mysql-rds"
_ENTITY_ID = "mysql-rds-orders"
_RUN_ID = "run-20260612-120000000000-cd56ef78"
_SCHEMA_FP = "b" * 64
_DATE = "2026-06-12"


def _make_writer() -> MySqlRdsRawLayerWriter:
    return MySqlRdsRawLayerWriter(
        s3_bucket=_BUCKET,
        s3_prefix=_PREFIX,
        region_name=_REGION,
    )


def _make_records(n: int = 3) -> list[ExtractionRecord]:
    return [
        ExtractionRecord(payload={"id": str(i), "customer_id": "42", "order_date": "2026-06-10"})
        for i in range(n)
    ]


@mock_aws
def _create_bucket() -> None:
    boto3.client("s3", region_name=_REGION).create_bucket(Bucket=_BUCKET)


class TestPartitionPath:
    @mock_aws
    def test_partition_path_contains_mysql_rds_source_name(self) -> None:
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
        assert "mysql_rds" in data_key
        assert f"extraction_date={_DATE}" in data_key
        assert f"run_id={_RUN_ID}" in data_key
        assert data_key.endswith("data.parquet")

    @mock_aws
    def test_metadata_json_fields_are_correct(self) -> None:
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
            f"{_PREFIX}/mysql_rds/{_ENTITY_ID}"
            f"/extraction_date={_DATE}/run_id={_RUN_ID}/metadata.json"
        )
        body = s3.get_object(Bucket=_BUCKET, Key=metadata_key)["Body"].read()
        metadata = json.loads(body)
        assert metadata["source_id"] == _SOURCE_ID
        assert metadata["entity_id"] == _ENTITY_ID
        assert metadata["record_count"] == 3
        assert metadata["schema_version"] == _SCHEMA_FP


class TestParquetOutput:
    @mock_aws
    def test_parquet_payload_fidelity(self) -> None:
        _create_bucket()
        writer = _make_writer()
        records = [ExtractionRecord(payload={"id": "99", "total": "199.99"})]
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
            f"{_PREFIX}/mysql_rds/{_ENTITY_ID}"
            f"/extraction_date={_DATE}/run_id={_RUN_ID}/data.parquet"
        )
        parquet_bytes = s3.get_object(Bucket=_BUCKET, Key=data_key)["Body"].read()
        table = pq.read_table(BytesIO(parquet_bytes))
        assert table.num_rows == 1
        row = {col: table.column(col)[0].as_py() for col in table.schema.names}
        assert row["id"] == "99"
        assert row["total"] == "199.99"

    @mock_aws
    def test_missing_fields_become_null(self) -> None:
        _create_bucket()
        writer = _make_writer()
        records = [
            ExtractionRecord(payload={"id": "1", "notes": "important"}),
            ExtractionRecord(payload={"id": "2"}),  # missing 'notes'
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
            f"{_PREFIX}/mysql_rds/{_ENTITY_ID}"
            f"/extraction_date={_DATE}/run_id={_RUN_ID}/data.parquet"
        )
        parquet_bytes = s3.get_object(Bucket=_BUCKET, Key=data_key)["Body"].read()
        table = pq.read_table(BytesIO(parquet_bytes))
        assert table.num_rows == 2
        assert table.column("notes")[1].is_valid is False


class TestInputValidation:
    @mock_aws
    def test_empty_records_raises(self) -> None:
        _create_bucket()
        writer = _make_writer()
        with pytest.raises(MySqlRdsRawLayerWriterError, match="empty record batch"):
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
        with pytest.raises(MySqlRdsRawLayerWriterError, match="stable ID pattern"):
            writer.write_partition(
                records=_make_records(),
                source_id=_SOURCE_ID,
                entity_id="../../malicious",
                run_id=_RUN_ID,
                schema_fingerprint=_SCHEMA_FP,
                extraction_date=_DATE,
            )

    def test_empty_bucket_name_raises(self) -> None:
        with pytest.raises(ValueError, match="s3_bucket"):
            MySqlRdsRawLayerWriter(s3_bucket="", s3_prefix="raw", region_name=_REGION)
