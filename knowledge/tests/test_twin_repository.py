"""Tests for the twin index repository (FR-1.3 / FR-1.4). DynamoDB is mocked with moto."""

from __future__ import annotations

from typing import Any

import boto3
import pytest
from moto import mock_aws

from knowledge.twin import Twin, TwinEdge
from knowledge.twin_repository import TwinNotFoundError, TwinRepository

_REGION = "us-east-1"


def _create_table(dynamodb: Any) -> Any:
    return dynamodb.create_table(
        TableName="EdlTwinIndex",
        KeySchema=[
            {"AttributeName": "tenant_code", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "tenant_code", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _twin(
    golden_id: str, *, stage: str | None = "ramp", scope_unit_id: str | None = "franchisee-0001"
) -> Twin:
    return Twin(
        entity_type="company",
        golden_id=golden_id,
        attributes={"full_name": "Acme"},
        edges=(TwinEdge("contract_of_company", "contract", "k1", scope_unit_id),),
        lifecycle_stage=stage,
        rollups={"contract_of_company_count": 1},
        scope_unit_id=scope_unit_id,
    )


class TestTwinRepository:
    @mock_aws
    def test_upsert_and_get_roundtrip(self):
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_table(dynamodb)
        repo = TwinRepository(region_name=_REGION)

        repo.upsert_twin("demo", _twin("c1"))
        got = repo.get_twin("demo", "company", "c1")

        assert got.golden_id == "c1"
        assert got.lifecycle_stage == "ramp"
        assert got.rollups == {"contract_of_company_count": 1}
        assert got.edges == (TwinEdge("contract_of_company", "contract", "k1", "franchisee-0001"),)
        assert got.scope_unit_id == "franchisee-0001"

    @mock_aws
    def test_get_missing_raises(self):
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_table(dynamodb)
        repo = TwinRepository(region_name=_REGION)
        with pytest.raises(TwinNotFoundError):
            repo.get_twin("demo", "company", "nope")

    @mock_aws
    def test_lifecycle_none_roundtrips(self):
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_table(dynamodb)
        repo = TwinRepository(region_name=_REGION)
        repo.upsert_twin("demo", _twin("c2", stage=None))
        assert repo.get_twin("demo", "company", "c2").lifecycle_stage is None

    @mock_aws
    def test_list_twins_scoped_to_entity_type(self):
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_table(dynamodb)
        repo = TwinRepository(region_name=_REGION)
        repo.upsert_twin("demo", _twin("c1"))
        repo.upsert_twin("demo", _twin("c2"))
        repo.upsert_twin(
            "demo",
            Twin("person", "p1", {}, (), None, {}, "franchisee-0001"),
        )
        companies = repo.list_twins("demo", "company")
        assert {t.golden_id for t in companies} == {"c1", "c2"}

    @mock_aws
    def test_tenant_isolation_on_get(self):
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_table(dynamodb)
        repo = TwinRepository(region_name=_REGION)
        repo.upsert_twin("acme-corp", _twin("c1"))
        with pytest.raises(TwinNotFoundError):
            repo.get_twin("globex-eu", "company", "c1")

    def test_invalid_tenant_code_rejected(self):
        repo = TwinRepository(region_name=_REGION)
        with pytest.raises(ValueError):
            repo.get_twin("BAD_TENANT", "company", "c1")
