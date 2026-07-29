"""
Twin routes under a *partitioned* tenant (DL-SCOPE-13).

The defect these exist for: the routes filtered on `twin.scope_unit_id` while the model carried
no such field, so `getattr(..., None)` made the filter read every twin as unattributed. It went
unnoticed because the twin tests used `demo`, a single-partition tenant whose claim contains
`__tenant__` — there, `matches(None)` is `True` and no filter can fail.

Every assertion here is written against two sibling franchisees, so a filter that stops working
turns one of them red.
"""

from __future__ import annotations

import json
from typing import Any, Final

import boto3
import pytest
from moto import mock_aws

import connector_runtime.api.control_plane_handler as cp
from knowledge.twin import Twin, TwinEdge
from knowledge.twin_repository import TwinRepository
from tenancy.scope_contract import (
    PartitionKind,
    PartitionModel,
    ScopeUnit,
    TenantPartitionProfile,
)
from tenancy.scope_unit_repository import ScopeUnitRepository

_REGION: Final[str] = "us-east-1"
_TENANT: Final[str] = "evive"
_UNIT_A: Final[str] = "franchisee-0001"
_UNIT_B: Final[str] = "franchisee-0002"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", _REGION)
    monkeypatch.setenv("PLATFORM_ENVIRONMENT", "dev")
    monkeypatch.setenv("ANALYTICS_S3_BUCKET", "edl-analytics-1")
    monkeypatch.setenv("TWIN_INDEX_TABLE", "EdlTwinIndex")
    monkeypatch.setenv("SCOPE_UNIT_TABLE", "EdlScopeUnit")


def _event(path: str, *, units: str | None = None, tenant_wide: bool = False) -> dict[str, Any]:
    claims: dict[str, str] = {"custom:tenant_code": _TENANT, "sub": "user-1"}
    if units is not None:
        claims["custom:scope_units"] = units
    if tenant_wide:
        claims["custom:scope_tenant_wide"] = "true"
    return {
        "httpMethod": "GET",
        "path": path,
        "body": None,
        "requestContext": {"authorizer": {"claims": claims}},
    }


