"""
Tests for SageRawLayerWriter.

Coverage:
  - Constructor validates empty s3_bucket and empty sage_product
  - Partition path matches platform spec: {prefix}/sage/{product}/{entity}/
    extraction_date={date}/run_id={run_id}/data.parquet
  - product_name embedded in partition path (avoids cross-product collisions)
  - Parquet file written to correct S3 key
  - Metadata JSON sidecar written alongside data.parquet with required fields
  - Payload fidelity: all fields preserved as strings
  - Missing fields become null in Parquet (sparse records supported)
  - Numeric values normalised to strings
  - Empty record batch → SageRawLayerWriterError
  - source_id / entity_id with path traversal characters → SageRawLayerWriterError
  - S3 Parquet put failure → SageRawLayerWriterError
  - Metadata write failure is WARNING (does not abort the run)
  - write_partition_streaming: basic streaming with single chunk
  - write_partition_streaming: multiple chunks → separate Parquet files
  - write_partition_streaming: returns (partition_prefix, total_record_count)
  - write_partition_streaming: empty iterator → SageRawLayerWriterError
  - Snappy compression used
  - No prefix → path starts directly with sage/
"""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

import boto3
import pyarrow.parquet as pq
import pytest
from moto import mock_aws

from connector_runtime.adapters.sage.common.sage_raw_layer_writer import (
    SageRawLayerWriter,
    SageRawLayerWriterError,
    _records_to_parquet,
)
from connector_runtime.interfaces.connector_interface import ExtractionRecord

_REGION = "us-east-1"
_BUCKET = "test-raw-bucket"
_PREFIX = "raw"
_SOURCE_ID = "sage"
_ENTITY_ID = "sage-intacct-customer"
_PRODUCT = "intacct"
_RUN_ID = "run-20260701-120000000000-ab12cd34"
_SCHEMA_FP = "a" * 64
_DATE = "2026-07-01"


def _make_writer(prefix: str = _PREFIX) -> SageRawLayerWriter:
    return SageRawLayerWriter(
        s3_bucket=_BUCKET,
        s3_prefix=prefix,
        sage_product=_PRODUCT,
        region_name=_REGION,
    )


def _make_records(n: int = 3) -> list[ExtractionRecord]:
    return [
        ExtractionRecord(
            payload={"key": str(i), "id": f"C{i:03d}", "name": f"Corp {i}"}
        )
        for i in range(n)
    ]


@mock_aws
def _create_bucket() -> None:
    boto3.client("s3", region_name=_REGION).create_bucket(Bucket=_BUCKET)


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestConstructorValidation:
    def test_empty_bucket_raises(self) -> None:
        with pytest.raises(ValueError, match="s3_bucket"):
            SageRawLayerWriter(
                s3_bucket="",
                s3_prefix=_PREFIX,
                sage_product=_PRODUCT,
                region_name=_REGION,
            )

    def test_empty_sage_product_raises(self) -> None:
        with pytest.raises(ValueError, match="sage_product"):
            SageRawLayerWriter(
                s3_bucket=_BUCKET,
                s3_prefix=_PREFIX,
                sage_product="",
                region_name=_REGION,
            )


# ---------------------------------------------------------------------------
# Partition path
# ---------------------------------------------------------------------------


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
        expected = (
            f"{_PREFIX}/sage/{_PRODUCT}/{_ENTITY_ID}"
            f"/extraction_date={_DATE}/run_id={_RUN_ID}/data.parquet"
        )
        assert data_key == expected

    @mock_aws
    def test_product_name_in_path(self) -> None:
        _create_bucket()
        writer = SageRawLayerWriter(
            s3_bucket=_BUCKET,
            s3_prefix=_PREFIX,
            sage_product="x3",
            region_name=_REGION,
        )
        data_key = writer.write_partition(
            records=_make_records(),
            source_id=_SOURCE_ID,
            entity_id="sage-x3-customer",
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
        )
        assert "/sage/x3/" in data_key

    @mock_aws
    def test_empty_prefix_path_starts_with_sage(self) -> None:
        _create_bucket()
        writer = _make_writer(prefix="")
        data_key = writer.write_partition(
            records=_make_records(),
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
        )
        assert data_key.startswith(f"sage/{_PRODUCT}/{_ENTITY_ID}/")


# ---------------------------------------------------------------------------
# write_partition
# ---------------------------------------------------------------------------


