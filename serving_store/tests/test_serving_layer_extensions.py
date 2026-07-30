"""View generation, RLS, engine constraint, merge and credential delivery (DL-07, DL-SCOPE-15)."""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from conftest import RESOURCE_NAME_ENVIRONMENT
from semantic.enterprise_model import TAG_FINANCE, build_enterprise_model
from semantic.semantic_model import Dimension, SemanticEntity
from serving_store.credential_delivery import (
    CLAIM_TTL_SECONDS,
    ROTATION_INTERVAL_DAYS,
    ClaimState,
    CredentialClaimError,
    ServingCredentialDelivery,
    serving_credential_secret_id,
)
from serving_store.merge_strategy import (
    MergeStrategyError,
    build_merge_plan,
    default_sizing_profile,
)
from serving_store.view_generator import (
    NATIVE_RLS_ENGINES,
    EngineUnsuitableError,
    IsolationMechanism,
    ServingEngine,
    decide_isolation,
    generate_entity_view,
    generate_row_security_policy,
    generate_views,
    render_view_script,
    require_suitable_engine,
    schema_per_scope_unit_statements,
    serving_view_s3_key,
)
from tenancy.scope_contract import PartitionKind, PartitionModel, TenantPartitionProfile

_REGION = "us-east-1"
_SINGLE = TenantPartitionProfile(tenant_code="demo")
_PARTITIONED = TenantPartitionProfile(
    tenant_code="evive",
    partition_model=PartitionModel.PARTITIONED,
    partition_kind=PartitionKind.FRANCHISE,
)
_ALL_TAGS = frozenset(
    {TAG_FINANCE, "dept_operations", "dept_sales_marketing", "tier_executive", "class_pii"}
)


class TestEngineIsolationConstraint:
    def test_single_tenant_needs_no_within_tenant_isolation(self):
        decision = decide_isolation(ServingEngine.MYSQL, _SINGLE)
        assert decision.mechanism is IsolationMechanism.NOT_REQUIRED
        assert decision.is_permitted is True

    def test_partitioned_tenant_on_mysql_is_unsuitable_by_default(self):
        decision = decide_isolation(ServingEngine.MYSQL, _PARTITIONED)
        assert decision.mechanism is IsolationMechanism.UNSUITABLE
        assert "no native row-level security" in decision.rationale
        with pytest.raises(EngineUnsuitableError, match="cannot isolate scope units"):
            require_suitable_engine(ServingEngine.MYSQL, _PARTITIONED)

    def test_mysql_may_opt_in_to_schema_per_scope_unit(self):
        decision = require_suitable_engine(
            ServingEngine.MYSQL, _PARTITIONED, allow_schema_per_scope_unit=True
        )
        assert decision.mechanism is IsolationMechanism.SCHEMA_PER_SCOPE_UNIT

    @pytest.mark.parametrize("engine", sorted(NATIVE_RLS_ENGINES, key=lambda e: e.value))
    def test_native_rls_engines_are_suitable(self, engine):
        assert decide_isolation(engine, _PARTITIONED).mechanism is IsolationMechanism.NATIVE_RLS


