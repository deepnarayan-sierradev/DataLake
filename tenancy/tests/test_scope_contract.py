"""Scope-unit dimension contract tests (DL-SCOPE-01, DL-SCOPE-02, DL-SCOPE-11)."""

from __future__ import annotations

from datetime import date

import pytest

from tenancy.scope_contract import (
    IMPLICIT_SCOPE_UNIT_ID,
    HistoryInheritancePolicy,
    PartitionKind,
    PartitionModel,
    ResolutionScope,
    ScopeUnit,
    TenantPartitionProfile,
    validate_scope_unit_id,
)


class TestTenantPartitionProfile:
    def test_single_tenant_has_one_implicit_unit(self):
        profile = TenantPartitionProfile(tenant_code="demo")
        assert profile.partition_model is PartitionModel.SINGLE
        assert profile.implicit_units() == (IMPLICIT_SCOPE_UNIT_ID,)

    def test_single_tenant_defaults_to_tenant_resolution(self):
        assert (
            TenantPartitionProfile(tenant_code="demo").default_resolution_scope
            is ResolutionScope.TENANT
        )

    def test_partitioned_tenant_defaults_to_scope_unit_resolution(self):
        profile = TenantPartitionProfile(
            tenant_code="evive",
            partition_model=PartitionModel.PARTITIONED,
            partition_kind=PartitionKind.FRANCHISE,
        )
        assert profile.default_resolution_scope is ResolutionScope.SCOPE_UNIT
        assert profile.implicit_units() == ()

    def test_partitioned_without_kind_is_rejected(self):
        with pytest.raises(ValueError, match="no partition_kind"):
            TenantPartitionProfile(tenant_code="evive", partition_model=PartitionModel.PARTITIONED)

    def test_single_with_kind_is_rejected(self):
        with pytest.raises(ValueError, match="one implicit unit"):
            TenantPartitionProfile(
                tenant_code="demo",
                partition_model=PartitionModel.SINGLE,
                partition_kind=PartitionKind.FRANCHISE,
            )

    def test_invalid_tenant_code_rejected(self):
        with pytest.raises(ValueError, match="tenant code format"):
            TenantPartitionProfile(tenant_code="Bad_Tenant")


class TestScopeUnit:
    def _unit(self, **overrides):
        base = {
            "tenant_code": "evive",
            "scope_unit_id": "franchisee-0042",
            "partition_kind": PartitionKind.FRANCHISE,
            "display_name": "Maid Brigade of Anywhere",
        }
        return ScopeUnit(**{**base, **overrides})

    def test_effective_by_default(self):
        assert self._unit().is_effective_on() is True

    def test_inactive_unit_is_not_effective(self):
        assert self._unit(active=False).is_effective_on() is False

    def test_effective_dates_bound_the_unit(self):
        unit = self._unit(effective_from=date(2026, 1, 1), effective_to=date(2026, 6, 30))
        assert unit.is_effective_on(date(2026, 3, 1)) is True
        assert unit.is_effective_on(date(2025, 12, 31)) is False
        assert unit.is_effective_on(date(2026, 7, 1)) is False

    def test_reversed_effective_dates_rejected(self):
        with pytest.raises(ValueError, match="precedes effective_from"):
            self._unit(effective_from=date(2026, 6, 30), effective_to=date(2026, 1, 1))

    def test_self_parent_rejected(self):
        with pytest.raises(ValueError, match="own parent"):
            self._unit(parent_scope_unit_id="franchisee-0042")

    def test_history_inheritance_defaults_to_none(self):
        assert self._unit().history_inheritance is HistoryInheritancePolicy.NONE


class TestScopeUnitIdValidation:
    def test_the_implicit_sentinel_is_not_a_registrable_unit_id(self) -> None:
        with pytest.raises(ValueError, match="reserved"):
            validate_scope_unit_id("__tenant__")

    def test_the_sentinel_is_permitted_inside_a_claim(self) -> None:
        assert validate_scope_unit_id("__tenant__", allow_reserved=True) == "__tenant__"

    @pytest.mark.parametrize("value", ["franchisee-0042", "region_north", "_internal-01"])
    def test_valid_ids(self, value):
        assert validate_scope_unit_id(value) == value

    @pytest.mark.parametrize("value", ["Franchisee", "1region", "a", "with space", ""])
    def test_invalid_ids(self, value):
        with pytest.raises(ValueError, match="scope unit id format"):
            validate_scope_unit_id(value)
