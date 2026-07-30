"""
Tests for MySqlRdsRawLayerWriter.

Coverage:
  - Partition path structure matches production wiring (RAW-1): single
    hyphenated "mysql-rds" source segment, no s3_prefix
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
_SOURCE_ID = "mysql-rds"
_ENTITY_ID = "mysql-rds-orders"
_RUN_ID = "run-20260612-120000000000-cd56ef78"
_SCHEMA_FP = "b" * 64
_DATE = "2026-06-12"
_TENANT_CODE = "demo"


def _make_writer() -> MySqlRdsRawLayerWriter:
    return MySqlRdsRawLayerWriter(
        s3_bucket=_BUCKET,
        region_name=_REGION,
        tenant_code=_TENANT_CODE,
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
    def test_partition_path_contains_single_hyphenated_source_segment(self) -> None:
        """RAW-1: exactly one "mysql-rds" segment — no doubled/underscored variant."""
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
        expected = (
            f"{_TENANT_CODE}/mysql-rds/{_ENTITY_ID}"
            f"/extraction_date={_DATE}/run_id={_RUN_ID}/data.parquet"
        )
        assert data_key == expected
        assert data_key.split("/")[1] == "mysql-rds"
        assert "mysql_rds" not in data_key

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
            f"{_TENANT_CODE}/mysql-rds/{_ENTITY_ID}"
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
            f"{_TENANT_CODE}/mysql-rds/{_ENTITY_ID}"
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
            f"{_TENANT_CODE}/mysql-rds/{_ENTITY_ID}"
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
            MySqlRdsRawLayerWriter(s3_bucket="", region_name=_REGION, tenant_code=_TENANT_CODE)