class TestViewGeneration:
    def _entity(self):
        return build_enterprise_model("evive").entity("ar_invoice")

    def test_view_exposes_the_model_columns(self):
        view = generate_entity_view(
            self._entity(),
            ServingEngine.POSTGRESQL,
            source_table="ar_invoice",
            granted_access_tags=_ALL_TAGS,
        )
        assert view.view_name == "vw_ar_invoice"
        assert "recognised_amount" in view.exposed_columns
        assert 'CREATE OR REPLACE VIEW "vw_ar_invoice"' in view.create_sql

    def test_untagged_audience_omits_the_finance_columns(self):
        view = generate_entity_view(
            self._entity(),
            ServingEngine.POSTGRESQL,
            source_table="ar_invoice",
            granted_access_tags=frozenset({"dept_operations"}),
        )
        assert "recognised_amount" not in view.exposed_columns
        assert "scope_unit_id" in view.exposed_columns

    def test_derived_metrics_are_not_materialised_in_a_view(self):
        view = generate_entity_view(
            self._entity(),
            ServingEngine.POSTGRESQL,
            source_table="ar_invoice",
            granted_access_tags=_ALL_TAGS,
        )
        assert "collection_rate" not in view.exposed_columns

    def test_dialect_quoting(self):
        for engine, opener in (
            (ServingEngine.MYSQL, "`"),
            (ServingEngine.SQLSERVER, "["),
            (ServingEngine.POSTGRESQL, '"'),
        ):
            view = generate_entity_view(
                self._entity(),
                engine,
                source_table="ar_invoice",
                granted_access_tags=_ALL_TAGS,
            )
            assert opener in view.create_sql

    def test_a_view_with_no_visible_columns_is_refused(self):
        entity = SemanticEntity(
            name="secret-entity",
            entity_type="secret_entity",
            dimensions=(
                Dimension(name="secret_column", column="secret_column", access_tag=TAG_FINANCE),
            ),
        )
        with pytest.raises(ValueError, match="no columns are visible"):
            generate_entity_view(
                entity,
                ServingEngine.POSTGRESQL,
                source_table="secret_entity",
                granted_access_tags=frozenset(),
                include_scope_column=False,
            )

    def test_model_wide_generation_skips_unviewable_entities(self):
        model = build_enterprise_model("evive")
        views = generate_views(
            model, ServingEngine.POSTGRESQL, granted_access_tags=frozenset({"dept_operations"})
        )
        assert len(views) > 0
        assert len(views) <= len(model.entities)

    def test_view_key_is_versioned_per_dialect(self):
        assert serving_view_s3_key("evive", "v3", ServingEngine.REDSHIFT) == (
            "evive/serving-views/v3.redshift.sql"
        )

    def test_script_renders_views_and_policies(self):
        model = build_enterprise_model("evive")
        views = generate_views(model, ServingEngine.POSTGRESQL, granted_access_tags=_ALL_TAGS)
        policy = generate_row_security_policy("ar_invoice", ServingEngine.POSTGRESQL)
        script = render_view_script(views, (policy,))
        assert "Generated from the published semantic model" in script
        assert "ENABLE ROW LEVEL SECURITY" in script


class TestRowSecurityPolicies:
    def test_postgres_policy_reads_a_session_setting(self):
        policy = generate_row_security_policy("ar_invoice", ServingEngine.POSTGRESQL)
        combined = " ".join(policy.sql_statements)
        assert "ENABLE ROW LEVEL SECURITY" in combined
        assert "FORCE ROW LEVEL SECURITY" in combined
        assert "current_setting('datalake.scope_units'" in combined
        assert "brand_code" in combined

    def test_brand_filtering_can_be_omitted(self):
        policy = generate_row_security_policy(
            "ar_invoice", ServingEngine.POSTGRESQL, include_brand=False
        )
        assert policy.security_columns == ("scope_unit_id",)
        assert "brand_code" not in " ".join(policy.sql_statements)

    def test_sqlserver_uses_a_predicate_function(self):
        policy = generate_row_security_policy("ar_invoice", ServingEngine.SQLSERVER)
        combined = " ".join(policy.sql_statements)
        assert "CREATE SECURITY POLICY" in combined
        assert "SESSION_CONTEXT" in combined

    def test_mysql_has_no_native_policy(self):
        with pytest.raises(EngineUnsuitableError, match="no native row-level security"):
            generate_row_security_policy("ar_invoice", ServingEngine.MYSQL)

    def test_mysql_fallback_grants_only_the_scoped_schema(self):
        statements = schema_per_scope_unit_statements(
            "evive", ("franchisee-0001",), ("ar_invoice",)
        )
        combined = " ".join(statements)
        assert "CREATE DATABASE IF NOT EXISTS `evive_franchisee_0001`" in combined
        assert "WHERE `scope_unit_id` = 'franchisee-0001'" in combined
        assert "GRANT SELECT ON `evive_franchisee_0001`.*" in combined
        assert "REVOKE ALL ON `evive`.*" in combined

    def test_non_allowlisted_table_name_is_rejected(self):
        with pytest.raises(ValueError, match="allowlisted SQL identifier"):
            generate_row_security_policy("ar_invoice; DROP TABLE x", ServingEngine.POSTGRESQL)


