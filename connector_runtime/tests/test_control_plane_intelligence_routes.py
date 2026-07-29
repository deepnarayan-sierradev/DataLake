"""
Tests for the intelligence-layer control-plane routes.

Covers twin read (get/list, 404, tenant-mismatch 403), semantic query execution
(success, access-denied 403, unknown metric 400, no-model 404), and saved-query
CRUD + run. DynamoDB mocked with moto; the engine and analytics-partition
locator are mocked so no real DuckDB/S3 read is needed.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

import connector_runtime.api.control_plane_handler as cp
from knowledge.twin import Twin, TwinEdge
from knowledge.twin_repository import TwinRepository
from semantic.semantic_model import Dimension, Metric, SemanticEntity, SemanticModel
from semantic.semantic_model_repository import SemanticModelRepository
from tenancy.scope_contract import IMPLICIT_SCOPE_UNIT_ID

_REGION = "us-east-1"


def _event(method, path, *, tenant_claim="demo", body=None, access_tags=None, no_claims=False):
    event = {
        "httpMethod": method,
        "path": path,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {},
    }
    if not no_claims:
        claims = {"custom:tenant_code": tenant_claim, "sub": "user-1"}
        if access_tags:
            claims["custom:access_tags"] = access_tags
        event["requestContext"]["authorizer"] = {"claims": claims}
    return event


def _ct(dynamodb, name, sort_key):
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


def _create_tables(dynamodb):
    _ct(dynamodb, "EdlTwinIndex", "sk")
    _ct(dynamodb, "EdlSavedQuery", "query_id")
    _ct(dynamodb, "EdlSemanticModel", "model_version")
    # Every read route now builds a scope predicate from the caller's claim, which reads the
    # tenant's partition profile and unit list (DL-SCOPE-14). Absent table -> absent profile ->
    # the single-partition default, which is the demo shape these tests exercise.
    _ct(dynamodb, "EdlScopeUnit", "scope_unit_id")


def _model():
    entity = SemanticEntity(
        name="company",
        entity_type="company",
        dimensions=(
            Dimension(name="industry", column="industry"),
            Dimension(name="country", column="billing_country", access_tag="pii"),
        ),
        metrics=(Metric(name="total_revenue", aggregation="sum", column="annual_revenue"),),
    )
    return SemanticModel(tenant_code="demo", model_version="v1", entities=(entity,))


def _fake_engine(rows):
    engine = MagicMock()
    engine.stream.return_value = [rows]
    return engine


def _patch_query_runtime(monkeypatch, rows):
    monkeypatch.setattr(
        cp,
        "latest_partition_uri",
        lambda *a, **k: "s3://edl-analytics-1/demo/analytics/company/analytics_date=2026-07-22",
    )
    monkeypatch.setattr(cp.set_based_engine_registry, "build", lambda *a, **k: _fake_engine(rows))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AWS_REGION", _REGION)
    monkeypatch.setenv("PLATFORM_ENVIRONMENT", "dev")
    monkeypatch.setenv("ANALYTICS_S3_BUCKET", "edl-analytics-1")
    monkeypatch.setenv("TWIN_INDEX_TABLE", "EdlTwinIndex")
    monkeypatch.setenv("SAVED_QUERY_TABLE", "EdlSavedQuery")
    monkeypatch.setenv("SEMANTIC_MODEL_TABLE", "EdlSemanticModel")


class TestTwinRoutes:
    @mock_aws
    def test_get_twin(self):
        _create_tables(boto3.resource("dynamodb", region_name=_REGION))
        TwinRepository(region_name=_REGION).upsert_twin(
            "demo",
            Twin(
                entity_type="company",
                golden_id="c-1",
                attributes={},
                edges=(TwinEdge("signed_by", "contract", "k-1", IMPLICIT_SCOPE_UNIT_ID),),
                lifecycle_stage="active",
                rollups={"signed_by_count": 1},
                scope_unit_id=IMPLICIT_SCOPE_UNIT_ID,
            ),
        )
        resp = cp.lambda_handler(_event("GET", "/tenants/demo/twins/company/c-1"), None)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["golden_id"] == "c-1"
        assert body["rollups"]["signed_by_count"] == 1
        assert body["edges"][0]["to_golden_id"] == "k-1"

    @mock_aws
    def test_get_twin_not_found(self):
        _create_tables(boto3.resource("dynamodb", region_name=_REGION))
        resp = cp.lambda_handler(_event("GET", "/tenants/demo/twins/company/missing"), None)
        assert resp["statusCode"] == 404

    @mock_aws
    def test_list_twins(self):
        _create_tables(boto3.resource("dynamodb", region_name=_REGION))
        repo = TwinRepository(region_name=_REGION)
        for golden_id in ("c-1", "c-2"):
            repo.upsert_twin(
                "demo",
                Twin(
                    entity_type="company",
                    golden_id=golden_id,
                    attributes={},
                    edges=(),
                    lifecycle_stage=None,
                    rollups={},
                    scope_unit_id=IMPLICIT_SCOPE_UNIT_ID,
                ),
            )
        resp = cp.lambda_handler(_event("GET", "/tenants/demo/twins/company"), None)
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["count"] == 2

    def test_get_twin_tenant_mismatch_forbidden(self):
        resp = cp.lambda_handler(
            _event("GET", "/tenants/demo/twins/company/c-1", tenant_claim="other"), None
        )
        assert resp["statusCode"] == 403


class TestSemanticQueryRoute:
    @mock_aws
    def test_run_semantic_query(self, monkeypatch):
        _create_tables(boto3.resource("dynamodb", region_name=_REGION))
        SemanticModelRepository(region_name=_REGION).publish(_model())
        _patch_query_runtime(monkeypatch, [{"industry": "Tech", "total_revenue": 100}])
        resp = cp.lambda_handler(
            _event(
                "POST",
                "/tenants/demo/semantic/query",
                body={
                    "entity": "company",
                    "metrics": ["total_revenue"],
                    "dimensions": ["industry"],
                },
            ),
            None,
        )
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["row_count"] == 1
        assert body["rows"][0]["total_revenue"] == 100

    @mock_aws
    def test_access_denied_returns_403(self, monkeypatch):
        _create_tables(boto3.resource("dynamodb", region_name=_REGION))
        SemanticModelRepository(region_name=_REGION).publish(_model())
        _patch_query_runtime(monkeypatch, [])
        resp = cp.lambda_handler(
            _event(
                "POST",
                "/tenants/demo/semantic/query",
                body={"entity": "company", "metrics": ["total_revenue"], "dimensions": ["country"]},
            ),
            None,
        )
        assert resp["statusCode"] == 403

    @mock_aws
    def test_unknown_metric_returns_400(self, monkeypatch):
        _create_tables(boto3.resource("dynamodb", region_name=_REGION))
        SemanticModelRepository(region_name=_REGION).publish(_model())
        _patch_query_runtime(monkeypatch, [])
        resp = cp.lambda_handler(
            _event(
                "POST",
                "/tenants/demo/semantic/query",
                body={"entity": "company", "metrics": ["nonexistent_metric"]},
            ),
            None,
        )
        assert resp["statusCode"] == 400

    @mock_aws
    def test_no_active_model_returns_404(self, monkeypatch):
        _create_tables(boto3.resource("dynamodb", region_name=_REGION))
        _patch_query_runtime(monkeypatch, [])
        resp = cp.lambda_handler(
            _event(
                "POST",
                "/tenants/demo/semantic/query",
                body={"entity": "company", "metrics": ["total_revenue"]},
            ),
            None,
        )
        assert resp["statusCode"] == 404


class TestSavedQueryRoutes:
    @mock_aws
    def test_create_list_get(self):
        _create_tables(boto3.resource("dynamodb", region_name=_REGION))
        body = {
            "query_id": "revenue-by-industry",
            "name": "Revenue by industry",
            "entity": "company",
            "metrics": ["total_revenue"],
            "dimensions": ["industry"],
        }
        created = cp.lambda_handler(_event("POST", "/tenants/demo/saved-queries", body=body), None)
        assert created["statusCode"] == 201

        listed = cp.lambda_handler(_event("GET", "/tenants/demo/saved-queries"), None)
        listed_body = json.loads(listed["body"])
        assert listed_body["count"] == 1
        assert listed_body["saved_queries"][0]["created_by"] == "user-1"

        fetched = cp.lambda_handler(
            _event("GET", "/tenants/demo/saved-queries/revenue-by-industry"), None
        )
        assert json.loads(fetched["body"])["query_id"] == "revenue-by-industry"

    @mock_aws
    def test_run_saved_query(self, monkeypatch):
        _create_tables(boto3.resource("dynamodb", region_name=_REGION))
        SemanticModelRepository(region_name=_REGION).publish(_model())
        cp.lambda_handler(
            _event(
                "POST",
                "/tenants/demo/saved-queries",
                body={
                    "query_id": "revenue-by-industry",
                    "name": "Revenue by industry",
                    "entity": "company",
                    "metrics": ["total_revenue"],
                    "dimensions": ["industry"],
                },
            ),
            None,
        )
        _patch_query_runtime(monkeypatch, [{"industry": "Tech", "total_revenue": 100}])
        resp = cp.lambda_handler(
            _event("POST", "/tenants/demo/saved-queries/revenue-by-industry/run"), None
        )
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["row_count"] == 1

    @mock_aws
    def test_get_saved_query_not_found(self):
        _create_tables(boto3.resource("dynamodb", region_name=_REGION))
        resp = cp.lambda_handler(_event("GET", "/tenants/demo/saved-queries/missing-one"), None)
        assert resp["statusCode"] == 404
