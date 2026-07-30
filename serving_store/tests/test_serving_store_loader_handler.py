"""Tests for the serving store load Lambda handler."""

from __future__ import annotations

import io
from typing import Any

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import structlog
from moto import mock_aws

import serving_store.serving_store_loader_handler as handler_module
from conftest import RESOURCE_NAME_ENVIRONMENT
from serving_store.loaders.mysql_rds_loader import ServingStoreLoadResult
from serving_store.serving_store_loader_handler import _validate_event, lambda_handler

_REGION = "us-east-1"
_BUCKET = "datalake-analytics-test"
_CONFIG_TABLE = RESOURCE_NAME_ENVIRONMENT["SERVING_STORE_CONFIG_TABLE"]
_SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:test"

_BASE_EVENT: dict[str, Any] = {
    "source_id": "salesforce",
    "entity_id": "salesforce-account",
    "entity_type": "company",
    "environment": "dev",
    "run_id": "run-ss-test-001",
    "tenant_code": "acme-corp",
    "analytics_s3_prefix": "acme-corp/analytics/company/analytics_date=2026-07-11/",
}


def _create_config_table(dynamodb: Any) -> Any:
    return dynamodb.create_table(
        TableName=_CONFIG_TABLE,
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


def _put_parquet_fixture(s3: Any, key: str) -> None:
    table = pa.table({"account_id": ["001", "002"], "name": ["Acme Corp", "Beta Ltd"]})
    buf = io.BytesIO()
    pq.write_table(table, buf)
    s3.put_object(Bucket=_BUCKET, Key=key, Body=buf.getvalue())


class TestValidateEventFields:
    def test_missing_field_raises(self) -> None:
        event = {k: v for k, v in _BASE_EVENT.items() if k != "tenant_code"}
        with pytest.raises(ValueError, match="tenant_code"):
            _validate_event(event)

    def test_invalid_tenant_code_rejected(self) -> None:
        with pytest.raises(ValueError, match="tenant_code"):
            _validate_event({**_BASE_EVENT, "tenant_code": "BAD_CODE"})

    def test_unknown_environment_rejected(self) -> None:
        with pytest.raises(ValueError, match="Unknown environment"):
            _validate_event({**_BASE_EVENT, "environment": "qa"})

    def test_unsafe_prefix_rejected(self) -> None:
        with pytest.raises(ValueError, match="analytics_s3_prefix"):
            _validate_event({**_BASE_EVENT, "analytics_s3_prefix": "../etc/passwd"})

    def test_valid_event_passes(self) -> None:
        _validate_event(dict(_BASE_EVENT))

    def test_underscore_entity_type_accepted(self) -> None:
        _validate_event({**_BASE_EVENT, "entity_type": "ap_bill"})

    def test_invalid_entity_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="entity_type"):
            _validate_event({**_BASE_EVENT, "entity_type": "Invalid Type"})


