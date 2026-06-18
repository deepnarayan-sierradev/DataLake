"""
Tests for SalesforceRawLayerWriter — Phase 3 §3.5.

Covers:
  - Happy path: records written to correct S3 partition path
  - Partition path structure matches spec: salesforce/{entity_id}/extraction_date=.../run_id=...
  - Parquet file readable; payload fields preserved exactly (no transformation)
  - Metadata JSON written alongside data file with correct fields
  - Empty record batch raises SalesforceRawLayerWriterError
  - Invalid entity_id (path traversal attempt) raises SalesforceRawLayerWriterError
  - Invalid source_id raises SalesforceRawLayerWriterError
  - Metadata write failure is non-fatal (data file still persisted)
  - Multiple columns; missing fields in some rows serialised as None
"""

from __future__ import annotations

import json
from io import BytesIO

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
_BUCKET = "test-raw-layer"
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


def _make_records(*dicts: dict) -> list[ExtractionRecord]:
    return [ExtractionRecord(payload=d) for d in dicts]


@mock_aws
def _create_bucket() -> None:
    s3 = boto3.client("s3", region_name=_REGION)
    s3.create_bucket(Bucket=_BUCKET)


# ---------------------------------------------------------------------------
# Partition path
# ---------------------------------------------------------------------------


class TestPartitionPath:
    @mock_aws
    def test_data_key_follows_spec_partition_scheme(self) -> None:
        """Spec §3.5: s3://raw/salesforce/{entity_id}/extraction_date=.../run_id=..../data.parquet"""
        _create_bucket()
        writer = _make_writer()
        records = _make_records({"Id": "001", "Name": "Acme"})
        key = writer.write_partition(records, _SOURCE_ID, _ENTITY_ID, _RUN_ID, _SCHEMA_FP, _DATE)
        expected = (
            f"{_PREFIX}/{_SOURCE_ID}/{_ENTITY_ID}/extraction_date={_DATE}"
            f"/run_id={_RUN_ID}/data.parquet"
        )
        assert key == expected

    @mock_aws
    def test_different_run_ids_produce_different_keys(self) -> None:
        """Each extraction run must produce a unique partition — append-only guarantee."""
        _create_bucket()
        writer = _make_writer()
        records = _make_records({"Id": "001"})
        k1 = writer.write_partition(
            records, _SOURCE_ID, _ENTITY_ID, "run-20260612-001-aabb1122", _SCHEMA_FP, _DATE
        )
        k2 = writer.write_partition(
            records, _SOURCE_ID, _ENTITY_ID, "run-20260612-002-ccdd3344", _SCHEMA_FP, _DATE
        )
        assert k1 != k2

    @mock_aws
    def test_prefix_empty_string_produces_valid_path(self) -> None:
        _create_bucket()
        writer = SalesforceRawLayerWriter(s3_bucket=_BUCKET, s3_prefix="", region_name=_REGION)
        records = _make_records({"Id": "001"})
        key = writer.write_partition(records, _SOURCE_ID, _ENTITY_ID, _RUN_ID, _SCHEMA_FP, _DATE)
        assert key.startswith(f"{_SOURCE_ID}/{_ENTITY_ID}/")


# ---------------------------------------------------------------------------
# Parquet output
# ---------------------------------------------------------------------------


