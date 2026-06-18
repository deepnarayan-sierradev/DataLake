"""Tests for AnalyticsLayerPublisher — Phase 8."""

from __future__ import annotations

import io

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from moto import mock_aws

from transformation.analytics_layer_publisher import (
    AnalyticsLayerPublisher,
    AnalyticsPublicationError,
)

_REGION = "us-east-1"
_SOURCE_BUCKET = "test-curated-bucket"
_ANALYTICS_BUCKET = "test-analytics-bucket"
_GLUE_DB = "edl_analytics"
_RUN_ID = "run-analytics-001"


def _write_parquet(s3_client, bucket, key, records):
    table = pa.Table.from_pylist(records)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    s3_client.put_object(Bucket=bucket, Key=key, Body=buf.getvalue())


@mock_aws
class TestAnalyticsLayerPublisher:
    def setup_method(self, method=None):
        s3 = boto3.client("s3", region_name=_REGION)
        s3.create_bucket(Bucket=_SOURCE_BUCKET)
        s3.create_bucket(Bucket=_ANALYTICS_BUCKET)
        self.s3 = s3

    def _publisher(self):
        return AnalyticsLayerPublisher(
            source_s3_bucket=_SOURCE_BUCKET,
            analytics_s3_bucket=_ANALYTICS_BUCKET,
            glue_database=_GLUE_DB,
            region_name=_REGION,
        )

    def test_publish_writes_parquet_to_analytics_bucket(self):
        records = [{"account_id": "001", "name": "Acme", "revenue": 1_000_000}]
        _write_parquet(
            self.s3,
            _SOURCE_BUCKET,
            "curated/customer/salesforce-account/run-001/data.parquet",
            records,
        )

        result = self._publisher().publish(
            source_s3_prefix="curated/customer/salesforce-account/run-001/",
            domain="customer",
            entity_id="salesforce-account",
            run_id=_RUN_ID,
        )
        assert result.record_count == 1
        assert "analytics/customer/salesforce-account/" in result.analytics_s3_prefix

    def test_published_file_is_readable(self):
        records = [{"id": "1", "name": "Acme", "value": 42}]
        _write_parquet(self.s3, _SOURCE_BUCKET, "curated/prefix/data.parquet", records)

        result = self._publisher().publish(
            source_s3_prefix="curated/prefix/",
            domain="customer",
            entity_id="salesforce-account",
            run_id=_RUN_ID,
        )
        obj = self.s3.get_object(Bucket=_ANALYTICS_BUCKET, Key=result.analytics_s3_key)
        table = pq.read_table(io.BytesIO(obj["Body"].read()))
        assert table.num_rows == 1

    def test_glue_table_registered(self):
        records = [{"id": "1", "name": "Acme"}]
        _write_parquet(self.s3, _SOURCE_BUCKET, "src/data.parquet", records)

        result = self._publisher().publish(
            source_s3_prefix="src/",
            domain="customer",
            entity_id="salesforce-account",
            run_id=_RUN_ID,
        )
        glue = boto3.client("glue", region_name=_REGION)
        table = glue.get_table(DatabaseName=_GLUE_DB, Name=result.glue_table)
        assert table["Table"]["Name"] == result.glue_table

    def test_glue_table_idempotent_on_second_publish(self):
        records = [{"id": "1", "name": "Acme"}]
        _write_parquet(self.s3, _SOURCE_BUCKET, "src2/data.parquet", records)

        publisher = self._publisher()
        publisher.publish("src2/", "customer", "salesforce-account", "run-1")
        _write_parquet(self.s3, _SOURCE_BUCKET, "src3/data.parquet", records)
        # Second publish should update, not fail
        publisher.publish("src3/", "customer", "salesforce-account", "run-2")

    def test_empty_source_prefix_raises(self):
        with pytest.raises(AnalyticsPublicationError):
            self._publisher().publish(
                "nonexistent/prefix/", "customer", "salesforce-account", _RUN_ID
            )