class TestMergePlan:
    _COLUMNS = ("invoice_id", "recognised_amount", "scope_unit_id")

    def test_merge_requires_a_primary_key(self):
        with pytest.raises(MergeStrategyError, match="requires at least one primary key"):
            build_merge_plan(ServingEngine.REDSHIFT, "ar_invoice", self._COLUMNS, ())

    def test_primary_key_must_be_loaded(self):
        with pytest.raises(MergeStrategyError, match="not among the loaded columns"):
            build_merge_plan(ServingEngine.REDSHIFT, "ar_invoice", self._COLUMNS, ("missing_key",))

    def test_redshift_uses_delete_then_insert_in_a_transaction(self):
        plan = build_merge_plan(
            ServingEngine.REDSHIFT, "ar_invoice", self._COLUMNS, ("invoice_id",)
        )
        combined = " ".join(plan.statements)
        assert combined.startswith("BEGIN;")
        assert "DELETE FROM ar_invoice USING stg_ar_invoice" in combined
        assert "INSERT INTO ar_invoice" in combined
        assert combined.rstrip().endswith("COMMIT;")

    def test_sqlserver_uses_merge(self):
        plan = build_merge_plan(
            ServingEngine.SQLSERVER, "ar_invoice", self._COLUMNS, ("invoice_id",)
        )
        assert any(s.startswith("MERGE ar_invoice") for s in plan.statements)

    def test_mysql_uses_on_duplicate_key(self):
        plan = build_merge_plan(ServingEngine.MYSQL, "ar_invoice", self._COLUMNS, ("invoice_id",))
        assert "ON DUPLICATE KEY UPDATE" in " ".join(plan.statements)

    def test_soft_delete_is_applied_after_the_merge(self):
        plan = build_merge_plan(
            ServingEngine.POSTGRESQL,
            "ar_invoice",
            (*self._COLUMNS, "is_deleted"),
            ("invoice_id",),
            soft_delete_column="is_deleted",
        )
        assert plan.statements[-1].startswith("DELETE FROM ar_invoice WHERE is_deleted")

    def test_verification_counts_distinct_keys(self):
        plan = build_merge_plan(
            ServingEngine.POSTGRESQL, "ar_invoice", self._COLUMNS, ("invoice_id",)
        )
        assert "COUNT(DISTINCT invoice_id)" in plan.verification_sql
        assert plan.statement_count > 0

    def test_unsafe_identifiers_are_rejected(self):
        with pytest.raises(MergeStrategyError, match="allowlisted SQL identifier"):
            build_merge_plan(
                ServingEngine.POSTGRESQL, "ar_invoice; DROP", self._COLUMNS, ("invoice_id",)
            )


class TestSizingProfile:
    def test_indexes_target_the_security_columns(self):
        profile = default_sizing_profile(("ar_invoice", "sales_order"))
        assert len(profile.indexes) == 2
        assert profile.indexes[0].columns == ("scope_unit_id", "brand_code", "activity_date")

    def test_redshift_uses_sort_keys_not_indexes(self):
        profile = default_sizing_profile(("ar_invoice",))
        sql = profile.indexes[0].create_sql(ServingEngine.REDSHIFT)
        assert "ALTER SORTKEY" in sql

    def test_other_engines_create_indexes(self):
        profile = default_sizing_profile(("ar_invoice",))
        assert profile.indexes[0].create_sql(ServingEngine.POSTGRESQL).startswith("CREATE INDEX")

    def test_summary_states_the_concurrency_target(self):
        summary = default_sizing_profile(("ar_invoice",)).render_summary()
        assert "Concurrency target" in summary
        assert "forbids throttling" in summary
        assert "mv_ar_invoice_daily" in summary


