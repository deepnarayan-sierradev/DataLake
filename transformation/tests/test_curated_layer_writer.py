"""Tests for CuratedLayerWriter — Phase 6."""

from __future__ import annotations

import io
from datetime import date

import boto3
import pyarrow.parquet as pq
import pytest
from moto import mock_aws

from transformation.curated_layer_writer import CuratedLayerWriter, CuratedWriteError

_REGION = "us-east-1"
_BUCKET = "test-curated-bucket"
_RUN_ID = "run-test-curated-001"


@mock_aws
class TestCuratedLayerWriter:
    def setup_method(self, method=None):
        boto3.client("s3", region_name=_REGION).create_bucket(Bucket=_BUCKET)
        self.writer = CuratedLayerWriter(_BUCKET, _REGION)

    def _sample_records(self):
        return [
            {"account_id": "001", "name": "Acme Corp", "annual_revenue": 1_000_000.0},
            {"account_id": "002", "name": "Beta Ltd", "annual_revenue": 500_000.0},
        ]

    def test_write_returns_correct_metadata(self):
        result = self.writer.write(
            records=self._sample_records(),
            domain="customer",
            entity_id="salesforce-account",
            run_id=_RUN_ID,
            curated_date=date(2024, 1, 15),
        )
        assert result.record_count == 2
        assert "curated_date=2024-01-15" in result.s3_prefix
        assert f"run_id={_RUN_ID}" in result.s3_prefix
        assert result.s3_key.endswith(".parquet")

    def test_written_file_is_readable_parquet(self):
        result = self.writer.write(
            records=self._sample_records(),
            domain="customer",
            entity_id="salesforce-account",
            run_id=_RUN_ID,
            curated_date=date(2024, 1, 15),
        )
        s3 = boto3.client("s3", region_name=_REGION)
        obj = s3.get_object(Bucket=_BUCKET, Key=result.s3_key)
        buf = io.BytesIO(obj["Body"].read())
        table = pq.read_table(buf)
        assert table.num_rows == 2
        assert "name" in table.schema.names

    def test_empty_records_raises(self):
        with pytest.raises(CuratedWriteError):
            self.writer.write([], "customer", "salesforce-account", _RUN_ID)

    def test_partition_scheme_includes_domain_and_entity(self):
        result = self.writer.write(
            records=self._sample_records(),
            domain="finance",
            entity_id="mysql-rds-orders",
            run_id=_RUN_ID,
        )
        assert "curated/finance/mysql-rds-orders/" in result.s3_prefix

    def test_two_runs_produce_separate_keys(self):
        r1 = self.writer.write(self._sample_records(), "customer", "salesforce-account", "run-001")
        r2 = self.writer.write(self._sample_records(), "customer", "salesforce-account", "run-002")
        assert r1.s3_key != r2.s3_key

    def test_default_date_uses_today(self):
        result = self.writer.write(
            self._sample_records(), "customer", "salesforce-account", _RUN_ID
        )
        from datetime import UTC, datetime

        today = datetime.now(UTC).date().isoformat()
        assert today in result.s3_prefix
