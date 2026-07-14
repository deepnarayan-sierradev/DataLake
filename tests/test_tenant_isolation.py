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
    DynamoDB backend, EntityTypeRegistryClient): the entity-config table is
    NOT yet tenant-partitioned at the key level (see SEC-2 in
    architecture/GAP_ANALYSIS_FINDINGS.md), so isolation here is enforced in
    application code, not IAM. These tests exist specifically to catch a
    regression in that application-level guard, since IAM cannot back it up.
  - WatermarkRepository DynamoDB key isolation: genuinely tenant-scoped as of
    ARCH-1's fix (tenant_scoped_key() applied to the source_id key
    attribute) — the key-collision regression test below proves two tenants
    extracting the same source_id/entity_id no longer share one item.
  - RawLayerWriter S3 partition isolation (ARCH-1): the tenant_code root
    segment prevents two tenants' raw Parquet for the same source/entity
    from landing in the same prefix.
  - Circuit breaker tenant isolation (ARCH-1): one tenant's consecutive
    extraction failures on a shared connector type never opens the circuit
    for another tenant.
  - Control-plane API: a run lookup for another tenant's run_id returns 404
    (not 403), so the API never confirms a foreign run's existence.
  - FieldMappingRegistryClient / ResolutionConfigRegistry S3 prefix isolation
    (ARCH-10/ARCH-11): field mapping rule sets and entity-resolution
    match-rule/survivorship configs are now tenant-prefixed in S3, mirroring
    SchemaSnapshotRepository. ResolutionConfigRegistry additionally gets a
    regression test for its in-process cache key, since that registry
    instance is reused across warm Lambda invocations for different tenants.
  - ServingStoreConfigRepositoryClient DynamoDB backend: genuinely
    tenant-partitioned from creation (PK=tenant_code, not a composite
    tenant_scoped_key() retrofit like EntityExtractionConfig above) — the
    test below proves Tenant B's load_config/list_configs_for_tenant calls
    can never see Tenant A's serving-store config, across every target
    engine (MySQL, PostgreSQL, SQL Server, Azure SQL).

Analytics-publisher and golden/canonical-record-publisher S3 path isolation
(also fixed under ARCH-1) are covered in their own modules'
test suites — analytics_publisher/tests/test_analytics_publisher_handler.py
and entity_resolution/tests/test_canonical_record_publisher.py — since
proving them requires the same Glue/S3/registry fakes those suites already
build; duplicating that harness here would just be a second copy to keep in
sync, not new coverage.

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