@mock_aws
class TestCredentialDelivery:
    def _delivery(self) -> ServingCredentialDelivery:
        boto3.client("dynamodb", region_name=_REGION).create_table(
            TableName=RESOURCE_NAME_ENVIRONMENT["SERVING_CLAIM_TABLE"],
            KeySchema=[
                {"AttributeName": "tenant_code", "KeyType": "HASH"},
                {"AttributeName": "claim_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "tenant_code", "AttributeType": "S"},
                {"AttributeName": "claim_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        secrets = boto3.client("secretsmanager", region_name=_REGION)
        secrets.create_secret(
            Name=serving_credential_secret_id("evive"),
            SecretString='{"username": "evive_ro", "password": "initial"}',
        )
        return ServingCredentialDelivery(environment="dev", region_name=_REGION)

    def test_claim_is_retrievable_exactly_once(self):
        delivery = self._delivery()
        claim = delivery.issue_claim("evive", issued_by="ops@example.test")
        credential = delivery.claim("evive", claim.claim_token)
        assert credential["username"] == "evive_ro"
        with pytest.raises(CredentialClaimError, match="already been used"):
            delivery.claim("evive", claim.claim_token)

    def test_claim_path_never_contains_the_password(self):
        delivery = self._delivery()
        claim = delivery.issue_claim("evive", issued_by="ops@example.test")
        assert "initial" not in claim.claim_url_path
        assert claim.claim_token in claim.claim_url_path

    def test_unknown_token_is_refused(self):
        delivery = self._delivery()
        with pytest.raises(CredentialClaimError, match="not recognised"):
            delivery.claim("evive", "not-a-token")

    def test_issuer_must_be_recorded(self):
        delivery = self._delivery()
        with pytest.raises(ValueError, match="must record who issued it"):
            delivery.issue_claim("evive", issued_by="")

    def test_revocation_invalidates_outstanding_claims(self):
        delivery = self._delivery()
        claim = delivery.issue_claim("evive", issued_by="ops@example.test")
        assert delivery.revoke_outstanding_claims("evive") == 1
        with pytest.raises(CredentialClaimError):
            delivery.claim("evive", claim.claim_token)

    def test_rotation_revokes_then_reissues(self):
        delivery = self._delivery()
        old_claim = delivery.issue_claim("evive", issued_by="ops@example.test")
        path = delivery.rotate("evive", rotated_by="ops@example.test")
        assert path.startswith("/tenants/evive/serving-credential/claim/")
        with pytest.raises(CredentialClaimError):
            delivery.claim("evive", old_claim.claim_token)

    def test_rotation_changes_the_stored_password(self):
        delivery = self._delivery()
        delivery.rotate("evive", rotated_by="ops@example.test")
        secrets = boto3.client("secretsmanager", region_name=_REGION)
        stored = secrets.get_secret_value(SecretId=serving_credential_secret_id("evive"))
        assert "initial" not in stored["SecretString"]

    def test_rotation_requires_an_actor(self):
        delivery = self._delivery()
        with pytest.raises(ValueError, match="must record who requested it"):
            delivery.rotate("evive", rotated_by="")

    def test_rotation_age_and_due_state(self):
        delivery = self._delivery()
        assert delivery.rotation_age_days("evive") is None
        assert delivery.is_rotation_due("evive") is True
        delivery.rotate("evive", rotated_by="ops@example.test")
        age = delivery.rotation_age_days("evive")
        assert age is not None
        assert age < ROTATION_INTERVAL_DAYS
        assert delivery.is_rotation_due("evive") is False

    def test_missing_secret_is_a_claim_error_not_a_crash(self):
        boto3.client("dynamodb", region_name=_REGION).create_table(
            TableName=RESOURCE_NAME_ENVIRONMENT["SERVING_CLAIM_TABLE"],
            KeySchema=[
                {"AttributeName": "tenant_code", "KeyType": "HASH"},
                {"AttributeName": "claim_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "tenant_code", "AttributeType": "S"},
                {"AttributeName": "claim_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        delivery = ServingCredentialDelivery(environment="dev", region_name=_REGION)
        claim = delivery.issue_claim("evive", issued_by="ops@example.test")
        with pytest.raises(CredentialClaimError, match="could not be read"):
            delivery.claim("evive", claim.claim_token)

    def test_claim_ttl_is_short(self):
        assert CLAIM_TTL_SECONDS <= 3_600

    def test_secret_path_is_per_tenant(self):
        assert serving_credential_secret_id("evive") == (
            f"{RESOURCE_NAME_ENVIRONMENT['SECRET_PATH_PREFIX']}/tenants/evive/serving-store/reader-credentials"
        )
        assert serving_credential_secret_id("acme-corp") != serving_credential_secret_id("evive")

    def test_claim_state_enum_covers_the_lifecycle(self):
        assert {s.value for s in ClaimState} == {"issued", "claimed", "expired", "revoked"}
