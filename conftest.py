"""
Scope fixtures shared by every isolation test in the repo.

The 2026-07-28 re-audit found the twin scope filter reading a field the model never carried. It
went undetected because every isolation test used `demo`, a `single`-partition tenant whose claim
contains `__tenant__`, so `matches(None)` is `True` and the filter cannot fail. A test written
against `demo` alone proves nothing about scope isolation.

Use `unit_a_claims` (or `unit_b_claims`) for any test asserting a scope boundary, and reach for
`single_tenant_claims` only when the degenerate case is the thing under test.
"""

from __future__ import annotations

from typing import Final

import pytest

from tenancy.scope_contract import (
    PartitionKind,
    PartitionModel,
    ScopeUnit,
    TenantPartitionProfile,
)
from tenancy.scope_predicate import ScopeClaims, build_scope_claims

PARTITIONED_TENANT: Final[str] = "evive"
SINGLE_TENANT: Final[str] = "demo"
UNIT_A: Final[str] = "franchisee-0001"
UNIT_B: Final[str] = "franchisee-0002"

TEST_ENVIRONMENT: Final[str] = "dev"
TEST_NAME_PREFIX: Final[str] = "datalake"


def _table(purpose: str) -> str:
    return f"{TEST_NAME_PREFIX}-{purpose}-{TEST_ENVIRONMENT}"


_TENANT_KEYED_TABLES: Final[tuple[str, ...]] = tuple(
    _table(purpose)
    for purpose in (
        "backfill-jobs",
        "brand-registry",
        "config-governance",
        "config-restatements",
        "data-quality-exceptions",
        "effective-config",
        "export-jobs",
        "quality-policy-attachments",
        "reconciliation-reports",
        "saved-query",
        "scope-units",
        "semantic-approvals",
        "semantic-model",
        "serving-credential-claims",
        "serving-store-config",
        "source-connections",
        "source-onboarding-registry",
        "subprocessor-register",
        "tenant-usage-metering",
        "twin-index",
        "webhook-event-dedup",
        "workflow-circuit-breaker",
        "workflow-definitions",
        "workflow-destinations",
        "workflow-executions",
        "workflow-idempotency",
        "workflow-tasks",
    )
)

RESOURCE_NAME_ENVIRONMENT: Final[dict[str, str]] = {
    "RESOURCE_NAME_PREFIX": TEST_NAME_PREFIX,
    "SECRET_PATH_PREFIX": f"{TEST_NAME_PREFIX}/{TEST_ENVIRONMENT}",
    "PLATFORM_ENVIRONMENT": TEST_ENVIRONMENT,
    "RAW_S3_BUCKET": f"{TEST_NAME_PREFIX}-raw-{TEST_ENVIRONMENT}-use1",
    "CURATED_S3_BUCKET": f"{TEST_NAME_PREFIX}-curated-{TEST_ENVIRONMENT}-use1",
    "ANALYTICS_S3_BUCKET": f"{TEST_NAME_PREFIX}-analytics-{TEST_ENVIRONMENT}-use1",
    "SCHEMA_SNAPSHOT_S3_BUCKET": f"{TEST_NAME_PREFIX}-schema-snapshots-{TEST_ENVIRONMENT}-use1",
    "AUDIT_LOG_TABLE": _table("run-audit-log"),
    "BACKFILL_JOB_TABLE": _table("backfill-jobs"),
    "BRAND_REGISTRY_TABLE": _table("brand-registry"),
    "CONFIG_GOVERNANCE_TABLE": _table("config-governance"),
    "CONFIG_RESTATEMENT_TABLE": _table("config-restatements"),
    "DATA_QUALITY_EXCEPTION_TABLE": _table("data-quality-exceptions"),
    "DELETION_CERTIFICATE_TABLE": _table("deletion-certificates"),
    "EFFECTIVE_CONFIG_TABLE": _table("effective-config"),
    "ENTITY_CONFIG_TABLE": _table("entity-extraction-config"),
    "ENTITY_TYPE_REGISTRY_TABLE": _table("entity-type-registry"),
    "EXPORT_JOB_TABLE": _table("export-jobs"),
    "QUALITY_POLICY_TABLE": _table("quality-policy-attachments"),
    "RECONCILIATION_REPORT_TABLE": _table("reconciliation-reports"),
    "SAVED_QUERY_TABLE": _table("saved-query"),
    "SCOPE_UNIT_TABLE": _table("scope-units"),
    "SEMANTIC_APPROVAL_TABLE": _table("semantic-approvals"),
    "SEMANTIC_MODEL_TABLE": _table("semantic-model"),
    "SERVING_CLAIM_TABLE": _table("serving-credential-claims"),
    "SERVING_STORE_CONFIG_TABLE": _table("serving-store-config"),
    "SOURCE_CONNECTION_TABLE": _table("source-connections"),
    "SOURCE_ONBOARDING_TABLE": _table("source-onboarding-registry"),
    "SUBPROCESSOR_TABLE": _table("subprocessor-register"),
    "TENANT_USAGE_TABLE": _table("tenant-usage-metering"),
    "TWIN_INDEX_TABLE": _table("twin-index"),
    "WATERMARK_TABLE": _table("watermark"),
    "WEBHOOK_DEDUP_TABLE": _table("webhook-event-dedup"),
    "WORKFLOW_BREAKER_TABLE": _table("workflow-circuit-breaker"),
    "WORKFLOW_DEFINITION_TABLE": _table("workflow-definitions"),
    "WORKFLOW_DESTINATION_TABLE": _table("workflow-destinations"),
    "WORKFLOW_EXECUTION_TABLE": _table("workflow-executions"),
    "WORKFLOW_IDEMPOTENCY_TABLE": _table("workflow-idempotency"),
    "WORKFLOW_TASK_TABLE": _table("workflow-tasks"),
    "TENANT_KEYED_TABLES": ",".join(_TENANT_KEYED_TABLES),
    "TENANT_SCOPED_KEY_TABLES": ",".join((_table("entity-extraction-config"), _table("watermark"))),
    "TENANT_ATTRIBUTED_TABLES": _table("run-audit-log"),
    "DELETION_EVIDENCE_TABLES": _table("deletion-certificates"),
    "PREVENT_DESTROY_TABLES": ",".join(
        _table(purpose)
        for purpose in (
            "watermark",
            "run-audit-log",
            "entity-extraction-config",
            "entity-type-registry",
            "serving-store-config",
            "twin-index",
            "semantic-model",
            "saved-query",
        )
    ),
}


