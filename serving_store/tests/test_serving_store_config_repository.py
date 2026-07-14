"""Tests for the Serving Store Configuration Repository Client."""

from __future__ import annotations

import json
from typing import Any

import boto3
import pytest
from moto import mock_aws

from contracts.serving_store_config_contract import ServingStoreLoadConfig
from serving_store.serving_store_config_repository import (
    ConfigurationBackend,
    ServingStoreConfigAlreadyExistsError,
    ServingStoreConfigNotFoundError,
    ServingStoreConfigRepositoryClient,
    ServingStoreConfigValidationError,
)

_REGION = "us-east-1"
_ENV = "dev"
_TABLE = "EdlServingStoreConfig"
_BUCKET = "edl-serving-store-config-s3"

_VALID_RECORD: dict[str, Any] = {
    "tenant_code": "acme-corp",
    "entity_type": "company",
    "target_engine": "mysql_rds",
    "table_name": "salesforce_account",
    "primary_keys": ["account_id"],
    "secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:test",
    "region_name": "us-east-1",
    "enabled": True,
}


def _create_dynamodb_table(dynamodb: Any) -> Any:
    return dynamodb.create_table(
        TableName=_TABLE,
        KeySchema=[
            {"AttributeName": "tenant_code", "KeyType": "HASH"},
            {"AttributeName": "entity_type", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "tenant_code", "AttributeType": "S"},
            {"AttributeName": "entity_type", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


class TestServingStoreConfigRepositoryDynamoDB:
    @mock_aws
    def test_load_config_success(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        table = _create_dynamodb_table(dynamodb)
        table.put_item(Item=_VALID_RECORD)

        client = ServingStoreConfigRepositoryClient(environment=_ENV, region_name=_REGION)
        config = client.load_config("acme-corp", "company")

        assert isinstance(config, ServingStoreLoadConfig)
        assert config.tenant_code == "acme-corp"
        assert config.primary_keys == ("account_id",)

    @mock_aws
    def test_load_config_not_found_raises(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_dynamodb_table(dynamodb)

        client = ServingStoreConfigRepositoryClient(environment=_ENV, region_name=_REGION)
        with pytest.raises(ServingStoreConfigNotFoundError):
            client.load_config("acme-corp", "company")

    @mock_aws
    def test_load_config_wrong_tenant_not_found(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        table = _create_dynamodb_table(dynamodb)
        table.put_item(Item=_VALID_RECORD)

        client = ServingStoreConfigRepositoryClient(environment=_ENV, region_name=_REGION)
        with pytest.raises(ServingStoreConfigNotFoundError):
            client.load_config("globex-eu", "company")

    @mock_aws
    def test_load_config_validation_failure_raises(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        table = _create_dynamodb_table(dynamodb)
        table.put_item(Item={**_VALID_RECORD, "primary_keys": []})

        client = ServingStoreConfigRepositoryClient(environment=_ENV, region_name=_REGION)
        with pytest.raises(ServingStoreConfigValidationError):
            client.load_config("acme-corp", "company")

    @mock_aws
    def test_load_config_underscore_entity_type_round_trips(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        table = _create_dynamodb_table(dynamodb)
        table.put_item(Item={**_VALID_RECORD, "entity_type": "ap_bill"})

        client = ServingStoreConfigRepositoryClient(environment=_ENV, region_name=_REGION)
        config = client.load_config("acme-corp", "ap_bill")
        assert config.entity_type == "ap_bill"

    @mock_aws
    def test_save_config_creates_new_record(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_dynamodb_table(dynamodb)

        client = ServingStoreConfigRepositoryClient(environment=_ENV, region_name=_REGION)
        config = ServingStoreLoadConfig(**{**_VALID_RECORD, "primary_keys": ("account_id",)})
        client.save_config(config)

        loaded = client.load_config("acme-corp", "company")
        assert loaded.table_name == "salesforce_account"

    @mock_aws
    def test_save_config_without_overwrite_rejects_duplicate(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_dynamodb_table(dynamodb)

        client = ServingStoreConfigRepositoryClient(environment=_ENV, region_name=_REGION)
        config = ServingStoreLoadConfig(**{**_VALID_RECORD, "primary_keys": ("account_id",)})
        client.save_config(config)

        with pytest.raises(ServingStoreConfigAlreadyExistsError):
            client.save_config(config)

    @mock_aws
    def test_list_configs_for_tenant_returns_only_that_tenant(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        table = _create_dynamodb_table(dynamodb)
        table.put_item(Item=_VALID_RECORD)
        table.put_item(
            Item={**_VALID_RECORD, "tenant_code": "globex-eu", "entity_type": "person"}
        )

        client = ServingStoreConfigRepositoryClient(environment=_ENV, region_name=_REGION)
        configs = client.list_configs_for_tenant("acme-corp")

        assert len(configs) == 1
        assert configs[0].entity_type == "company"


class TestServingStoreConfigRepositoryS3:
    @mock_aws
    def test_load_config_success(self) -> None:
        s3 = boto3.client("s3", region_name=_REGION)
        s3.create_bucket(Bucket=_BUCKET)
        s3.put_object(
            Bucket=_BUCKET,
            Key="acme-corp/serving-store/company/config.json",
            Body=json.dumps(_VALID_RECORD).encode("utf-8"),
        )

        client = ServingStoreConfigRepositoryClient(
            environment=_ENV,
            region_name=_REGION,
            backend=ConfigurationBackend.S3,
            s3_bucket=_BUCKET,
        )
        config = client.load_config("acme-corp", "company")
        assert config.entity_type == "company"

    @mock_aws
    def test_load_config_not_found_raises(self) -> None:
        s3 = boto3.client("s3", region_name=_REGION)
        s3.create_bucket(Bucket=_BUCKET)

        client = ServingStoreConfigRepositoryClient(
            environment=_ENV,
            region_name=_REGION,
            backend=ConfigurationBackend.S3,
            s3_bucket=_BUCKET,
        )
        with pytest.raises(ServingStoreConfigNotFoundError):
            client.load_config("acme-corp", "company")

    def test_s3_backend_requires_bucket(self) -> None:
        with pytest.raises(ValueError, match="s3_bucket"):
            ServingStoreConfigRepositoryClient(
                environment=_ENV, region_name=_REGION, backend=ConfigurationBackend.S3
            )