class TestWritePartition:
    @mock_aws
    def test_parquet_written_to_s3(self) -> None:
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
        s3 = boto3.client("s3", region_name=_REGION)
        obj = s3.get_object(Bucket=_BUCKET, Key=data_key)
        parquet_bytes = obj["Body"].read()
        table = pq.read_table(BytesIO(parquet_bytes))
        assert table.num_rows == 3

    @mock_aws
    def test_parquet_payload_fidelity(self) -> None:
        _create_bucket()
        writer = _make_writer()
        records = [ExtractionRecord(payload={"key": "42", "name": "Acme Corp"})]
        data_key = writer.write_partition(
            records=records,
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
        )
        s3 = boto3.client("s3", region_name=_REGION)
        parquet_bytes = s3.get_object(Bucket=_BUCKET, Key=data_key)["Body"].read()
        table = pq.read_table(BytesIO(parquet_bytes))
        row = {col: table.column(col)[0].as_py() for col in table.schema.names}
        assert row["key"] == "42"
        assert row["name"] == "Acme Corp"

    @mock_aws
    def test_numeric_values_normalised_to_strings(self) -> None:
        _create_bucket()
        writer = _make_writer()
        records = [ExtractionRecord(payload={"key": "1", "amount": 12345.67})]
        data_key = writer.write_partition(
            records=records,
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
        )
        s3 = boto3.client("s3", region_name=_REGION)
        parquet_bytes = s3.get_object(Bucket=_BUCKET, Key=data_key)["Body"].read()
        table = pq.read_table(BytesIO(parquet_bytes))
        assert table.column("amount")[0].as_py() == "12345.67"

    @mock_aws
    def test_missing_fields_become_null(self) -> None:
        _create_bucket()
        writer = _make_writer()
        records = [
            ExtractionRecord(payload={"key": "1", "name": "Alpha"}),
            ExtractionRecord(payload={"key": "2"}),  # 'name' absent
        ]
        data_key = writer.write_partition(
            records=records,
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
        )
        s3 = boto3.client("s3", region_name=_REGION)
        parquet_bytes = s3.get_object(Bucket=_BUCKET, Key=data_key)["Body"].read()
        table = pq.read_table(BytesIO(parquet_bytes))
        assert table.column("name")[1].as_py() is None

    @mock_aws
    def test_metadata_sidecar_written(self) -> None:
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
            f"{_PREFIX}/sage/{_PRODUCT}/{_ENTITY_ID}"
            f"/extraction_date={_DATE}/run_id={_RUN_ID}/metadata.json"
        )
        body = s3.get_object(Bucket=_BUCKET, Key=metadata_key)["Body"].read()
        metadata = json.loads(body)
        assert metadata["run_id"] == _RUN_ID
        assert metadata["source_id"] == _SOURCE_ID
        assert metadata["sage_product"] == _PRODUCT
        assert metadata["entity_id"] == _ENTITY_ID
        assert metadata["record_count"] == 3
        assert metadata["extraction_date"] == _DATE
        assert metadata["schema_version"] == _SCHEMA_FP

    @mock_aws
    def test_metadata_write_failure_does_not_raise(self) -> None:
        """Metadata sidecar failure must be a WARNING — the extraction still succeeds."""
        _create_bucket()
        writer = _make_writer()
        # Patch put_object to fail only for metadata.json
        original_put = writer._s3.put_object  # type: ignore[attr-defined]

        def selective_fail(**kwargs: object) -> object:
            if str(kwargs.get("Key", "")).endswith("metadata.json"):
                raise RuntimeError("Simulated metadata write failure")
            return original_put(**kwargs)

        writer._s3.put_object = selective_fail  # type: ignore[attr-defined]
        # Should complete without raising.
        data_key = writer.write_partition(
            records=_make_records(),
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
        )
        assert data_key.endswith("data.parquet")

    @mock_aws
    def test_s3_parquet_write_failure_raises(self) -> None:
        _create_bucket()
        writer = _make_writer()
        writer._s3.put_object = MagicMock(  # type: ignore[attr-defined]
            side_effect=RuntimeError("S3 unavailable")
        )
        with pytest.raises(SageRawLayerWriterError, match="Failed to write Parquet"):
            writer.write_partition(
                records=_make_records(),
                source_id=_SOURCE_ID,
                entity_id=_ENTITY_ID,
                run_id=_RUN_ID,
                schema_fingerprint=_SCHEMA_FP,
                extraction_date=_DATE,
            )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    @mock_aws
    def test_invalid_source_id_raises(self) -> None:
        _create_bucket()
        writer = _make_writer()
        with pytest.raises(SageRawLayerWriterError, match="source_id"):
            writer.write_partition(
                records=_make_records(),
                source_id="../../../etc/passwd",
                entity_id=_ENTITY_ID,
                run_id=_RUN_ID,
                schema_fingerprint=_SCHEMA_FP,
                extraction_date=_DATE,
            )

    @mock_aws
    def test_invalid_entity_id_raises(self) -> None:
        _create_bucket()
        writer = _make_writer()
        with pytest.raises(SageRawLayerWriterError, match="entity_id"):
            writer.write_partition(
                records=_make_records(),
                source_id=_SOURCE_ID,
                entity_id="UPPERCASE_NOT_ALLOWED",
                run_id=_RUN_ID,
                schema_fingerprint=_SCHEMA_FP,
                extraction_date=_DATE,
            )


# ---------------------------------------------------------------------------
# _records_to_parquet helper
# ---------------------------------------------------------------------------