@pytest.fixture(autouse=True)
def resource_name_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Supply the deploy-time resource names every repository now reads from the environment."""
    for name, value in RESOURCE_NAME_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def partitioned_profile() -> TenantPartitionProfile:
    """A franchise tenant: the case where an absent scope filter is a disclosure."""
    return TenantPartitionProfile(
        tenant_code=PARTITIONED_TENANT,
        partition_model=PartitionModel.PARTITIONED,
        partition_kind=PartitionKind.FRANCHISE,
    )


@pytest.fixture
def single_profile() -> TenantPartitionProfile:
    """The degenerate tenant, where every scope check legitimately matches everything."""
    return TenantPartitionProfile(tenant_code=SINGLE_TENANT, partition_model=PartitionModel.SINGLE)


@pytest.fixture
def scope_units() -> list[ScopeUnit]:
    """Two sibling units, so a cross-unit reach has somewhere to reach to."""
    return [
        ScopeUnit(
            tenant_code=PARTITIONED_TENANT,
            scope_unit_id=unit_id,
            partition_kind=PartitionKind.FRANCHISE,
            display_name=unit_id,
        )
        for unit_id in (UNIT_A, UNIT_B)
    ]


@pytest.fixture
def unit_a_claims(
    partitioned_profile: TenantPartitionProfile, scope_units: list[ScopeUnit]
) -> ScopeClaims:
    """A caller granted exactly one franchisee."""
    return build_scope_claims(
        PARTITIONED_TENANT,
        partitioned_profile,
        granted_scope_unit_ids=frozenset({UNIT_A}),
        units=scope_units,
    )


@pytest.fixture
def unit_b_claims(
    partitioned_profile: TenantPartitionProfile, scope_units: list[ScopeUnit]
) -> ScopeClaims:
    """The sibling caller, used to assert A's rows are invisible to B."""
    return build_scope_claims(
        PARTITIONED_TENANT,
        partitioned_profile,
        granted_scope_unit_ids=frozenset({UNIT_B}),
        units=scope_units,
    )


@pytest.fixture
def tenant_wide_claims(
    partitioned_profile: TenantPartitionProfile, scope_units: list[ScopeUnit]
) -> ScopeClaims:
    """An affirmative tenant-wide grant, which is not the same as an absent claim."""
    return build_scope_claims(
        PARTITIONED_TENANT, partitioned_profile, tenant_wide=True, units=scope_units
    )


@pytest.fixture
def single_tenant_claims(single_profile: TenantPartitionProfile) -> ScopeClaims:
    """The `demo` claim most existing tests use; kept explicit so its weakness is visible."""
    return build_scope_claims(SINGLE_TENANT, single_profile)