from connector_runtime.adapters.mysql_rds.mysql_rds_raw_layer_writer import (
    MySqlRdsRawLayerWriter,
)
from connector_runtime.adapters.netsuite.netsuite_raw_layer_writer import (
    NetSuiteRawLayerWriter,
)
from connector_runtime.adapters.sage.common.sage_raw_layer_writer import SageRawLayerWriter
from connector_runtime.adapters.salesforce.salesforce_raw_layer_writer import (
    SalesforceRawLayerWriter,
)
from connector_runtime.api.control_plane_handler import lambda_handler as control_plane_handler
from connector_runtime.configuration_repository.configuration_repository import (
    ConfigurationBackend,
    ConfigurationNotFoundError,
    ConfigurationRepositoryClient,
)
from connector_runtime.interfaces.connector_interface import ExtractionRecord
from contracts.entity_configuration_contract import EntityExtractionConfig, LoadType
from contracts.identifier_policy import DEFAULT_TENANT_CODE
from contracts.serving_store_config_contract import ServingStoreEngine, ServingStoreLoadConfig
from entity_resolution.entity_type_registry import EntityTypeRecord, EntityTypeRegistryClient
from entity_resolution.resolution_config.resolution_config_registry import (
    ResolutionConfigNotFoundError,
    ResolutionConfigRegistry,
)
from orchestration.step_functions.extraction_retry_policy import ExtractionRetryPolicy
from schema_management.snapshot_repository.snapshot_repository import (
    FieldSnapshot,
    SchemaSnapshot,
    SchemaSnapshotRepository,
)
from serving_store.serving_store_config_repository import (
    ServingStoreConfigNotFoundError,
    ServingStoreConfigRepositoryClient,
)
from transformation.field_mapping.field_mapping_registry import (
    FieldMappingRegistryClient,
    FieldMappingRule,
    FieldMappingRuleSet,
    MappingRuleSetNotFoundError,
    MappingTransformation,
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
    _BUCKET = "edl-entity-config"

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
    _TABLE = "EdlEntityExtractionConfig"

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
# ServingStoreConfigRepositoryClient — DynamoDB backend (genuine partition isolation)
# ---------------------------------------------------------------------------


class TestServingStoreConfigRepositoryIsolation:
    _TABLE = "EdlServingStoreConfig"

    def _create_table(self) -> None:
        boto3.resource("dynamodb", region_name=_REGION).create_table(
            TableName=self._TABLE,
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

    @mock_aws
    @pytest.mark.parametrize(
        "engine",
        [
            ServingStoreEngine.MYSQL_RDS,
            ServingStoreEngine.POSTGRESQL,
            ServingStoreEngine.SQLSERVER,
            ServingStoreEngine.AZURE_SQL,
        ],
    )
    def test_tenant_b_cannot_read_tenant_a_config(self, engine: ServingStoreEngine) -> None:
        self._create_table()
        client = ServingStoreConfigRepositoryClient(environment=_ENV, region_name=_REGION)
        config = ServingStoreLoadConfig(
            tenant_code=_TENANT_A,
            entity_type="company",
            target_engine=engine,
            table_name="salesforce_account",
            primary_keys=("account_id",),
            secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test",
            region_name=_REGION,
        )
        client.save_config(config)

        loaded = client.load_config(_TENANT_A, "company")
        assert loaded.tenant_code == _TENANT_A

        # tenant_code is the DynamoDB partition key here — Tenant B's key literally
        # cannot address Tenant A's item, unlike the app-level guard above.
        with pytest.raises(ServingStoreConfigNotFoundError):
            client.load_config(_TENANT_B, "company")

    @mock_aws
    def test_list_configs_for_tenant_never_returns_another_tenants_records(self) -> None:
        self._create_table()
        client = ServingStoreConfigRepositoryClient(environment=_ENV, region_name=_REGION)
        for tenant, entity_type in ((_TENANT_A, "company"), (_TENANT_B, "person")):
            client.save_config(
                ServingStoreLoadConfig(
                    tenant_code=tenant,
                    entity_type=entity_type,
                    table_name=entity_type,
                    primary_keys=("id",),
                    secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test",
                    region_name=_REGION,
                )
            )

        tenant_a_configs = client.list_configs_for_tenant(_TENANT_A)
        assert [c.entity_type for c in tenant_a_configs] == ["company"]


# ---------------------------------------------------------------------------
# WatermarkRepository (app-level guard)
# ---------------------------------------------------------------------------


class TestWatermarkRepositoryIsolation:
    _TABLE = "EdlWatermarkRepository"

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
# WatermarkRepository — genuine key-collision proof (ARCH-1 fix)
# ---------------------------------------------------------------------------


class TestWatermarkRepositoryKeyIsolation:
    """
    Regression test for the ARCH-1 fix: WatermarkRepository's DynamoDB key is
    now tenant-scoped (tenant_scoped_key() applied to the source_id
    attribute). Before the fix, two tenants extracting the same
    source_id/entity_id shared one DynamoDB item — a post-read tenant check
    masked the collision for get_watermark(), but advance_watermark() would
    still silently overwrite the other tenant's watermark value. This proves
    the write side, not just the read side, using the exact tenant pair
    (DEFAULT_TENANT_CODE / "acme-test") named in
    architecture/MULTI_TENANT_ROLLOUT_PLAN.md's Phase 2 exit criterion.
    """

    _TABLE = "EdlWatermarkRepository"
    _OTHER_TENANT = "acme-test"

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
    def test_advancing_one_tenants_watermark_does_not_affect_the_other(self) -> None:
        self._create_table()
        repo = WatermarkRepository(environment=_ENV, region_name=_REGION)

        repo.initialise_watermark(
            "salesforce",
            "salesforce-account",
            upper_watermark=datetime(2026, 7, 1, tzinfo=UTC),
            run_id="run-demo-001",
            tenant_code=DEFAULT_TENANT_CODE,
        )
        repo.initialise_watermark(
            "salesforce",
            "salesforce-account",
            upper_watermark=datetime(2026, 7, 1, tzinfo=UTC),
            run_id="run-acme-001",
            tenant_code=self._OTHER_TENANT,
        )

        demo_record = repo.get_watermark(
            "salesforce", "salesforce-account", tenant_code=DEFAULT_TENANT_CODE
        )
        assert demo_record is not None
        repo.advance_watermark(
            current=demo_record,
            new_upper_watermark=datetime(2026, 7, 5, tzinfo=UTC),
            run_id="run-demo-002",
        )

        # The other tenant's watermark must be completely untouched by
        # demo's advance — this is the write-side proof the old
        # post-read-only guard could never provide.
        other_record = repo.get_watermark(
            "salesforce", "salesforce-account", tenant_code=self._OTHER_TENANT
        )
        assert other_record is not None
        assert other_record.upper_watermark == datetime(2026, 7, 1, tzinfo=UTC)
        assert other_record.run_id == "run-acme-001"

        demo_record_after = repo.get_watermark(
            "salesforce", "salesforce-account", tenant_code=DEFAULT_TENANT_CODE
        )
        assert demo_record_after is not None
        assert demo_record_after.upper_watermark == datetime(2026, 7, 5, tzinfo=UTC)


# ---------------------------------------------------------------------------
# RawLayerWriter — genuine S3 partition isolation (ARCH-1 fix)
# ---------------------------------------------------------------------------


def _new_salesforce_writer(bucket: str, tenant_code: str) -> Any:
    return SalesforceRawLayerWriter(s3_bucket=bucket, region_name=_REGION, tenant_code=tenant_code)


def _new_netsuite_writer(bucket: str, tenant_code: str) -> Any:
    return NetSuiteRawLayerWriter(s3_bucket=bucket, region_name=_REGION, tenant_code=tenant_code)


def _new_mysql_rds_writer(bucket: str, tenant_code: str) -> Any:
    return MySqlRdsRawLayerWriter(s3_bucket=bucket, region_name=_REGION, tenant_code=tenant_code)


def _new_sage_writer(bucket: str, tenant_code: str) -> Any:
    return SageRawLayerWriter(
        s3_bucket=bucket,
        sage_product="intacct",
        region_name=_REGION,
        tenant_code=tenant_code,
    )


# (writer_factory, source_id, entity_id, expected_source_segment) per connector
# adapter. expected_source_segment is the single, hyphenated path segment
# production wiring now produces (RAW-1) — no s3_prefix, no doubled segment.
_RAW_LAYER_WRITER_CASES = [
    (_new_salesforce_writer, "salesforce", "salesforce-account", "salesforce"),
    (_new_netsuite_writer, "netsuite", "netsuite-customer", "netsuite"),
    (_new_mysql_rds_writer, "mysql-rds", "mysql-rds-orders", "mysql-rds"),
    (_new_sage_writer, "sage", "sage-intacct-customer", "sage-intacct"),
]


class TestRawLayerWriterPathIsolation:
    """
    Regression test for ARCH-1: RawLayerWriter previously had no tenant_code
    parameter at all, so every tenant's raw Parquet for a given
    source/entity/run_id landed at the same S3 prefix.

    Parametrized across all four connector adapters — a prior version of
    this test only exercised the Salesforce subclass, which proved the
    shared base-class mechanism but would not have caught a regression
    isolated to one adapter's constructor (e.g. Sage forgetting to forward
    tenant_code to super().__init__()).
    """

    _BUCKET = "edl-raw-tenant-isolation-test"

    @pytest.mark.parametrize(
        "make_writer, source_id, entity_id, expected_source_segment",
        _RAW_LAYER_WRITER_CASES,
        ids=["salesforce", "netsuite", "mysql-rds", "sage"],
    )
    @mock_aws
    def test_two_tenants_same_run_id_produce_distinct_prefixes(
        self,
        make_writer: Any,
        source_id: str,
        entity_id: str,
        expected_source_segment: str,
    ) -> None:
        boto3.client("s3", region_name=_REGION).create_bucket(Bucket=self._BUCKET)
        records = [ExtractionRecord(payload={"Id": "1", "Name": "Acme"})]

        writer_a = make_writer(self._BUCKET, _TENANT_A)
        writer_b = make_writer(self._BUCKET, _TENANT_B)

        key_a = writer_a.write_partition(
            records, source_id, entity_id, "run-shared-001", "a" * 64, "2026-07-01"
        )
        key_b = writer_b.write_partition(
            records, source_id, entity_id, "run-shared-001", "a" * 64, "2026-07-01"
        )

        assert key_a != key_b

        # RAW-1: exactly {tenant_code}/{source}/{entity_id}/... — a single
        # source segment, no s3_prefix, no doubled "{source}/{source}".
        expected_a = (
            f"{_TENANT_A}/{expected_source_segment}/{entity_id}"
            f"/extraction_date=2026-07-01/run_id=run-shared-001/data.parquet"
        )
        expected_b = (
            f"{_TENANT_B}/{expected_source_segment}/{entity_id}"
            f"/extraction_date=2026-07-01/run_id=run-shared-001/data.parquet"
        )
        assert key_a == expected_a
        assert key_b == expected_b
        assert key_a.startswith(f"{_TENANT_A}/")
        assert key_b.startswith(f"{_TENANT_B}/")

        # Both objects genuinely exist independently — neither write
        # overwrote the other.
        s3 = boto3.client("s3", region_name=_REGION)
        assert s3.get_object(Bucket=self._BUCKET, Key=key_a)["Body"].read()
        assert s3.get_object(Bucket=self._BUCKET, Key=key_b)["Body"].read()


# ---------------------------------------------------------------------------
# Circuit breaker — tenant isolation (ARCH-1 fix)
# ---------------------------------------------------------------------------


class TestCircuitBreakerTenantIsolation:
    """
    Regression test for ARCH-1: the circuit breaker's key was previously
    `source_id:entity_id` with no tenant dimension, so one tenant's
    consecutive extraction failures on a shared connector type (e.g. both
    tenants running "salesforce") could open the circuit and block another
    tenant's healthy runs.
    """

    def test_tenant_a_failures_do_not_open_circuit_for_tenant_b(self) -> None:
        policy = ExtractionRetryPolicy(circuit_open_threshold=2)

        policy.record_failure("salesforce", tenant_code=_TENANT_A)
        policy.record_failure("salesforce", tenant_code=_TENANT_A)

        assert policy.is_circuit_open("salesforce", tenant_code=_TENANT_A) is True
        assert policy.is_circuit_open("salesforce", tenant_code=_TENANT_B) is False


# ---------------------------------------------------------------------------
# SchemaSnapshotRepository (genuine S3 prefix isolation)
# ---------------------------------------------------------------------------


class TestSchemaSnapshotRepositoryIsolation:
    _BUCKET = "edl-schema-snapshots-087972550871"

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
# FieldMappingRegistryClient (S3 prefix isolation — ARCH-11)
# ---------------------------------------------------------------------------


class TestFieldMappingRegistryIsolation:
    _BUCKET = "edl-curated-087972550871"

    def _rule_set(self) -> FieldMappingRuleSet:
        return FieldMappingRuleSet(
            source_id="salesforce",
            entity_id="salesforce-account",
            mapping_version="v1",
            rules=(
                FieldMappingRule(
                    source_fields=("Name",),
                    canonical_field="account_name",
                    transformation=MappingTransformation.RENAME,
                    transformation_params={},
                ),
            ),
        )

    @mock_aws
    def test_tenant_b_does_not_see_tenant_a_rule_set(self) -> None:
        boto3.client("s3", region_name=_REGION).create_bucket(Bucket=self._BUCKET)
        client = FieldMappingRegistryClient(s3_bucket=self._BUCKET, region_name=_REGION)
        client.publish_rule_set(self._rule_set(), _TENANT_A)

        loaded_a = client.load_rule_set("salesforce", "salesforce-account", _TENANT_A)
        assert loaded_a.mapping_version == "v1"

        with pytest.raises(MappingRuleSetNotFoundError):
            client.load_rule_set("salesforce", "salesforce-account", _TENANT_B)


# ---------------------------------------------------------------------------
# ResolutionConfigRegistry (S3 prefix isolation + in-process cache isolation
# — ARCH-10)
# ---------------------------------------------------------------------------

_ISOLATION_MATCH_RULES_FIXTURE = {
    "entity_type": "company",
    "rule_set_version": "v1",
    "rules": [
        {
            "rule_id": "email-exact",
            "strategy": "deterministic",
            "fields": [{"field_name": "email_address", "normalise": True}],
        }
    ],
}

_ISOLATION_SURVIVORSHIP_FIXTURE = {
    "entity_type": "company",
    "policy_version": "v1",
    "output_fields": ["email_address"],
    "default_strategy": "first_non_null",
    "attribute_rules": [],
}


class TestResolutionConfigRegistryIsolation:
    _BUCKET = "edl-curated-087972550871"

    def _publish_for(self, registry: ResolutionConfigRegistry, tenant_code: str) -> None:
        registry.publish(
            entity_type="company",
            tenant_code=tenant_code,
            match_rules_raw=_ISOLATION_MATCH_RULES_FIXTURE,
            survivorship_raw=_ISOLATION_SURVIVORSHIP_FIXTURE,
        )

    @mock_aws
    def test_tenant_b_does_not_see_tenant_a_config(self) -> None:
        boto3.client("s3", region_name=_REGION).create_bucket(Bucket=self._BUCKET)
        registry = ResolutionConfigRegistry(s3_bucket=self._BUCKET, region_name=_REGION)
        self._publish_for(registry, _TENANT_A)

        loaded_a = registry.load("company", _TENANT_A)
        assert loaded_a.entity_type == "company"

        with pytest.raises(ResolutionConfigNotFoundError):
            registry.load("company", _TENANT_B)

    @mock_aws
    def test_warm_container_reuse_does_not_leak_cached_config_across_tenants(self) -> None:
        """Regression: warm-container cache key must be tenant-scoped, not just the S3 key."""
        boto3.client("s3", region_name=_REGION).create_bucket(Bucket=self._BUCKET)
        registry = ResolutionConfigRegistry(s3_bucket=self._BUCKET, region_name=_REGION)
        self._publish_for(registry, _TENANT_A)

        config_a = registry.load("company", _TENANT_A)

        with pytest.raises(ResolutionConfigNotFoundError):
            registry.load("company", _TENANT_B)

        # Tenant A's own cache entry is unaffected by tenant B's failed lookup.
        assert registry.load("company", _TENANT_A) is config_a


# ---------------------------------------------------------------------------
# EntityTypeRegistryClient (single-table design, PK=tenant_code)
# ---------------------------------------------------------------------------


class TestEntityTypeRegistryIsolation:
    _TABLE = "EdlEntityTypeRegistry"

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
    _ENTITY_CONFIG_TABLE = "EdlEntityExtractionConfig"
    _ENTITY_TYPE_REGISTRY_TABLE = "EdlEntityTypeRegistry"
    _AUDIT_LOG_TABLE = "EdlRunAuditLog"

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