def _create_tables(dynamodb: Any) -> None:
    for name, sort_key in (("EdlTwinIndex", "sk"), ("EdlScopeUnit", "scope_unit_id")):
        dynamodb.create_table(
            TableName=name,
            KeySchema=[
                {"AttributeName": "tenant_code", "KeyType": "HASH"},
                {"AttributeName": sort_key, "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "tenant_code", "AttributeType": "S"},
                {"AttributeName": sort_key, "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )


def _seed_partitioned_tenant() -> None:
    repository = ScopeUnitRepository(environment="dev", region_name=_REGION)
    repository.save_partition_profile(
        TenantPartitionProfile(
            tenant_code=_TENANT,
            partition_model=PartitionModel.PARTITIONED,
            partition_kind=PartitionKind.FRANCHISE,
        )
    )
    for unit_id in (_UNIT_A, _UNIT_B):
        repository.save_scope_unit(
            ScopeUnit(
                tenant_code=_TENANT,
                scope_unit_id=unit_id,
                partition_kind=PartitionKind.FRANCHISE,
                display_name=unit_id,
            )
        )


def _seed_twins() -> None:
    repository = TwinRepository(region_name=_REGION)
    for golden_id, unit in (("c-a", _UNIT_A), ("c-b", _UNIT_B)):
        repository.upsert_twin(
            _TENANT,
            Twin(
                entity_type="company",
                golden_id=golden_id,
                attributes={},
                edges=(),
                lifecycle_stage="active",
                rollups={},
                scope_unit_id=unit,
            ),
        )


@mock_aws
def _bootstrap() -> None:
    _create_tables(boto3.resource("dynamodb", region_name=_REGION))
    _seed_partitioned_tenant()
    _seed_twins()


class TestGetTwinAcrossScopeUnits:
    @mock_aws
    def test_caller_sees_its_own_units_twin(self) -> None:
        _create_tables(boto3.resource("dynamodb", region_name=_REGION))
        _seed_partitioned_tenant()
        _seed_twins()
        resp = cp.lambda_handler(
            _event(f"/tenants/{_TENANT}/twins/company/c-a", units=_UNIT_A), None
        )
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["golden_id"] == "c-a"

    @mock_aws
    def test_foreign_units_twin_is_404_not_403(self) -> None:
        # 404, because 403 confirms the twin exists — that is the disclosure DL-SCOPE-13 forbids.
        _create_tables(boto3.resource("dynamodb", region_name=_REGION))
        _seed_partitioned_tenant()
        _seed_twins()
        resp = cp.lambda_handler(
            _event(f"/tenants/{_TENANT}/twins/company/c-b", units=_UNIT_A), None
        )
        assert resp["statusCode"] == 404

    @mock_aws
    def test_sibling_sees_the_mirror_image(self) -> None:
        _create_tables(boto3.resource("dynamodb", region_name=_REGION))
        _seed_partitioned_tenant()
        _seed_twins()
        assert (
            cp.lambda_handler(_event(f"/tenants/{_TENANT}/twins/company/c-b", units=_UNIT_B), None)[
                "statusCode"
            ]
            == 200
        )
        assert (
            cp.lambda_handler(_event(f"/tenants/{_TENANT}/twins/company/c-a", units=_UNIT_B), None)[
                "statusCode"
            ]
            == 404
        )


class TestListTwinsAcrossScopeUnits:
    @mock_aws
    def test_listing_shows_only_the_callers_unit(self) -> None:
        _create_tables(boto3.resource("dynamodb", region_name=_REGION))
        _seed_partitioned_tenant()
        _seed_twins()
        resp = cp.lambda_handler(_event(f"/tenants/{_TENANT}/twins/company", units=_UNIT_A), None)
        body = json.loads(resp["body"])
        assert resp["statusCode"] == 200
        assert body["count"] == 1
        assert [twin["golden_id"] for twin in body["twins"]] == ["c-a"]

    @mock_aws
    def test_tenant_wide_grant_sees_both(self) -> None:
        _create_tables(boto3.resource("dynamodb", region_name=_REGION))
        _seed_partitioned_tenant()
        _seed_twins()
        resp = cp.lambda_handler(
            _event(f"/tenants/{_TENANT}/twins/company", tenant_wide=True), None
        )
        assert json.loads(resp["body"])["count"] == 2

    @mock_aws
    def test_empty_scope_grant_is_denied_not_unfiltered(self) -> None:
        # The single most likely implementation defect in DL-12: reading "no units" as "no filter".
        _create_tables(boto3.resource("dynamodb", region_name=_REGION))
        _seed_partitioned_tenant()
        _seed_twins()
        resp = cp.lambda_handler(_event(f"/tenants/{_TENANT}/twins/company", units=""), None)
        assert resp["statusCode"] == 403


class TestEdgeFanOutIsFiltered:
    @mock_aws
    def test_edge_to_a_foreign_unit_is_hidden(self) -> None:
        # A node the caller may see can still point at another unit's entity; listing that edge
        # discloses the target exists, which is the half of DL-SCOPE-13 left open until now.
        _create_tables(boto3.resource("dynamodb", region_name=_REGION))
        _seed_partitioned_tenant()
        TwinRepository(region_name=_REGION).upsert_twin(
            _TENANT,
            Twin(
                entity_type="company",
                golden_id="c-shared",
                attributes={},
                edges=(
                    TwinEdge("supplied_by", "vendor", "v-own", _UNIT_A),
                    TwinEdge("supplied_by", "vendor", "v-foreign", _UNIT_B),
                ),
                lifecycle_stage="active",
                rollups={"supplied_by_count": 2},
                scope_unit_id=_UNIT_A,
            ),
        )
        resp = cp.lambda_handler(
            _event(f"/tenants/{_TENANT}/twins/company/c-shared", units=_UNIT_A), None
        )
        body = json.loads(resp["body"])
        assert resp["statusCode"] == 200
        assert [edge["to_golden_id"] for edge in body["edges"]] == ["v-own"]
        # The suppressed count is deliberately absent from the response: it lets a franchisee
        # enumerate how many peer relationships exist, which is a weaker form of the disclosure this
        # filter prevents. It is logged and metered instead.
        assert "edges_hidden_by_scope" not in body