class TestRecordsToParquet:
    def test_empty_records_raises(self) -> None:
        with pytest.raises(SageRawLayerWriterError, match="empty record batch"):
            _records_to_parquet([])

    def test_returns_valid_parquet_bytes(self) -> None:
        records = [ExtractionRecord(payload={"id": "1", "name": "Test"})]
        parquet_bytes = _records_to_parquet(records)
        table = pq.read_table(BytesIO(parquet_bytes))
        assert table.num_rows == 1

    def test_snappy_compression_used(self) -> None:
        records = [ExtractionRecord(payload={"id": "1"})]
        parquet_bytes = _records_to_parquet(records)
        pf = pq.ParquetFile(BytesIO(parquet_bytes))
        col_meta = pf.metadata.row_group(0).column(0)
        assert col_meta.compression == "SNAPPY"

    def test_all_columns_large_utf8_type(self) -> None:
        records = [ExtractionRecord(payload={"id": "1", "amount": "100.50"})]
        parquet_bytes = _records_to_parquet(records)
        table = pq.read_table(BytesIO(parquet_bytes))
        import pyarrow as pa
        for field in table.schema:
            assert field.type == pa.large_utf8(), f"Expected large_utf8 for {field.name}"


# ---------------------------------------------------------------------------
# Streaming write
# ---------------------------------------------------------------------------


class TestWritePartitionStreaming:
    @mock_aws
    def test_streaming_single_chunk(self) -> None:
        _create_bucket()
        writer = _make_writer()
        records_iter = iter(_make_records(5))
        prefix, total = writer.write_partition_streaming(
            record_iter=records_iter,
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
        )
        assert total == 5
        assert f"sage/{_PRODUCT}/{_ENTITY_ID}" in prefix

    @mock_aws
    def test_streaming_multiple_chunks(self) -> None:
        _create_bucket()
        writer = _make_writer()
        # chunk_size=2, 5 records → chunks 0,1,2
        records_iter = iter(_make_records(5))
        prefix, total = writer.write_partition_streaming(
            record_iter=records_iter,
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
            chunk_size=2,
        )
        assert total == 5
        # Verify chunk files written
        s3 = boto3.client("s3", region_name=_REGION)
        objects = s3.list_objects_v2(Bucket=_BUCKET, Prefix=prefix)["Contents"]
        parquet_keys = [o["Key"] for o in objects if o["Key"].endswith(".parquet")]
        assert len(parquet_keys) == 3  # data_0000, data_0001, data_0002

    @mock_aws
    def test_streaming_empty_iterator_raises(self) -> None:
        _create_bucket()
        writer = _make_writer()
        with pytest.raises(SageRawLayerWriterError, match="zero records"):
            writer.write_partition_streaming(
                record_iter=iter([]),
                source_id=_SOURCE_ID,
                entity_id=_ENTITY_ID,
                run_id=_RUN_ID,
                schema_fingerprint=_SCHEMA_FP,
                extraction_date=_DATE,
            )

    @mock_aws
    def test_streaming_invalid_source_id_raises(self) -> None:
        _create_bucket()
        writer = _make_writer()
        with pytest.raises(SageRawLayerWriterError, match="source_id"):
            writer.write_partition_streaming(
                record_iter=iter(_make_records(2)),
                source_id="INVALID_SOURCE",
                entity_id=_ENTITY_ID,
                run_id=_RUN_ID,
                schema_fingerprint=_SCHEMA_FP,
                extraction_date=_DATE,
            )

    @mock_aws
    def test_streaming_metadata_sidecar_written(self) -> None:
        _create_bucket()
        writer = _make_writer()
        prefix, total = writer.write_partition_streaming(
            record_iter=iter(_make_records(4)),
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            run_id=_RUN_ID,
            schema_fingerprint=_SCHEMA_FP,
            extraction_date=_DATE,
            chunk_size=2,
        )
        s3 = boto3.client("s3", region_name=_REGION)
        metadata_key = f"{prefix}/metadata.json"
        body = s3.get_object(Bucket=_BUCKET, Key=metadata_key)["Body"].read()
        metadata = json.loads(body)
        assert metadata["record_count"] == 4
        assert metadata["chunk_count"] == 2
        assert metadata["sage_product"] == _PRODUCT

    @mock_aws
    def test_streaming_chunk_write_failure_raises(self) -> None:
        _create_bucket()
        writer = _make_writer()
        writer._s3.put_object = MagicMock(  # type: ignore[attr-defined]
            side_effect=RuntimeError("S3 unavailable")
        )
        with pytest.raises(SageRawLayerWriterError):
            writer.write_partition_streaming(
                record_iter=iter(_make_records(3)),
                source_id=_SOURCE_ID,
                entity_id=_ENTITY_ID,
                run_id=_RUN_ID,
                schema_fingerprint=_SCHEMA_FP,
                extraction_date=_DATE,
            )
