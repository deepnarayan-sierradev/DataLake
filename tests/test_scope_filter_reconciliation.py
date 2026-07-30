"""
Tests for the scope-filter reconciliation (F14, DL-SEC-11).

Athena row-level isolation is `aws_lakeformation_data_cells_filter` — the only Lake Formation
construct that filters rows, so the mechanism is correct. The lifecycle is not: scope units are
runtime data in `datalake-scope-units-dev`, published when a franchisee is onboarded, while the
filters that
enforce their boundary are static Terraform. A unit could therefore exist, own rows, and have no
filter — readable by any principal holding the tenant tag, which is the wildcard grant the filters
replaced, and with nothing reporting it.

The security-relevant direction is units-without-filters, so that is what the exit code and the
metric key on.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from generate_scope_unit_filters import build_fragment, detect_drift

_UNITS = [
    {"tenant_code": "evive", "scope_unit_id": "franchisee-0001"},
    {"tenant_code": "evive", "scope_unit_id": "franchisee-0002"},
]
_TABLES = ("evive_company", "evive_sales_order")


class TestFragmentGeneration:
    def test_one_filter_per_unit_and_table(self) -> None:
        fragment = build_fragment(_UNITS, _TABLES, "123456789012", {})
        assert len(fragment["scope_unit_row_filters"]) == 4

    def test_a_filter_carries_its_units_id_for_the_row_expression(self) -> None:
        fragment = build_fragment(_UNITS, _TABLES, "123456789012", {})
        entry = fragment["scope_unit_row_filters"]["evive:franchisee-0001:evive_company"]
        assert entry["scope_unit_id"] == "franchisee-0001"
        assert entry["table_name"] == "evive_company"

    def test_a_unit_with_no_known_principal_gets_no_grant(self) -> None:
        """
        A grant to a guessed principal ARN is worse than no grant — the reason the Terraform
        variables default to empty in the first place. The filter is still emitted, so the boundary
        exists the moment a real principal is supplied.
        """
        fragment = build_fragment(_UNITS, _TABLES, "123456789012", {})
        assert fragment["scope_unit_row_filters"]
        assert fragment["scope_unit_grants"] == {}

    def test_a_known_principal_produces_a_grant_per_filter(self) -> None:
        fragment = build_fragment(
            _UNITS, _TABLES, "123456789012", {"franchisee-0001": "arn:aws:iam::1:role/f1"}
        )
        grants = fragment["scope_unit_grants"]
        assert len(grants) == 2  # one per table, for the one unit with a principal
        assert all(g["principal_arn"] == "arn:aws:iam::1:role/f1" for g in grants.values())


class TestDriftDetection:
    def test_a_registered_unit_with_no_filter_is_unenforced(self) -> None:
        drift = detect_drift({"evive:f-1:t", "evive:f-2:t"}, {"evive:f-1:t"})
        assert drift["unenforced_units"] == ["evive:f-2:t"]

    def test_a_filter_with_no_unit_is_stale(self) -> None:
        drift = detect_drift({"evive:f-1:t"}, {"evive:f-1:t", "evive:gone:t"})
        assert drift["stale_filters"] == ["evive:gone:t"]

    def test_no_drift_when_they_agree(self) -> None:
        drift = detect_drift({"evive:f-1:t"}, {"evive:f-1:t"})
        assert drift == {"unenforced_units": [], "stale_filters": []}

    def test_generated_keys_round_trip_through_drift_detection(self) -> None:
        fragment = build_fragment(_UNITS, _TABLES, "123456789012", {})
        keys = set(fragment["scope_unit_row_filters"])
        assert detect_drift(keys, keys) == {"unenforced_units": [], "stale_filters": []}


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