class TestContextvarsAndErrorHandling:
    """
    The handler routes through `stage_execution` since 2026-07-29 (it was one of five stages with no
    DLQ producer), so it now reads AWS_REGION to build the SQS client that enqueues a failure.
    """

    @pytest.fixture(autouse=True)
    def _region(self, monkeypatch: Any) -> None:
        monkeypatch.setenv("AWS_REGION", _REGION)

    def setup_method(self, method: object = None) -> None:
        structlog.contextvars.clear_contextvars()

    def teardown_method(self, method: object = None) -> None:
        structlog.contextvars.clear_contextvars()

    def test_contextvars_cleared_after_success(self, monkeypatch) -> None:
        monkeypatch.setattr(
            handler_module, "_run_serving_store_load", lambda **_kwargs: {"skipped": True}
        )
        lambda_handler(dict(_BASE_EVENT), context=None)
        assert structlog.contextvars.get_contextvars() == {}

    def test_contextvars_cleared_after_failure(self, monkeypatch) -> None:
        def _boom(**_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("simulated failure")

        monkeypatch.setattr(handler_module, "_run_serving_store_load", _boom)
        with pytest.raises(RuntimeError, match="simulated failure"):
            lambda_handler(dict(_BASE_EVENT), context=None)
        assert structlog.contextvars.get_contextvars() == {}


@mock_aws
class TestRunServingStoreLoad:
    def setup_method(self, method: object = None) -> None:
        boto3.client("s3", region_name=_REGION).create_bucket(Bucket=_BUCKET)
        _create_config_table(boto3.resource("dynamodb", region_name=_REGION))

    def test_no_config_returns_skipped(self, monkeypatch) -> None:
        monkeypatch.setenv("AWS_REGION", _REGION)
        monkeypatch.setenv("ANALYTICS_S3_BUCKET", _BUCKET)

        result = handler_module._run_serving_store_load(
            entity_id="salesforce-account",
            entity_type="company",
            environment="dev",
            run_id="run-1",
            tenant_code="acme-corp",
            analytics_s3_prefix=_BASE_EVENT["analytics_s3_prefix"],
            context=None,
        )
        assert result == {"skipped": True, "reason": "no_config"}

    def test_disabled_config_returns_skipped(self, monkeypatch) -> None:
        monkeypatch.setenv("AWS_REGION", _REGION)
        monkeypatch.setenv("ANALYTICS_S3_BUCKET", _BUCKET)
        boto3.resource("dynamodb", region_name=_REGION).Table(_CONFIG_TABLE).put_item(
            Item={
                "tenant_code": "acme-corp",
                "entity_type": "company",
                "target_engine": "mysql_rds",
                "table_name": "salesforce_account",
                "primary_keys": ["account_id"],
                "secret_arn": _SECRET_ARN,
                "region_name": _REGION,
                "enabled": False,
            }
        )

        result = handler_module._run_serving_store_load(
            entity_id="salesforce-account",
            entity_type="company",
            environment="dev",
            run_id="run-1",
            tenant_code="acme-corp",
            analytics_s3_prefix=_BASE_EVENT["analytics_s3_prefix"],
            context=None,
        )
        assert result == {"skipped": True, "reason": "disabled"}

    def test_enabled_config_invokes_loader_and_shapes_result(self, monkeypatch) -> None:
        monkeypatch.setenv("AWS_REGION", _REGION)
        monkeypatch.setenv("ANALYTICS_S3_BUCKET", _BUCKET)
        boto3.resource("dynamodb", region_name=_REGION).Table(_CONFIG_TABLE).put_item(
            Item={
                "tenant_code": "acme-corp",
                "entity_type": "company",
                "target_engine": "mysql_rds",
                "table_name": "salesforce_account",
                "primary_keys": ["account_id"],
                "secret_arn": _SECRET_ARN,
                "region_name": _REGION,
                "enabled": True,
            }
        )
        _put_parquet_fixture(
            boto3.client("s3", region_name=_REGION), "acme-corp/analytics/company/data.parquet"
        )

        captured: dict[str, Any] = {}

        class _FakeLoader:
            supports_s3_bulk_load = False

            def __init__(self, **kwargs: Any) -> None:
                captured["init_kwargs"] = kwargs

            def apply_statements(self, statements, tenant_code, table_name) -> int:
                captured["rls_statements"] = list(statements)
                return len(statements)

            def load_batches(self, record_batches, table_name, primary_keys, tenant_code, **kwargs):
                captured["batches"] = list(record_batches)
                captured["table_name"] = table_name
                captured["primary_keys"] = primary_keys
                captured["tenant_code"] = tenant_code
                return ServingStoreLoadResult(
                    database_name="acme_corp",
                    table_name=table_name,
                    records_loaded=2,
                    records_skipped=0,
                    started_at="2026-07-11T00:00:00+00:00",
                    completed_at="2026-07-11T00:00:01+00:00",
                )

        monkeypatch.setattr(
            handler_module.serving_store_registry,
            "resolve",
            lambda _engine, **kw: _FakeLoader(**kw),
        )

        result = handler_module._run_serving_store_load(
            entity_id="salesforce-account",
            entity_type="company",
            environment="dev",
            run_id="run-1",
            tenant_code="acme-corp",
            analytics_s3_prefix="acme-corp/analytics/company/",
            context=None,
        )

        assert result["skipped"] is False
        assert result["database_name"] == "acme_corp"
        assert result["records_loaded"] == 2
        assert captured["table_name"] == "salesforce_account"
        assert captured["primary_keys"] == ("account_id",)
        assert len(captured["batches"]) == 1
        assert len(captured["batches"][0]) == 2

    def test_s3_bulk_engine_uses_load_from_s3_not_batches(self, monkeypatch) -> None:
        monkeypatch.setenv("AWS_REGION", _REGION)
        monkeypatch.setenv("ANALYTICS_S3_BUCKET", _BUCKET)
        boto3.resource("dynamodb", region_name=_REGION).Table(_CONFIG_TABLE).put_item(
            Item={
                "tenant_code": "acme-corp",
                "entity_type": "company",
                "target_engine": "redshift",
                "table_name": "salesforce_account",
                "primary_keys": ["account_id"],
                "secret_arn": _SECRET_ARN,
                "region_name": _REGION,
                "enabled": True,
            }
        )

        captured: dict[str, Any] = {}

        class _FakeS3Loader:
            supports_s3_bulk_load = True

            def __init__(self, **kwargs: Any) -> None:
                pass

            def apply_statements(self, statements, tenant_code, table_name) -> int:
                return len(statements)

            def load_from_s3(self, bucket, prefix, table_name, primary_keys, tenant_code, **kwargs):
                captured["bucket"] = bucket
                captured["prefix"] = prefix
                captured["table_name"] = table_name
                return ServingStoreLoadResult(
                    database_name="acme_corp",
                    table_name=table_name,
                    records_loaded=7,
                    records_skipped=1,
                    started_at="2026-07-22T00:00:00+00:00",
                    completed_at="2026-07-22T00:00:01+00:00",
                )

            def load_batches(self, *args, **kwargs):  # pragma: no cover - must not be called
                raise AssertionError("load_batches must not be called for an S3-bulk engine")

        monkeypatch.setattr(
            handler_module.serving_store_registry,
            "resolve",
            lambda _engine, **kw: _FakeS3Loader(**kw),
        )

        result = handler_module._run_serving_store_load(
            entity_id="salesforce-account",
            entity_type="company",
            environment="dev",
            run_id="run-1",
            tenant_code="acme-corp",
            analytics_s3_prefix="acme-corp/analytics/company/",
            context=None,
        )

        assert result["records_loaded"] == 7
        assert result["records_skipped"] == 1
        assert captured["bucket"] == _BUCKET
        assert captured["table_name"] == "salesforce_account"
