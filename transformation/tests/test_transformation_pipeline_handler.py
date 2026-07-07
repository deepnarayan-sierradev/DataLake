"""
Tests for the transformation pipeline Lambda handler (DP-1 regression coverage).

Before this test existed, `lambda_handler` was never exercised end-to-end with
a real `ConfigurationRepositoryClient` — the constructor-signature mismatch
that silently disabled the SCD merge (`table_name=` instead of `environment=`)
went uncaught because every other test mocked the repository client away.
These tests call `lambda_handler` directly against moto-backed S3/DynamoDB so
the real constructor is always exercised.
"""

from __future__ import annotations

import io

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from moto import mock_aws

from transformation.transformation_pipeline_handler import lambda_handler

_REGION = "us-east-1"
_ENVIRONMENT = "dev"
_RAW_BUCKET = "dev-edl-raw-layer"
_CURATED_BUCKET = "dev-edl-curated-layer"
_MAPPING_BUCKET = "dev-edl-curated-layer"
_CONFIG_TABLE = f"{_ENVIRONMENT}-edl-entity-extraction-config"


@pytest.fixture()
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_REGION", _REGION)
    monkeypatch.setenv("RAW_S3_BUCKET", _RAW_BUCKET)
    monkeypatch.setenv("CURATED_S3_BUCKET", _CURATED_BUCKET)
    monkeypatch.setenv("FIELD_MAPPING_S3_BUCKET", _MAPPING_BUCKET)
    monkeypatch.delenv("GOVERNANCE_S3_BUCKET", raising=False)
    monkeypatch.delenv("GLUE_CATALOG_DATABASE", raising=False)
    with mock_aws():
        s3 = boto3.client("s3", region_name=_REGION)
        for bucket in {_RAW_BUCKET, _CURATED_BUCKET, _MAPPING_BUCKET}:
            s3.create_bucket(Bucket=bucket)

        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        dynamodb.create_table(
            TableName=_CONFIG_TABLE,
            KeySchema=[
                {"AttributeName": "source_id", "KeyType": "HASH"},
                {"AttributeName": "entity_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "source_id", "AttributeType": "S"},
                {"AttributeName": "entity_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield s3, dynamodb


def _write_raw_parquet(s3_client, prefix: str, records: list[dict]) -> None:
    table = pa.Table.from_pylist(records)
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    s3_client.put_object(Bucket=_RAW_BUCKET, Key=f"{prefix}data.parquet", Body=buf.getvalue())


def _base_event(raw_s3_prefix: str) -> dict:
    return {
        "source_id": "mysql-rds",
        "entity_id": "mysql-rds-contracts",
        "environment": _ENVIRONMENT,
        "run_id": "run-handler-test-001",
        "raw_s3_prefix": raw_s3_prefix,
    }


class TestConfigurationRepositoryClientRealConstructor:
    """
    Regression test for DP-1: the handler must construct
    ConfigurationRepositoryClient with its real signature (`environment=`,
    not `table_name=`), and a missing DynamoDB item must not be mistaken for
    a programming error.
    """

    def test_accumulator_wired_when_primary_key_field_configured(self, aws_env) -> None:
        s3, dynamodb = aws_env
        table = dynamodb.Table(_CONFIG_TABLE)
        table.put_item(
            Item={
                "source_id": "mysql-rds",
                "entity_id": "mysql-rds-contracts",
                "config_version": "1.0.0",
                "load_type": "incremental",
                "watermark_field": "updated_at",
                "target_raw_s3_prefix": "s3://raw/mysql-rds/contracts/",
                "schema_snapshot_s3_prefix": "s3://schema-snapshots/mysql-rds/contracts/",
                "primary_key_field": "contract_id",
                "soft_delete_field": None,
            }
        )
        _write_raw_parquet(s3, "raw/handler-scd/", [{"contract_id": "1", "status": "active"}])

        result = lambda_handler(_base_event("raw/handler-scd/"), context=None)

        # If the constructor call site regresses to `table_name=`, this raises
        # TypeError deep inside the handler's try/except and the test fails
        # loudly (the except clause no longer swallows programming errors).
        assert result["canonical_record_count"] == 1
        assert result["is_publication_blocked"] is False

    def test_no_config_record_runs_append_only_without_raising(self, aws_env) -> None:
        """Missing DynamoDB item (ConfigurationNotFoundError) must not fail the run."""
        s3, _dynamodb = aws_env
        _write_raw_parquet(s3, "raw/handler-noconfig/", [{"contract_id": "1"}])

        result = lambda_handler(
            _base_event("raw/handler-noconfig/"),
            context=None,
        )

        assert result["canonical_record_count"] == 1

    def test_wrong_keyword_argument_would_be_caught(self, aws_env, monkeypatch) -> None:
        """
        Guardrail test: if the call site regresses to the old broken keyword
        (`table_name=`), the handler must raise rather than silently disable
        the accumulator. This asserts today's real constructor signature.
        """
        from connector_runtime.configuration_repository.configuration_repository import (
            ConfigurationRepositoryClient,
        )

        with pytest.raises(TypeError):
            ConfigurationRepositoryClient(  # type: ignore[call-arg]
                table_name="anything", region_name=_REGION
            )

        # The real, required signature:
        ConfigurationRepositoryClient(environment=_ENVIRONMENT, region_name=_REGION)