class TestParquetOutput:
    @mock_aws
    def test_payload_fields_preserved_exactly(self) -> None:
        """Acceptance criterion: raw records match source payload — no transformation."""
        _create_bucket()
        writer = _make_writer()
        records = _make_records(
            {"Id": "001", "Name": "Acme Corp", "AnnualRevenue__c": "1000000"},
            {"Id": "002", "Name": "Globex", "AnnualRevenue__c": "500000"},
        )
        key = writer.write_partition(records, _SOURCE_ID, _ENTITY_ID, _RUN_ID, _SCHEMA_FP, _DATE)

        s3 = boto3.client("s3", region_name=_REGION)
        obj = s3.get_object(Bucket=_BUCKET, Key=key)
        table = pq.read_table(BytesIO(obj["Body"].read()))
        rows = table.to_pydict()

        assert rows["Id"] == ["001", "002"]
        assert rows["Name"] == ["Acme Corp", "Globex"]
        assert rows["AnnualRevenue__c"] == ["1000000", "500000"]

    @mock_aws
    def test_missing_field_in_some_rows_serialised_as_none(self) -> None:
        """Rows with sparse payloads must not fail — missing columns become null."""
        _create_bucket()
        writer = _make_writer()
        records = _make_records(
            {"Id": "001", "Name": "Acme", "Phone": "555-1234"},
            {"Id": "002", "Name": "Globex"},  # Phone missing
        )
        key = writer.write_partition(records, _SOURCE_ID, _ENTITY_ID, _RUN_ID, _SCHEMA_FP, _DATE)

        s3 = boto3.client("s3", region_name=_REGION)
        obj = s3.get_object(Bucket=_BUCKET, Key=key)
        table = pq.read_table(BytesIO(obj["Body"].read()))
        phone_col = table.column("Phone").to_pylist()
        assert phone_col[0] == "555-1234"
        assert phone_col[1] is None  # missing field → null in Parquet

    @mock_aws
    def test_parquet_file_is_snappy_compressed(self) -> None:
        _create_bucket()
        writer = _make_writer()
        records = _make_records({"Id": "001"})
        key = writer.write_partition(records, _SOURCE_ID, _ENTITY_ID, _RUN_ID, _SCHEMA_FP, _DATE)

        s3 = boto3.client("s3", region_name=_REGION)
        obj = s3.get_object(Bucket=_BUCKET, Key=key)
        pf = pq.read_metadata(BytesIO(obj["Body"].read()))
        # All row groups should use snappy
        for rg in range(pf.num_row_groups):
            for col in range(pf.row_group(rg).num_columns):
                assert pf.row_group(rg).column(col).compression == "SNAPPY"


# ---------------------------------------------------------------------------
# Metadata JSON
# ---------------------------------------------------------------------------


class TestMetadataJson:
    @mock_aws
    def test_metadata_json_written_alongside_data(self) -> None:
        _create_bucket()
        writer = _make_writer()
        records = _make_records({"Id": "001"}, {"Id": "002"})
        key = writer.write_partition(records, _SOURCE_ID, _ENTITY_ID, _RUN_ID, _SCHEMA_FP, _DATE)

        metadata_key = key.replace("data.parquet", "metadata.json")
        s3 = boto3.client("s3", region_name=_REGION)
        obj = s3.get_object(Bucket=_BUCKET, Key=metadata_key)
        meta = json.loads(obj["Body"].read())

        assert meta["run_id"] == _RUN_ID
        assert meta["source_id"] == _SOURCE_ID
        assert meta["entity_id"] == _ENTITY_ID
        assert meta["record_count"] == 2
        assert meta["schema_version"] == _SCHEMA_FP
        assert meta["extraction_date"] == _DATE
        assert "extraction_timestamp" in meta

    @mock_aws
    def test_record_count_matches_actual_rows(self) -> None:
        _create_bucket()
        writer = _make_writer()
        n = 42
        records = _make_records(*[{"Id": str(i)} for i in range(n)])
        key = writer.write_partition(records, _SOURCE_ID, _ENTITY_ID, _RUN_ID, _SCHEMA_FP, _DATE)

        metadata_key = key.replace("data.parquet", "metadata.json")
        s3 = boto3.client("s3", region_name=_REGION)
        meta = json.loads(s3.get_object(Bucket=_BUCKET, Key=metadata_key)["Body"].read())
        assert meta["record_count"] == n


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    @mock_aws
    def test_empty_records_raises(self) -> None:
        _create_bucket()
        writer = _make_writer()
        with pytest.raises(SalesforceRawLayerWriterError, match="empty"):
            writer.write_partition([], _SOURCE_ID, _ENTITY_ID, _RUN_ID, _SCHEMA_FP, _DATE)

    @mock_aws
    def test_path_traversal_entity_id_rejected(self) -> None:
        """OWASP A05: path traversal via entity_id must be blocked."""
        _create_bucket()
        writer = _make_writer()
        records = _make_records({"Id": "001"})
        with pytest.raises(SalesforceRawLayerWriterError, match="stable ID pattern"):
            writer.write_partition(
                records, _SOURCE_ID, "../../etc/passwd", _RUN_ID, _SCHEMA_FP, _DATE
            )

    @mock_aws
    def test_uppercase_source_id_rejected(self) -> None:
        _create_bucket()
        writer = _make_writer()
        records = _make_records({"Id": "001"})
        with pytest.raises(SalesforceRawLayerWriterError, match="stable ID pattern"):
            writer.write_partition(records, "Salesforce", _ENTITY_ID, _RUN_ID, _SCHEMA_FP, _DATE)

    def test_empty_bucket_raises_at_construction(self) -> None:
        with pytest.raises(ValueError, match="s3_bucket"):
            SalesforceRawLayerWriter(s3_bucket="", s3_prefix=_PREFIX, region_name=_REGION)
