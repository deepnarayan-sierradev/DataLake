"""
Tests for the Configuration Repository Client (2.1).

Covers:
  - DynamoDB backend: successful load, item not found, validation failure
  - S3 backend: successful load, key not found, validation failure
  - Construction guard: S3 backend requires s3_bucket
"""

from __future__ import annotations

import json
from typing import Any

import boto3
import pytest
from moto import mock_aws

from connector_runtime.configuration_repository.configuration_repository import (
    ConfigurationBackend,
    ConfigurationNotFoundError,
    ConfigurationRepositoryClient,
    ConfigurationValidationError,
)
from contracts.entity_configuration_contract import EntityExtractionConfig, LoadType

_REGION = "us-east-1"
_ENV = "dev"
_TABLE = f"{_ENV}-entity-extraction-config"
_BUCKET = f"{_ENV}-entity-extraction-config-s3"

_VALID_RECORD: dict[str, Any] = {
    "source_id": "salesforce",
    "entity_id": "salesforce-account",
    "config_version": "1.0.0",
    "load_type": "incremental",
    "watermark_field": "SystemModstamp",
    "target_raw_s3_prefix": "s3://raw/salesforce/account/",
    "schema_snapshot_s3_prefix": "s3://schema-snapshots/salesforce/account/",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_dynamodb_table(dynamodb: Any) -> Any:
    return dynamodb.create_table(
        TableName=_TABLE,
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


# ---------------------------------------------------------------------------
# DynamoDB backend tests
# ---------------------------------------------------------------------------


class TestConfigurationRepositoryDynamoDB:
    @mock_aws
    def test_load_config_success(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        table = _create_dynamodb_table(dynamodb)
        table.put_item(Item=_VALID_RECORD)

        client = ConfigurationRepositoryClient(
            environment=_ENV, region_name=_REGION, backend=ConfigurationBackend.DYNAMODB
        )
        config = client.load_config("salesforce", "salesforce-account")

        assert isinstance(config, EntityExtractionConfig)
        assert config.source_id == "salesforce"
        assert config.entity_id == "salesforce-account"
        assert config.load_type == LoadType.INCREMENTAL

    @mock_aws
    def test_load_config_item_not_found_raises(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_dynamodb_table(dynamodb)

        client = ConfigurationRepositoryClient(
            environment=_ENV, region_name=_REGION, backend=ConfigurationBackend.DYNAMODB
        )
        with pytest.raises(ConfigurationNotFoundError, match="salesforce"):
            client.load_config("salesforce", "salesforce-account")

    @mock_aws
    def test_load_config_invalid_record_raises_validation_error(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        table = _create_dynamodb_table(dynamodb)
        # INCREMENTAL without watermark_field — fails EntityExtractionConfig validation
        invalid = {
            "source_id": "salesforce",
            "entity_id": "salesforce-account",
            "config_version": "1.0.0",
            "load_type": "incremental",
            "target_raw_s3_prefix": "s3://raw/salesforce/account/",
            "schema_snapshot_s3_prefix": "s3://schema-snapshots/salesforce/account/",
        }
        table.put_item(Item=invalid)

        client = ConfigurationRepositoryClient(
            environment=_ENV, region_name=_REGION, backend=ConfigurationBackend.DYNAMODB
        )
        with pytest.raises(ConfigurationValidationError, match="salesforce"):
            client.load_config("salesforce", "salesforce-account")


# ---------------------------------------------------------------------------
# S3 backend tests
# ---------------------------------------------------------------------------


class TestConfigurationRepositoryS3:
    @mock_aws
    def test_load_config_success(self) -> None:
        s3 = boto3.client("s3", region_name=_REGION)
        s3.create_bucket(Bucket=_BUCKET)
        s3.put_object(
            Bucket=_BUCKET,
            Key="salesforce/salesforce-account/config.json",
            Body=json.dumps(_VALID_RECORD).encode("utf-8"),
            ContentType="application/json",
        )

        client = ConfigurationRepositoryClient(
            environment=_ENV,
            region_name=_REGION,
            backend=ConfigurationBackend.S3,
            s3_bucket=_BUCKET,
        )
        config = client.load_config("salesforce", "salesforce-account")

        assert config.source_id == "salesforce"
        assert config.load_type == LoadType.INCREMENTAL

    @mock_aws
    def test_load_config_key_not_found_raises(self) -> None:
        s3 = boto3.client("s3", region_name=_REGION)
        s3.create_bucket(Bucket=_BUCKET)

        client = ConfigurationRepositoryClient(
            environment=_ENV,
            region_name=_REGION,
            backend=ConfigurationBackend.S3,
            s3_bucket=_BUCKET,
        )
        with pytest.raises(ConfigurationNotFoundError):
            client.load_config("salesforce", "salesforce-account")

    @mock_aws
    def test_load_config_invalid_json_record_raises(self) -> None:
        s3 = boto3.client("s3", region_name=_REGION)
        s3.create_bucket(Bucket=_BUCKET)
        # Missing required fields
        s3.put_object(
            Bucket=_BUCKET,
            Key="salesforce/salesforce-account/config.json",
            Body=json.dumps({"source_id": "salesforce"}).encode("utf-8"),
            ContentType="application/json",
        )

        client = ConfigurationRepositoryClient(
            environment=_ENV,
            region_name=_REGION,
            backend=ConfigurationBackend.S3,
            s3_bucket=_BUCKET,
        )
        with pytest.raises(ConfigurationValidationError):
            client.load_config("salesforce", "salesforce-account")

    def test_s3_backend_without_bucket_raises(self) -> None:
        with pytest.raises(ValueError, match="s3_bucket"):
            ConfigurationRepositoryClient(
                environment=_ENV,
                region_name=_REGION,
                backend=ConfigurationBackend.S3,
            )


# ---------------------------------------------------------------------------
# Regression tests for fixed bugs
# ---------------------------------------------------------------------------


class TestInputValidation:
    """
    Regression tests for Bug #5: source_id / entity_id not validated before S3
    key construction (OWASP A03 defense-in-depth).

    load_config() must reject identifiers that do not conform to the stable ID
    format before any AWS API call is attempted.
    """

    def test_uppercase_source_id_raises_value_error(self) -> None:
        client = ConfigurationRepositoryClient(
            environment=_ENV, region_name=_REGION, backend=ConfigurationBackend.DYNAMODB
        )
        with pytest.raises(ValueError, match="stable identifier"):
            client.load_config("Salesforce", "salesforce-account")

    def test_underscore_source_id_raises_value_error(self) -> None:
        client = ConfigurationRepositoryClient(
            environment=_ENV, region_name=_REGION, backend=ConfigurationBackend.DYNAMODB
        )
        with pytest.raises(ValueError, match="stable identifier"):
            client.load_config("salesforce_crm", "salesforce-account")

    def test_path_traversal_source_id_raises_value_error(self) -> None:
        client = ConfigurationRepositoryClient(
            environment=_ENV, region_name=_REGION, backend=ConfigurationBackend.DYNAMODB
        )
        with pytest.raises(ValueError, match="stable identifier"):
            client.load_config("../other", "salesforce-account")

    def test_invalid_entity_id_raises_value_error(self) -> None:
        client = ConfigurationRepositoryClient(
            environment=_ENV, region_name=_REGION, backend=ConfigurationBackend.DYNAMODB
        )
        with pytest.raises(ValueError, match="stable identifier"):
            client.load_config("salesforce", "SALESFORCE-ACCOUNT")

    @mock_aws
    def test_validation_also_applied_to_s3_backend(self) -> None:
        s3 = boto3.client("s3", region_name=_REGION)
        s3.create_bucket(Bucket=_BUCKET)
        client = ConfigurationRepositoryClient(
            environment=_ENV,
            region_name=_REGION,
            backend=ConfigurationBackend.S3,
            s3_bucket=_BUCKET,
        )
        with pytest.raises(ValueError, match="stable identifier"):
            client.load_config("../inject", "salesforce-account")
