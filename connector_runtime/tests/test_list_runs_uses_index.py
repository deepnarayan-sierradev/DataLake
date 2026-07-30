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
from conftest import RESOURCE_NAME_ENVIRONMENT

_REGION: Final[str] = "us-east-1"
_TABLE: Final[str] = RESOURCE_NAME_ENVIRONMENT["AUDIT_LOG_TABLE"]


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", _REGION)
    monkeypatch.setenv("PLATFORM_ENVIRONMENT", "dev")
    monkeypatch.setenv("AUDIT_LOG_TABLE", _TABLE)
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
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        table = _create_table(dynamodb, with_index=False)
        _seed(table, "demo", 2)

        response = cp.lambda_handler(_event(), None)
        assert response["statusCode"] == 200
        assert json.loads(response["body"])["count"] == 2


def _seed_multi_stage(table: Any, tenant_code: str, runs: int, stages: int) -> None:
    """Seed `runs` runs, each writing one audit item per stage — the real shape of the table."""
    for run_index in range(runs):
        for stage_index in range(stages):
            table.put_item(
                Item={
                    "run_id": f"run-{tenant_code}-{run_index:03d}",
                    "stage": f"stage-{stage_index:02d}",
                    "tenant_code": tenant_code,
                    "started_at": f"2026-07-29T{run_index:02d}:00:00Z",
                    "completed_at": f"2026-07-29T{run_index:02d}:{stage_index:02d}:00Z",
                    "status": "SUCCEEDED",
                    "source_id": "salesforce",
                    "entity_id": "account",
                }
            )


class TestRunsArePagedByRunNotByAuditRow:
    """
    A run writes one audit item per stage, and the cap counted *items*. With 11 stages in the
    extraction workflow alone, a cap of 50 returned roughly four runs — reported as `count`, with no
    cursor to reach the rest. These drive the endpoint with the real multi-stage shape.
    """

    @mock_aws
    def test_a_run_with_many_stages_collapses_to_one_run(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        table = _create_table(dynamodb, with_index=True)
        _seed_multi_stage(table, "demo", runs=1, stages=13)

        body = json.loads(cp.lambda_handler(_event(), None)["body"])
        assert body["count"] == 1
        assert body["runs"][0]["run_id"] == "run-demo-000"

    @mock_aws
    def test_thirty_runs_of_thirteen_stages_all_appear(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        table = _create_table(dynamodb, with_index=True)
        _seed_multi_stage(table, "demo", runs=30, stages=13)

        body = json.loads(cp.lambda_handler(_event(), None)["body"])
        assert body["count"] == 30
        assert len({run["run_id"] for run in body["runs"]}) == 30

    @mock_aws
    def test_the_page_is_bounded_and_offers_a_cursor(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        table = _create_table(dynamodb, with_index=True)
        _seed_multi_stage(table, "demo", runs=cp._MAX_RUNS_LISTED + 10, stages=3)

        body = json.loads(cp.lambda_handler(_event(), None)["body"])
        assert body["count"] == cp._MAX_RUNS_LISTED
        assert body["next_token"] is not None

    @mock_aws
    def test_following_the_cursor_reaches_runs_the_first_page_omitted(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        table = _create_table(dynamodb, with_index=True)
        _seed_multi_stage(table, "demo", runs=cp._MAX_RUNS_LISTED + 10, stages=3)

        first = json.loads(cp.lambda_handler(_event(), None)["body"])
        event = _event()
        event["queryStringParameters"] = {"next_token": first["next_token"]}
        second = json.loads(cp.lambda_handler(event, None)["body"])

        assert second["count"] > 0
        assert {run["run_id"] for run in second["runs"]} - {
            run["run_id"] for run in first["runs"]
        }, "the second page returned nothing the first page had not already shown"

    @mock_aws
    def test_the_last_page_advertises_no_cursor(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        table = _create_table(dynamodb, with_index=True)
        _seed_multi_stage(table, "demo", runs=2, stages=3)

        body = json.loads(cp.lambda_handler(_event(), None)["body"])
        assert body["next_token"] is None
