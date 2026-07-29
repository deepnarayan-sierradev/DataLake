"""
Adversarial tests for the single scope predicate builder (DL-SCOPE-14, DL-SCOPE-18).

Tested as an attacker, not as a filter: the empty-claim case, the crafted-claim case, and
the degenerate single-tenant case each get a dedicated assertion.
"""

from __future__ import annotations

import pytest

from tenancy.scope_contract import (
    IMPLICIT_SCOPE_UNIT_ID,
    PartitionKind,
    PartitionModel,
    ScopeUnit,
    TenantPartitionProfile,
)
from tenancy.scope_predicate import (
    ConsumptionSurface,
    EmptyScopeDenialError,
    ScopeClaims,
    UnknownScopeUnitError,
    build_scope_claims,
    expand_scope_grant,
    scope_predicate,
)

_SINGLE = TenantPartitionProfile(tenant_code="demo")
_PARTITIONED = TenantPartitionProfile(
    tenant_code="evive",
    partition_model=PartitionModel.PARTITIONED,
    partition_kind=PartitionKind.FRANCHISE,
)


def _unit(scope_unit_id: str, parent: str | None = None) -> ScopeUnit:
    return ScopeUnit(
        tenant_code="evive",
        scope_unit_id=scope_unit_id,
        partition_kind=PartitionKind.FRANCHISE,
        display_name=scope_unit_id,
        parent_scope_unit_id=parent,
    )


class TestEmptyScopeMeansDeny:
    def test_empty_claim_denies_all_access(self):
        claims = ScopeClaims(tenant_code="evive")
        assert claims.is_empty is True
        with pytest.raises(EmptyScopeDenialError, match="denies all access"):
            scope_predicate(claims)

    def test_empty_claim_denies_on_every_surface(self):
        claims = ScopeClaims(tenant_code="evive")
        for surface in ConsumptionSurface:
            with pytest.raises(EmptyScopeDenialError):
                scope_predicate(claims, surface=surface)


class TestDegenerateSingleTenant:
    def test_predicate_is_applied_and_matches_everything(self):
        claims = build_scope_claims("demo", _SINGLE)
        predicate = scope_predicate(claims)
        assert predicate.matches_all_rows is True
        assert "scope_unit_id" in predicate.sql
        assert predicate.matches(None) is True
        assert predicate.matches(IMPLICIT_SCOPE_UNIT_ID) is True
        assert predicate.matches("anything-at-all") is True

    def test_single_tenant_ignores_stray_unit_grants(self):
        # A single tenant has exactly one implicit unit; a crafted grant cannot add units.
        claims = build_scope_claims(
            "demo", _SINGLE, granted_scope_unit_ids=frozenset({"franchisee-0042"})
        )
        assert claims.scope_unit_ids == frozenset({IMPLICIT_SCOPE_UNIT_ID})


class TestPartitionedTenant:
    def test_unit_scoped_predicate_binds_values_as_parameters(self):
        claims = build_scope_claims(
            "evive",
            _PARTITIONED,
            granted_scope_unit_ids=frozenset({"franchisee-0001"}),
            units=[_unit("franchisee-0001"), _unit("franchisee-0002")],
        )
        predicate = scope_predicate(claims)
        assert predicate.sql == "scope_unit_id IN (:scope_unit_0)"
        assert predicate.parameters == {"scope_unit_0": "franchisee-0001"}
        assert predicate.matches_all_rows is False

    def test_cross_unit_row_is_not_matched(self):
        claims = build_scope_claims(
            "evive",
            _PARTITIONED,
            granted_scope_unit_ids=frozenset({"franchisee-0001"}),
            units=[_unit("franchisee-0001"), _unit("franchisee-0002")],
        )
        predicate = scope_predicate(claims)
        assert predicate.matches("franchisee-0002") is False

    def test_unattributable_row_is_not_visible_to_a_unit_scoped_caller(self):
        # DL-12 D2: null resolves to tenant-level, never "visible to everyone".
        claims = build_scope_claims(
            "evive",
            _PARTITIONED,
            granted_scope_unit_ids=frozenset({"franchisee-0001"}),
            units=[_unit("franchisee-0001")],
        )
        assert scope_predicate(claims).matches(None) is False

    def test_tenant_wide_is_an_affirmative_grant(self):
        claims = build_scope_claims("evive", _PARTITIONED, tenant_wide=True)
        predicate = scope_predicate(claims)
        assert predicate.matches_all_rows is True
        assert predicate.matches("franchisee-0002") is True
        assert predicate.matches(None) is True

    def test_crafted_claim_naming_an_unknown_unit_is_rejected(self):
        with pytest.raises(UnknownScopeUnitError, match="do not exist"):
            build_scope_claims(
                "evive",
                _PARTITIONED,
                granted_scope_unit_ids=frozenset({"franchisee-9999"}),
                units=[_unit("franchisee-0001")],
            )

    def test_grant_with_no_units_still_denies(self):
        claims = build_scope_claims(
            "evive", _PARTITIONED, granted_scope_unit_ids=frozenset(), units=[_unit("f-1")]
        )
        with pytest.raises(EmptyScopeDenialError):
            scope_predicate(claims)


