"""Scope attribution stamping tests (DL-SCOPE-09, DL-12 design decision D2)."""

from __future__ import annotations

from tenancy.scope_attribution import ScopeAttributor
from tenancy.scope_contract import (
    IMPLICIT_SCOPE_UNIT_ID,
    AttributionMode,
    PartitionKind,
    PartitionModel,
    TenantPartitionProfile,
)
from tenancy.scope_predicate import SCOPE_UNIT_COLUMN
from tenancy.source_connection import ConnectionOwnerType, ConnectionState, SourceConnection

_SINGLE = TenantPartitionProfile(tenant_code="demo")
_PARTITIONED = TenantPartitionProfile(
    tenant_code="evive",
    partition_model=PartitionModel.PARTITIONED,
    partition_kind=PartitionKind.FRANCHISE,
)


def _connection(**overrides) -> SourceConnection:
    base = {
        "tenant_code": "evive",
        "connection_id": "hubspot-grasons",
        "source_id": "hubspot",
        "display_name": "Grasons HubSpot",
        "state": ConnectionState.ACTIVE,
    }
    return SourceConnection(**{**base, **overrides})


class TestSingleTenant:
    def test_every_row_gets_the_implicit_unit(self):
        attributor = ScopeAttributor(_connection(), _SINGLE)
        stamped = attributor.stamp({"id": "1"})
        assert stamped[SCOPE_UNIT_COLUMN] == IMPLICIT_SCOPE_UNIT_ID
        assert attributor.outcome.unattributed_rows == 0


class TestProvenanceDerived:
    def test_owner_is_stamped_regardless_of_record_content(self):
        connection = _connection(
            owner_type=ConnectionOwnerType.SCOPE_UNIT, owner_id="franchisee-0042"
        )
        attributor = ScopeAttributor(connection, _PARTITIONED)
        stamped = attributor.stamp({"franchise_code": "someone-else"})
        assert stamped[SCOPE_UNIT_COLUMN] == "franchisee-0042"


class TestFieldDerived:
    def _attributor(self, known=None) -> ScopeAttributor:
        connection = _connection(
            attribution_mode=AttributionMode.FIELD_DERIVED,
            scope_attribution_field="franchise_code",
        )
        return ScopeAttributor(connection, _PARTITIONED, known_scope_unit_ids=known)

    def test_mapped_value_is_used(self):
        attributor = self._attributor()
        assert (
            attributor.stamp({"franchise_code": "Franchisee-0042"})[SCOPE_UNIT_COLUMN]
            == "franchisee-0042"
        )

    def test_missing_value_is_unattributable_not_public(self):
        attributor = self._attributor()
        assert attributor.stamp({"franchise_code": None})[SCOPE_UNIT_COLUMN] is None
        assert attributor.outcome.unattributed_rows == 1

    def test_malformed_value_is_unattributable(self):
        attributor = self._attributor()
        assert attributor.stamp({"franchise_code": "'; DROP TABLE x --"})[SCOPE_UNIT_COLUMN] is None

    def test_unknown_unit_is_unattributable(self):
        attributor = self._attributor(known=frozenset({"franchisee-0001"}))
        assert attributor.stamp({"franchise_code": "franchisee-9999"})[SCOPE_UNIT_COLUMN] is None

    def test_unattributed_rate_threshold(self):
        attributor = self._attributor()
        for _ in range(9):
            attributor.stamp({"franchise_code": "franchisee-0001"})
        attributor.stamp({"franchise_code": None})
        assert attributor.outcome.unattributed_rate_pct == 10.0
        assert attributor.outcome.exceeds(threshold_pct=5.0) is True
        assert attributor.outcome.exceeds(threshold_pct=20.0) is False
        attributor.log_outcome("hubspot-company")


class TestStreaming:
    def test_stamp_all_is_lazy(self):
        attributor = ScopeAttributor(_connection(), _SINGLE)
        stream = attributor.stamp_all({"id": str(i)} for i in range(3))
        assert attributor.outcome.total_rows == 0
        assert len(list(stream)) == 3
        assert attributor.outcome.total_rows == 3

    def test_zero_rows_reports_zero_rate(self):
        attributor = ScopeAttributor(_connection(), _SINGLE)
        assert attributor.outcome.unattributed_rate_pct == 0.0
