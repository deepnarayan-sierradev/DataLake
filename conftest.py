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
