"""
`GET /runs` must Query the tenant GSI, not Scan (F6).

A Scan with a FilterExpression reads and bills every tenant's items to answer one tenant's
request, so the endpoint degrades with total platform volume rather than with the caller's own
data. The index (`tenant-started-index`) already existed; only usage metering used it, and this
route's docstring asserted no such index existed.

The assertion that matters is the negative one: with the index present, `scan` is never called.
Asserting only on the response body would pass either way, which is how the Scan survived a
review.
"""

from __future__ import annotations

import json
from typing import Any, Final

import boto3
import pytest
from moto import mock_aws

import connector_runtime.api.control_plane_handler as cp

_REGION: Final[str] = "us-east-1"
_TABLE: Final[str] = "EdlRunAuditLog"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", _REGION)
    monkeypatch.setenv("PLATFORM_ENVIRONMENT", "dev")
    monkeypatch.setenv("AUDIT_LOG_TABLE", _TABLE)
    # The presence cache is per container; a stale entry would leak between tests.
    cp._INDEX_PRESENCE.clear()


def _event(tenant_code: str = "demo") -> dict[str, Any]:
    return {
        "httpMethod": "GET",
        "path": f"/tenants/{tenant_code}/runs",
        "body": None,
        "requestContext": {
            "authorizer": {"claims": {"custom:tenant_code": tenant_code, "sub": "user-1"}}
        },
    }


def _create_table(dynamodb: Any, *, with_index: bool) -> Any:
    kwargs: dict[str, Any] = {
        "TableName": _TABLE,
        "KeySchema": [
            {"AttributeName": "run_id", "KeyType": "HASH"},
            {"AttributeName": "stage", "KeyType": "RANGE"},
        ],
        "AttributeDefinitions": [
            {"AttributeName": "run_id", "AttributeType": "S"},
            {"AttributeName": "stage", "AttributeType": "S"},
            {"AttributeName": "tenant_code", "AttributeType": "S"},
            {"AttributeName": "started_at", "AttributeType": "S"},
        ],
        "BillingMode": "PAY_PER_REQUEST",
    }
    if with_index:
        kwargs["GlobalSecondaryIndexes"] = [
            {
                "IndexName": "tenant-started-index",
                "KeySchema": [
                    {"AttributeName": "tenant_code", "KeyType": "HASH"},
                    {"AttributeName": "started_at", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ]
    else:
        kwargs["AttributeDefinitions"] = [
            definition
            for definition in kwargs["AttributeDefinitions"]
            if definition["AttributeName"] in {"run_id", "stage"}
        ]
    return dynamodb.create_table(**kwargs)


def _seed(table: Any, tenant_code: str, runs: int) -> None:
    for index in range(runs):
        table.put_item(
            Item={
                "run_id": f"run-{tenant_code}-{index:03d}",
                "stage": "extraction",
                "tenant_code": tenant_code,
                "started_at": f"2026-07-2{index % 9}T00:00:00Z",
                "completed_at": f"2026-07-2{index % 9}T01:00:00Z",
                "status": "SUCCEEDED",
                "source_id": "salesforce",
                "entity_id": "account",
            }
        )


class TestIndexIsUsedWhenPresent:
    @mock_aws
    def test_scan_is_never_called(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        table = _create_table(dynamodb, with_index=True)
        _seed(table, "demo", 3)

        calls: list[str] = []
        real_table = cp._run_audit_log_table()
        original_query = real_table.query

        def _spy_query(**kwargs: Any) -> Any:
            calls.append("query")
            return original_query(**kwargs)

        def _forbidden_scan(**kwargs: Any) -> Any:
            calls.append("scan")
            raise AssertionError("list_runs fell back to Scan while the GSI was present")

        monkeypatch.setattr(cp, "_run_audit_log_table", lambda: real_table)
        monkeypatch.setattr(real_table, "query", _spy_query)
        monkeypatch.setattr(real_table, "scan", _forbidden_scan)

        response = cp.lambda_handler(_event(), None)
        assert response["statusCode"] == 200
        assert calls == ["query"]

    @mock_aws
    def test_returns_only_the_callers_tenant(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        table = _create_table(dynamodb, with_index=True)
        _seed(table, "demo", 2)
        _seed(table, "other", 3)

        body = json.loads(cp.lambda_handler(_event(), None)["body"])
        assert body["count"] == 2
        assert all(run["run_id"].startswith("run-demo-") for run in body["runs"])


class TestFallbackWhenIndexIsAbsent:
    @mock_aws
    def test_scan_is_used_so_code_can_deploy_before_terraform(self) -> None:
        # Deploy ordering: the Lambda ships before the GSI exists. Failing closed here would take
        # the endpoint down rather than merely making it slow.
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        table = _create_table(dynamodb, with_index=False)
        _seed(table, "demo", 2)

        response = cp.lambda_handler(_event(), None)
        assert response["statusCode"] == 200
        assert json.loads(response["body"])["count"] == 2
