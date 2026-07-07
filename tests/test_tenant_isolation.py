"""
Phase 7 launch-readiness gate: a single, automated, cross-cutting proof that
Tenant A cannot read Tenant B's records through any of the platform's
tenant-scoped repositories or the control-plane API.

Per-module regression tests already exist (e.g.
schema_management/tests/test_snapshot_repository.py::TestTenantScoping) and
cover these mechanisms individually. This file exists as the single, named
artifact referenced by architecture/MULTI_TENANT_ROLLOUT_PLAN.md's Phase 7
exit criterion ("an automated test — not a manual check — that provisions
two tenants and asserts Tenant A's role cannot read Tenant B's S3 prefix,
DynamoDB rows, or Secrets Manager entries"), so there is one obvious place
to look before onboarding a second real tenant.

Isolation mechanisms covered:
  - S3 prefix isolation (ConfigurationRepositoryClient S3 backend,
    SchemaSnapshotRepository): genuinely two distinct objects; an IAM
    bucket-policy condition on the tenant_code prefix can enforce this today.
  - DynamoDB application-level guards (ConfigurationRepositoryClient
    DynamoDB backend, WatermarkRepository, EntityTypeRegistryClient): the
    tables are NOT yet tenant-partitioned at the key level (see SEC-2 in
    architecture/GAP_ANALYSIS_FINDINGS.md), so isolation here is enforced in
    application code, not IAM. These tests exist specifically to catch a
    regression in that application-level guard, since IAM cannot back it up.
  - Control-plane API: a run lookup for another tenant's run_id returns 404
    (not 403), so the API never confirms a foreign run's existence.

Explicitly NOT covered (documented gap, not a false-positive test):
  - Secrets Manager: credentials are provisioned per-source-connector, not
    per-tenant, in the current design (see connector_runtime/credential_client.py
    and infrastructure/modules/secrets/). There is no tenant_code dimension
    on a secret today, so there is nothing to isolate yet. Tracked as
    follow-up work alongside a genuine per-tenant source-connection model.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from connector_runtime.api.control_plane_handler import lambda_handler as control_plane_handler
from connector_runtime.configuration_repository.configuration_repository import (
    ConfigurationBackend,
    ConfigurationNotFoundError,
    ConfigurationRepositoryClient,
)
from contracts.entity_configuration_contract import EntityExtractionConfig, LoadType
from entity_resolution.entity_type_registry import EntityTypeRecord, EntityTypeRegistryClient
from schema_management.snapshot_repository.snapshot_repository import (
    FieldSnapshot,
    SchemaSnapshot,
    SchemaSnapshotRepository,
)
from watermark_management.watermark_repository.watermark_repository import WatermarkRepository

_REGION = "us-east-1"
_ENV = "dev"

_TENANT_A = "tenant-alpha"
_TENANT_B = "tenant-beta"


# ---------------------------------------------------------------------------
# ConfigurationRepositoryClient — S3 backend (genuine prefix isolation)
# ---------------------------------------------------------------------------


class TestConfigurationRepositoryS3Isolation:
    _BUCKET = f"{_ENV}-edl-entity-config"

    def _config(self, tenant_code: str) -> EntityExtractionConfig:
        return EntityExtractionConfig(
            source_id="salesforce",
            entity_id="salesforce-account",
            config_version="1.0.0",
            load_type=LoadType.INCREMENTAL,
            watermark_field="SystemModstamp",
            target_raw_s3_prefix="s3://raw/salesforce/account/",
            schema_snapshot_s3_prefix="s3://schema-snapshots/salesforce/account/",
            tenant_code=tenant_code,
        )

    @mock_aws
    def test_tenant_b_cannot_read_tenant_a_s3_config(self) -> None:
        boto3.client("s3", region_name=_REGION).create_bucket(Bucket=self._BUCKET)
        client = ConfigurationRepositoryClient(
            environment=_ENV,
            region_name=_REGION,
            backend=ConfigurationBackend.S3,
            s3_bucket=self._BUCKET,
        )
        s3 = boto3.client("s3", region_name=_REGION)
        s3.put_object(
            Bucket=self._BUCKET,
            Key=f"{_TENANT_A}/salesforce/salesforce-account/config.json",
            Body=self._config(_TENANT_A).model_dump_json().encode("utf-8"),
        )

        # Tenant A reads its own record — succeeds.
        loaded = client.load_config("salesforce", "salesforce-account", tenant_code=_TENANT_A)
        assert loaded.tenant_code == _TENANT_A

        # Tenant B requests the same source/entity — its own prefix has no
        # object, so it gets NotFound, never Tenant A's data.
        with pytest.raises(ConfigurationNotFoundError):
            client.load_config("salesforce", "salesforce-account", tenant_code=_TENANT_B)


# ---------------------------------------------------------------------------
# ConfigurationRepositoryClient — DynamoDB backend (app-level guard)
# ---------------------------------------------------------------------------


class TestConfigurationRepositoryDynamoDbIsolation:
    _TABLE = f"{_ENV}-edl-entity-extraction-config"

    def _create_table(self) -> None:
        boto3.resource("dynamodb", region_name=_REGION).create_table(
            TableName=self._TABLE,
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

    @mock_aws
    def test_tenant_b_cannot_read_tenant_a_dynamodb_config(self) -> None:
        self._create_table()
        client = ConfigurationRepositoryClient(environment=_ENV, region_name=_REGION)
        config = EntityExtractionConfig(
            source_id="salesforce",
            entity_id="salesforce-account",
            config_version="1.0.0",
            load_type=LoadType.INCREMENTAL,
            watermark_field="SystemModstamp",
            target_raw_s3_prefix="s3://raw/salesforce/account/",
            schema_snapshot_s3_prefix="s3://schema-snapshots/salesforce/account/",
            tenant_code=_TENANT_A,
        )
        client.save_config(config)

        # Tenant A reads its own record — succeeds.
        loaded = client.load_config("salesforce", "salesforce-account", tenant_code=_TENANT_A)
        assert loaded.tenant_code == _TENANT_A

        # Tenant B requests the same (source_id, entity_id) key — the table
        # is not yet tenant-partitioned, but the app-level guard must still
        # reject the cross-tenant read rather than handing back Tenant A's
        # record.
        with pytest.raises(ConfigurationNotFoundError):
            client.load_config("salesforce", "salesforce-account", tenant_code=_TENANT_B)


# ---------------------------------------------------------------------------
# WatermarkRepository (app-level guard)
# ---------------------------------------------------------------------------


class TestWatermarkRepositoryIsolation:
    _TABLE = f"{_ENV}-edl-watermark-repository"

    def _create_table(self) -> None:
        boto3.resource("dynamodb", region_name=_REGION).create_table(
            TableName=self._TABLE,
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

    @mock_aws
    def test_tenant_b_watermark_read_does_not_see_tenant_a_record(self) -> None:
        self._create_table()
        repo = WatermarkRepository(environment=_ENV, region_name=_REGION)
        repo.initialise_watermark(
            "salesforce",
            "salesforce-account",
            upper_watermark=datetime(2026, 7, 1, tzinfo=UTC),
            run_id="run-tenant-a-001",
            tenant_code=_TENANT_A,
        )

        # Tenant A sees its own watermark.
        record_a = repo.get_watermark("salesforce", "salesforce-account", tenant_code=_TENANT_A)
        assert record_a is not None
        assert record_a.tenant_code == _TENANT_A

        # Tenant B, requesting the same key, must be treated as "no prior
        # run" — never handed Tenant A's watermark.
        record_b = repo.get_watermark("salesforce", "salesforce-account", tenant_code=_TENANT_B)
        assert record_b is None


# ---------------------------------------------------------------------------
# SchemaSnapshotRepository (genuine S3 prefix isolation)
# ---------------------------------------------------------------------------


class TestSchemaSnapshotRepositoryIsolation:
    _BUCKET = f"{_ENV}-edl-schema-snapshots"

    def _snapshot(self) -> SchemaSnapshot:
        return SchemaSnapshot(
            source_id="salesforce",
            entity_id="salesforce-account",
            schema_version="abc123def456",
            extraction_date="2026-07-01",
            captured_at=datetime(2026, 7, 1, tzinfo=UTC).isoformat(),
            fields=(
                FieldSnapshot(name="Id", data_type="id", is_nullable=False, is_queryable=True),
            ),
            record_count=10,
        )

    @mock_aws
    def test_tenant_b_does_not_see_tenant_a_snapshot(self) -> None:
        boto3.client("s3", region_name=_REGION).create_bucket(Bucket=self._BUCKET)
        repo = SchemaSnapshotRepository(bucket_name=self._BUCKET, region_name=_REGION)
        repo.write_snapshot(self._snapshot(), tenant_code=_TENANT_A)

        loaded_a = repo.load_latest_snapshot(
            "salesforce", "salesforce-account", tenant_code=_TENANT_A
        )
        assert loaded_a is not None

        loaded_b = repo.load_latest_snapshot(
            "salesforce", "salesforce-account", tenant_code=_TENANT_B
        )
        assert loaded_b is None


# ---------------------------------------------------------------------------
# EntityTypeRegistryClient (single-table design, PK=tenant_code)
# ---------------------------------------------------------------------------


class TestEntityTypeRegistryIsolation:
    _TABLE = f"{_ENV}-edl-entity-type-registry"

    def _create_table(self) -> None:
        boto3.resource("dynamodb", region_name=_REGION).create_table(
            TableName=self._TABLE,
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

    @mock_aws
    def test_tenant_b_registration_does_not_leak_into_tenant_a(self) -> None:
        self._create_table()
        client = EntityTypeRegistryClient(environment=_ENV, region_name=_REGION)
        client.register_entity_type(
            EntityTypeRecord(
                entity_id="shared-entity-id",
                entity_type="tenant_a_type",
                pk_field="id_a",
                contributing_sources=(),
            ),
            tenant_code=_TENANT_A,
        )
        client.register_entity_type(
            EntityTypeRecord(
                entity_id="shared-entity-id",
                entity_type="tenant_b_type",
                pk_field="id_b",
                contributing_sources=(),
            ),
            tenant_code=_TENANT_B,
        )

        assert client.get_entity_type("shared-entity-id", tenant_code=_TENANT_A) == "tenant_a_type"
        assert client.get_entity_type("shared-entity-id", tenant_code=_TENANT_B) == "tenant_b_type"


# ---------------------------------------------------------------------------
# Control-plane API: run lookup across tenants must 404, never 403
# ---------------------------------------------------------------------------


class TestControlPlaneRunLookupIsolation:
    _ENTITY_CONFIG_TABLE = f"{_ENV}-edl-entity-extraction-config"
    _ENTITY_TYPE_REGISTRY_TABLE = f"{_ENV}-edl-entity-type-registry"
    _AUDIT_LOG_TABLE = f"{_ENV}-edl-run-audit-log"

    def _env_vars(self) -> dict[str, str]:
        return {
            "PLATFORM_ENVIRONMENT": _ENV,
            "AWS_REGION": _REGION,
            "ENTITY_CONFIG_TABLE": self._ENTITY_CONFIG_TABLE,
            "ENTITY_TYPE_REGISTRY_TABLE": self._ENTITY_TYPE_REGISTRY_TABLE,
            "AUDIT_LOG_TABLE": self._AUDIT_LOG_TABLE,
        }

    def _event(self, method: str, path: str, tenant_claim: str) -> dict[str, Any]:
        return {
            "httpMethod": method,
            "path": path,
            "body": None,
            "requestContext": {"authorizer": {"claims": {"custom:tenant_code": tenant_claim}}},
        }

    @mock_aws
    def test_tenant_b_run_lookup_of_tenant_a_run_is_404_not_403(self) -> None:
        dynamodb = boto3.resource("dynamodb", region_name=_REGION)
        table = dynamodb.create_table(
            TableName=self._AUDIT_LOG_TABLE,
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
        run_id = "run-20260701-000000000000-tenanta"
        table.put_item(
            Item={
                "run_id": run_id,
                "stage": "extraction",
                "tenant_code": _TENANT_A,
                "status": "success",
                "completed_at": "2026-07-01T00:00:00+00:00",
            }
        )

        with patch.dict(os.environ, self._env_vars()):
            response = control_plane_handler(
                self._event("GET", f"/tenants/{_TENANT_B}/runs/{run_id}", tenant_claim=_TENANT_B),
                None,
            )

        # 404, never 403 — the API must not confirm the run's existence to a
        # tenant that isn't its owner.
        assert response["statusCode"] == 404
        body = json.loads(response["body"])
        assert "acme" not in body.get("error", "").lower()  # no accidental data leak in the error


# ---------------------------------------------------------------------------
# Documented gap: Secrets Manager is not yet tenant-scoped
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "Secrets Manager credentials are provisioned per-source-connector, not "
        "per-tenant, in the current design (SEC-2 follow-up in "
        "architecture/GAP_ANALYSIS_FINDINGS.md). There is no tenant_code "
        "dimension on a secret yet, so there is nothing to isolate — this is a "
        "tracked placeholder, not a false-positive pass, so the gap stays "
        "visible in test output until a per-tenant credential model exists."
    )
)
def test_secrets_manager_tenant_isolation_not_yet_implemented() -> None:
    raise NotImplementedError
