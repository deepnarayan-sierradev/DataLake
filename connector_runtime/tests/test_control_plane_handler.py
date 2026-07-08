"""
Tests for connector_runtime/api/control_plane_handler.py

Covers:
  - Tenant provisioning: success + duplicate rejection (409)
  - Entity registration: validation failures (400) + success + duplicate (409)
  - Entity listing (tenant-scoped Scan)
  - Pipeline trigger: correct SQS FIFO message shape enqueued
  - Run status: tenant-mismatch rejection (404) — security-critical
  - Run status: not-found (404), success summary
  - Authentication/authorization: missing claims (401), tenant mismatch (403)
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import patch

import boto3
from moto import mock_aws

from connector_runtime.api.control_plane_handler import lambda_handler

_REGION = "us-east-1"
_ENV = "dev"
_ENTITY_CONFIG_TABLE = "EdlEntityExtractionConfig"
_ENTITY_TYPE_REGISTRY_TABLE = "EdlEntityTypeRegistry"
_AUDIT_LOG_TABLE = "EdlRunAuditLog"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _event(
    method: str,
    path: str,
    *,
    tenant_claim: str | None = "demo",
    body: dict[str, Any] | None = None,
    no_claims: bool = False,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "httpMethod": method,
        "path": path,
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {},
    }
    if not no_claims:
        event["requestContext"]["authorizer"] = {"claims": {"custom:tenant_code": tenant_claim}}
    return event


def _create_entity_config_table(dynamodb: Any) -> Any:
    return dynamodb.create_table(
        TableName=_ENTITY_CONFIG_TABLE,
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


def _create_entity_type_registry_table(dynamodb: Any) -> Any:
    return dynamodb.create_table(
        TableName=_ENTITY_TYPE_REGISTRY_TABLE,
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


def _create_run_audit_log_table(dynamodb: Any) -> Any:
    return dynamodb.create_table(
        TableName=_AUDIT_LOG_TABLE,
        KeySchema=[
            {"AttributeName": "run_id", "KeyType": "HASH"},
            {"AttributeName": "stage", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "run_id", "AttributeType": "S"},
            {"AttributeName": "stage", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


_BASE_ENV_VARS = {
    "PLATFORM_ENVIRONMENT": _ENV,
    "AWS_REGION": _REGION,
    "ENTITY_CONFIG_TABLE": _ENTITY_CONFIG_TABLE,
    "ENTITY_TYPE_REGISTRY_TABLE": _ENTITY_TYPE_REGISTRY_TABLE,
    "AUDIT_LOG_TABLE": _AUDIT_LOG_TABLE,
}

_VALID_ENTITY_BODY: dict[str, Any] = {
    "source_id": "salesforce",
    "entity_id": "salesforce-account",
    "config_version": "1.0.0",
    "load_type": "incremental",
    "watermark_field": "SystemModstamp",
    "target_raw_s3_prefix": "s3://raw/salesforce/account/",
    "schema_snapshot_s3_prefix": "s3://schema-snapshots/salesforce/account/",
}


# ---------------------------------------------------------------------------
# Tenant provisioning
# ---------------------------------------------------------------------------


class TestCreateTenant:
    @mock_aws
    def test_provision_new_tenant_succeeds(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_entity_type_registry_table(dynamodb)

        with patch.dict(os.environ, _BASE_ENV_VARS):
            response = lambda_handler(
                _event("POST", "/tenants", body={"tenant_code": "acme-corp"}), None
            )

        assert response["statusCode"] == 201
        body = json.loads(response["body"])
        assert body["tenant_code"] == "acme-corp"
        assert body["status"] == "active"

    @mock_aws
    def test_duplicate_tenant_rejected_with_409(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        table = _create_entity_type_registry_table(dynamodb)
        table.put_item(
            Item={"tenant_code": "acme-corp", "sk": "tenant_registry#meta", "status": "active"}
        )

        with patch.dict(os.environ, _BASE_ENV_VARS):
            response = lambda_handler(
                _event("POST", "/tenants", body={"tenant_code": "acme-corp"}), None
            )

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert "already exists" in body["error"]

    @mock_aws
    def test_invalid_tenant_code_rejected_with_400(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_entity_type_registry_table(dynamodb)

        with patch.dict(os.environ, _BASE_ENV_VARS):
            response = lambda_handler(
                _event("POST", "/tenants", body={"tenant_code": "UPPER_CASE"}), None
            )

        assert response["statusCode"] == 400

    def test_missing_authenticated_context_rejected_with_401(self) -> None:
        with patch.dict(os.environ, _BASE_ENV_VARS):
            response = lambda_handler(
                _event("POST", "/tenants", body={"tenant_code": "acme-corp"}, no_claims=True), None
            )
        assert response["statusCode"] == 401


# ---------------------------------------------------------------------------
# Entity registration / listing
# ---------------------------------------------------------------------------


class TestEntityRegistration:
    @mock_aws
    def test_register_entity_succeeds(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_entity_config_table(dynamodb)

        with patch.dict(os.environ, _BASE_ENV_VARS):
            response = lambda_handler(
                _event(
                    "POST",
                    "/tenants/demo/entities",
                    tenant_claim="demo",
                    body=_VALID_ENTITY_BODY,
                ),
                None,
            )

        assert response["statusCode"] == 201
        body = json.loads(response["body"])
        assert body["source_id"] == "salesforce"
        assert body["entity_id"] == "salesforce-account"
        assert body["tenant_code"] == "demo"

    @mock_aws
    def test_register_entity_validation_failure_returns_400(self) -> None:
        """Missing watermark_field with load_type=incremental fails EntityExtractionConfig."""
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_entity_config_table(dynamodb)

        invalid_body = {**_VALID_ENTITY_BODY}
        del invalid_body["watermark_field"]

        with patch.dict(os.environ, _BASE_ENV_VARS):
            response = lambda_handler(
                _event("POST", "/tenants/demo/entities", tenant_claim="demo", body=invalid_body),
                None,
            )

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "failed validation" in body["error"]

    @mock_aws
    def test_register_entity_extra_field_rejected(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_entity_config_table(dynamodb)

        invalid_body = {**_VALID_ENTITY_BODY, "unknown_field": "nope"}

        with patch.dict(os.environ, _BASE_ENV_VARS):
            response = lambda_handler(
                _event("POST", "/tenants/demo/entities", tenant_claim="demo", body=invalid_body),
                None,
            )

        assert response["statusCode"] == 400

    @mock_aws
    def test_register_duplicate_entity_returns_409(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_entity_config_table(dynamodb)

        with patch.dict(os.environ, _BASE_ENV_VARS):
            first = lambda_handler(
                _event(
                    "POST", "/tenants/demo/entities", tenant_claim="demo", body=_VALID_ENTITY_BODY
                ),
                None,
            )
            second = lambda_handler(
                _event(
                    "POST", "/tenants/demo/entities", tenant_claim="demo", body=_VALID_ENTITY_BODY
                ),
                None,
            )

        assert first["statusCode"] == 201
        assert second["statusCode"] == 409

    @mock_aws
    def test_register_entity_body_tenant_mismatch_rejected(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_entity_config_table(dynamodb)

        mismatched_body = {**_VALID_ENTITY_BODY, "tenant_code": "other-tenant"}

        with patch.dict(os.environ, _BASE_ENV_VARS):
            response = lambda_handler(
                _event("POST", "/tenants/demo/entities", tenant_claim="demo", body=mismatched_body),
                None,
            )

        assert response["statusCode"] == 400

    @mock_aws
    def test_path_tenant_mismatch_with_authenticated_tenant_returns_403(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_entity_config_table(dynamodb)

        with patch.dict(os.environ, _BASE_ENV_VARS):
            response = lambda_handler(
                _event(
                    "POST",
                    "/tenants/acme-corp/entities",
                    tenant_claim="demo",  # authenticated as "demo" but targeting "acme-corp"
                    body=_VALID_ENTITY_BODY,
                ),
                None,
            )

        assert response["statusCode"] == 403

    @mock_aws
    def test_list_entities_scoped_to_tenant(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        table = _create_entity_config_table(dynamodb)
        table.put_item(Item={**_VALID_ENTITY_BODY, "tenant_code": "demo"})
        table.put_item(
            Item={
                **_VALID_ENTITY_BODY,
                "entity_id": "salesforce-contact",
                "tenant_code": "acme-corp",
            }
        )

        with patch.dict(os.environ, _BASE_ENV_VARS):
            response = lambda_handler(
                _event("GET", "/tenants/demo/entities", tenant_claim="demo"), None
            )

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["count"] == 1
        assert body["entities"][0]["entity_id"] == "salesforce-account"


# ---------------------------------------------------------------------------
# Pipeline trigger
# ---------------------------------------------------------------------------


class TestTriggerPipeline:
    @mock_aws
    def test_trigger_enqueues_correct_message_shape(self) -> None:
        sqs = boto3.client("sqs", region_name=_REGION)
        queue = sqs.create_queue(
            QueueName="EdlPipelineTrigger.fifo",
            Attributes={"FifoQueue": "true", "ContentBasedDeduplication": "true"},
        )
        queue_url = queue["QueueUrl"]

        env_vars = {**_BASE_ENV_VARS, "PIPELINE_TRIGGER_QUEUE_URL": queue_url}
        with patch.dict(os.environ, env_vars):
            response = lambda_handler(
                _event(
                    "POST",
                    "/tenants/demo/pipelines/trigger",
                    tenant_claim="demo",
                    body={
                        "source_id": "salesforce",
                        "entity_id": "salesforce-account",
                        "connector_params": {"object_name": "Account"},
                    },
                ),
                None,
            )

        assert response["statusCode"] == 202
        body = json.loads(response["body"])
        assert body["status"] == "enqueued"

        messages = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1)
        received = json.loads(messages["Messages"][0]["Body"])
        assert received["source_id"] == "salesforce"
        assert received["entity_id"] == "salesforce-account"
        assert received["tenant_code"] == "demo"
        assert received["environment"] == _ENV
        assert received["connector_params"] == {"object_name": "Account"}
        assert received["is_replay"] is False

    @mock_aws
    def test_trigger_invalid_source_id_returns_400(self) -> None:
        sqs = boto3.client("sqs", region_name=_REGION)
        queue = sqs.create_queue(
            QueueName="EdlPipelineTrigger.fifo",
            Attributes={"FifoQueue": "true", "ContentBasedDeduplication": "true"},
        )
        env_vars = {**_BASE_ENV_VARS, "PIPELINE_TRIGGER_QUEUE_URL": queue["QueueUrl"]}

        with patch.dict(os.environ, env_vars):
            response = lambda_handler(
                _event(
                    "POST",
                    "/tenants/demo/pipelines/trigger",
                    tenant_claim="demo",
                    body={"source_id": "INVALID_ID!", "entity_id": "salesforce-account"},
                ),
                None,
            )

        assert response["statusCode"] == 400

    @mock_aws
    def test_trigger_tenant_mismatch_returns_403(self) -> None:
        sqs = boto3.client("sqs", region_name=_REGION)
        queue = sqs.create_queue(
            QueueName="EdlPipelineTrigger.fifo",
            Attributes={"FifoQueue": "true", "ContentBasedDeduplication": "true"},
        )
        env_vars = {**_BASE_ENV_VARS, "PIPELINE_TRIGGER_QUEUE_URL": queue["QueueUrl"]}

        with patch.dict(os.environ, env_vars):
            response = lambda_handler(
                _event(
                    "POST",
                    "/tenants/acme-corp/pipelines/trigger",
                    tenant_claim="demo",
                    body={"source_id": "salesforce", "entity_id": "salesforce-account"},
                ),
                None,
            )

        assert response["statusCode"] == 403


# ---------------------------------------------------------------------------
# Run status — tenant isolation is security-critical
# ---------------------------------------------------------------------------


class TestGetRunStatus:
    @mock_aws
    def test_run_status_success_summary(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        table = _create_run_audit_log_table(dynamodb)
        run_id = "run-20260707-120000000000-a1b2c3d4"
        table.put_item(
            Item={
                "run_id": run_id,
                "stage": "extraction",
                "tenant_code": "demo",
                "status": "success",
                "completed_at": "2026-07-07T12:00:00+00:00",
                "duration_ms": 1234,
            }
        )

        with patch.dict(os.environ, _BASE_ENV_VARS):
            response = lambda_handler(
                _event("GET", f"/tenants/demo/runs/{run_id}", tenant_claim="demo"), None
            )

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["run_id"] == run_id
        assert body["status"] == "success"
        assert len(body["stages"]) == 1

    @mock_aws
    def test_run_status_tenant_mismatch_returns_404_not_403(self) -> None:
        """
        Security-critical: a run belonging to a different tenant must be reported
        as not-found, never as a permission error — no cross-tenant existence leak.
        """
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        table = _create_run_audit_log_table(dynamodb)
        run_id = "run-20260707-120000000000-deadbeef"
        table.put_item(
            Item={
                "run_id": run_id,
                "stage": "extraction",
                "tenant_code": "acme-corp",
                "status": "success",
                "completed_at": "2026-07-07T12:00:00+00:00",
            }
        )

        with patch.dict(os.environ, _BASE_ENV_VARS):
            response = lambda_handler(
                # Authenticated as "demo", requesting a run that belongs to "acme-corp".
                _event("GET", f"/tenants/demo/runs/{run_id}", tenant_claim="demo"),
                None,
            )

        assert response["statusCode"] == 404

    @mock_aws
    def test_run_status_not_found_returns_404(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_run_audit_log_table(dynamodb)

        with patch.dict(os.environ, _BASE_ENV_VARS):
            response = lambda_handler(
                _event(
                    "GET",
                    "/tenants/demo/runs/run-20260707-000000000000-ffffffff",
                    tenant_claim="demo",
                ),
                None,
            )

        assert response["statusCode"] == 404

    @mock_aws
    def test_run_status_invalid_run_id_returns_400(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_run_audit_log_table(dynamodb)

        with patch.dict(os.environ, _BASE_ENV_VARS):
            response = lambda_handler(
                # Bare sequential integer run_id is rejected by validate_run_id.
                _event("GET", "/tenants/demo/runs/12345", tenant_claim="demo"),
                None,
            )

        assert response["statusCode"] == 400

    @mock_aws
    def test_run_status_authenticated_tenant_mismatch_with_path_returns_403(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        _create_run_audit_log_table(dynamodb)

        with patch.dict(os.environ, _BASE_ENV_VARS):
            response = lambda_handler(
                _event(
                    "GET",
                    "/tenants/acme-corp/runs/run-20260707-000000000000-ffffffff",
                    tenant_claim="demo",
                ),
                None,
            )

        assert response["statusCode"] == 403


class TestListRuns:
    @mock_aws
    def test_list_runs_scoped_to_tenant(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        table = _create_run_audit_log_table(dynamodb)
        table.put_item(
            Item={
                "run_id": "run-a",
                "stage": "extraction",
                "tenant_code": "demo",
                "status": "success",
                "completed_at": "2026-07-07T12:00:00+00:00",
                "source_id": "salesforce",
                "entity_id": "salesforce-account",
            }
        )
        table.put_item(
            Item={
                "run_id": "run-b",
                "stage": "extraction",
                "tenant_code": "acme-corp",
                "status": "success",
                "completed_at": "2026-07-07T13:00:00+00:00",
                "source_id": "salesforce",
                "entity_id": "salesforce-account",
            }
        )

        with patch.dict(os.environ, _BASE_ENV_VARS):
            response = lambda_handler(
                _event("GET", "/tenants/demo/runs", tenant_claim="demo"), None
            )

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body["count"] == 1
        assert body["runs"][0]["run_id"] == "run-a"


# ---------------------------------------------------------------------------
# Routing / misc
# ---------------------------------------------------------------------------


class TestRoutingAndErrors:
    def test_unknown_route_returns_404(self) -> None:
        with patch.dict(os.environ, _BASE_ENV_VARS):
            response = lambda_handler(_event("DELETE", "/tenants/demo/entities"), None)
        assert response["statusCode"] == 404

    def test_invalid_json_body_returns_400(self) -> None:
        event = _event("POST", "/tenants", tenant_claim="demo")
        event["body"] = "not-json{{{"
        with patch.dict(os.environ, _BASE_ENV_VARS):
            response = lambda_handler(event, None)
        assert response["statusCode"] == 400

    @mock_aws
    def test_error_response_never_leaks_raw_exception_text(self) -> None:
        """Unexpected failures must return a generic message, never the raw exception string."""
        with (
            patch.dict(os.environ, _BASE_ENV_VARS),
            patch(
                "connector_runtime.api.control_plane_handler._entity_type_registry_table",
                side_effect=RuntimeError("super secret internal detail"),
            ),
        ):
            response = lambda_handler(
                _event("POST", "/tenants", body={"tenant_code": "acme-corp"}), None
            )

        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert "super secret internal detail" not in body["error"]
