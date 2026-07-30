"""
Guards the scope fixtures themselves (the negative test for the test instrument).

A fixture that quietly degenerates takes every test built on it down with it, silently: the suite
stays green while proving nothing. These assertions fail the moment `unit_a_claims` stops being
able to deny something.
"""

from __future__ import annotations

from conftest import SINGLE_TENANT, UNIT_A, UNIT_B
from tenancy.scope_contract import (
    IMPLICIT_SCOPE_UNIT_ID,
    PartitionModel,
    ResolutionScope,
    TenantPartitionProfile,
)
from tenancy.scope_predicate import (
    ConsumptionSurface,
    ScopeClaims,
    ScopePredicate,
    scope_predicate,
)


def _predicate(claims: ScopeClaims) -> ScopePredicate:
    return scope_predicate(claims, surface=ConsumptionSurface.TWIN_TRAVERSAL)


class TestPartitionedFixtureCanDeny:
    def test_unit_a_matches_its_own_unit(self, unit_a_claims: ScopeClaims) -> None:
        assert _predicate(unit_a_claims).matches(UNIT_A) is True

    def test_unit_a_denies_the_sibling_unit(self, unit_a_claims: ScopeClaims) -> None:
        assert _predicate(unit_a_claims).matches(UNIT_B) is False

    def test_unit_a_denies_unattributed_rows(self, unit_a_claims: ScopeClaims) -> None:
        assert _predicate(unit_a_claims).matches(None) is False

    def test_unit_a_is_not_match_all(self, unit_a_claims: ScopeClaims) -> None:
        assert _predicate(unit_a_claims).matches_all_rows is False

    def test_siblings_are_mutually_invisible(
        self, unit_a_claims: ScopeClaims, unit_b_claims: ScopeClaims
    ) -> None:
        assert _predicate(unit_b_claims).matches(UNIT_A) is False
        assert _predicate(unit_a_claims).matches(UNIT_B) is False


class TestTenantWideIsAnAffirmativeGrant:
    def test_tenant_wide_matches_every_unit(self, tenant_wide_claims: ScopeClaims) -> None:
        assert _predicate(tenant_wide_claims).matches(UNIT_A) is True
        assert _predicate(tenant_wide_claims).matches(UNIT_B) is True
        assert _predicate(tenant_wide_claims).matches_all_rows is True


class TestWhyTheDefaultTenantProvesNothing:
    """Pins the blind spot that hid the twin defect, so nobody rediscovers it the hard way."""

    def test_single_tenant_claim_matches_unattributed_rows(
        self, single_tenant_claims: ScopeClaims
    ) -> None:
        assert single_tenant_claims.scope_unit_ids == frozenset({IMPLICIT_SCOPE_UNIT_ID})
        assert _predicate(single_tenant_claims).matches(None) is True

    def test_default_tenant_is_single_partition(
        self, single_profile: TenantPartitionProfile
    ) -> None:
        assert single_profile.tenant_code == SINGLE_TENANT
        assert single_profile.partition_model is PartitionModel.SINGLE
        assert single_profile.default_resolution_scope is ResolutionScope.TENANT