class TestUnseededPartitionedTenantCannotBecomeMatchAll:
    """
    The `known and u not in known` short-circuit meant an empty unit list skipped validation
    entirely, so a partitioned tenant whose units were not yet seeded accepted any grant —
    including the implicit sentinel, which builds a match-all predicate.
    """

    def test_unknown_unit_is_rejected_even_when_no_units_are_registered(self):
        with pytest.raises(UnknownScopeUnitError, match="do not exist"):
            build_scope_claims(
                "evive",
                _PARTITIONED,
                granted_scope_unit_ids=frozenset({"franchisee-0001"}),
                units=[],
            )

    def test_implicit_unit_is_rejected_for_a_partitioned_tenant(self):
        with pytest.raises(UnknownScopeUnitError, match="reserved implicit"):
            build_scope_claims(
                "evive",
                _PARTITIONED,
                granted_scope_unit_ids=frozenset({IMPLICIT_SCOPE_UNIT_ID}),
                units=[_unit("franchisee-0001")],
            )

    def test_implicit_unit_is_rejected_even_with_no_units_registered(self):
        # The exact bypass: unseeded tenant + crafted `__tenant__` claim.
        with pytest.raises(UnknownScopeUnitError):
            build_scope_claims(
                "evive",
                _PARTITIONED,
                granted_scope_unit_ids=frozenset({IMPLICIT_SCOPE_UNIT_ID}),
                units=[],
            )

    def test_hand_built_implicit_claim_cannot_reach_the_match_all_branch(self):
        # Defence in depth: the predicate builder does not trust the caller to have validated.
        claims = ScopeClaims(
            tenant_code="evive", scope_unit_ids=frozenset({IMPLICIT_SCOPE_UNIT_ID})
        )
        with pytest.raises(UnknownScopeUnitError, match="not declared single-partition"):
            scope_predicate(claims)

    def test_positive_control_a_declared_single_tenant_still_matches_all(self):
        # Without this, a builder that always raised would pass every test above.
        claims = build_scope_claims("demo", _SINGLE)
        assert scope_predicate(claims).matches_all_rows is True


class TestHierarchyExpansion:
    def test_parent_grant_expands_to_leaves(self):
        units = [
            _unit("region-north"),
            _unit("franchisee-0001", parent="region-north"),
            _unit("franchisee-0002", parent="region-north"),
            _unit("franchisee-0003"),
        ]
        expanded = expand_scope_grant(frozenset({"region-north"}), units)
        assert expanded == frozenset({"region-north", "franchisee-0001", "franchisee-0002"})

    def test_expansion_terminates_on_a_cycle(self):
        # A cycle is a data defect; expansion must still terminate rather than hang.
        units = [
            _unit("unit-a", parent="unit-b"),
            _unit("unit-b", parent="unit-a"),
        ]
        assert expand_scope_grant(frozenset({"unit-a"}), units) == frozenset({"unit-a", "unit-b"})

    def test_expansion_happens_at_claim_issuance(self):
        units = [_unit("region-north"), _unit("franchisee-0001", parent="region-north")]
        claims = build_scope_claims(
            "evive", _PARTITIONED, granted_scope_unit_ids=frozenset({"region-north"}), units=units
        )
        assert claims.scope_unit_ids == frozenset({"region-north", "franchisee-0001"})


class TestInjectionDefence:
    def test_non_allowlisted_column_is_rejected(self):
        claims = build_scope_claims("demo", _SINGLE)
        with pytest.raises(ValueError, match="allowlisted identifier"):
            scope_predicate(claims, column="scope_unit_id; DROP TABLE x")

    def test_scope_unit_ids_are_validated_on_claim_construction(self):
        with pytest.raises(ValueError, match="scope unit id format"):
            ScopeClaims(tenant_code="evive", scope_unit_ids=frozenset({"'; DROP TABLE x --"}))
