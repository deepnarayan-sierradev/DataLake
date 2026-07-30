"""Tests for the semantic model repository (FR-2.1). DynamoDB mocked with moto."""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from moto import mock_aws

from conftest import RESOURCE_NAME_ENVIRONMENT
from semantic.semantic_model import Dimension, Metric, SemanticEntity, SemanticModel
from semantic.semantic_model_repository import (
    SemanticModelNotFoundError,
    SemanticModelRepository,
)

_REGION = "us-east-1"


def _create_table(dynamodb: Any) -> Any:
    return dynamodb.create_table(
        TableName=RESOURCE_NAME_ENVIRONMENT["SEMANTIC_MODEL_TABLE"],
        KeySchema=[
            {"AttributeName": "tenant_code", "KeyType": "HASH"},
            {"AttributeName": "model_version", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "tenant_code", "AttributeType": "S"},
            {"AttributeName": "model_version", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _model(version: str = "v1") -> SemanticModel:
    entity = SemanticEntity(
        name="company",
        entity_type="company",
        dimensions=(Dimension(name="industry", column="industry"),),
        metrics=(Metric(name="total_revenue", aggregation="sum", column="annual_revenue"),),
    )
    return SemanticModel(tenant_code="demo", model_version=version, entities=(entity,))


class TestSemanticModelRepository:
    @mock_aws
    def test_publish_and_load_roundtrip(self):
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_table(dynamodb)
        repo = SemanticModelRepository(region_name=_REGION)
        repo.publish(_model("v1"))
        loaded = repo.load("demo", "v1")
        assert loaded.model_version == "v1"
        assert loaded.entity("company").metric("total_revenue").aggregation == "sum"

    @mock_aws
    def test_load_active_returns_latest_published(self):
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_table(dynamodb)
        repo = SemanticModelRepository(region_name=_REGION)
        repo.publish(_model("v1"))
        repo.publish(_model("v2"))
        assert repo.load_active("demo").model_version == "v2"

    @mock_aws
    def test_load_missing_raises(self):
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_table(dynamodb)
        repo = SemanticModelRepository(region_name=_REGION)
        with pytest.raises(SemanticModelNotFoundError):
            repo.load("demo", "nope")

    @mock_aws
    def test_load_active_without_publish_raises(self):
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_table(dynamodb)
        repo = SemanticModelRepository(region_name=_REGION)
        with pytest.raises(SemanticModelNotFoundError):
            repo.load_active("demo")

    def test_invalid_tenant_rejected(self):
        repo = SemanticModelRepository(region_name=_REGION)
        with pytest.raises(ValueError):
            repo.load("BAD_TENANT", "v1")
