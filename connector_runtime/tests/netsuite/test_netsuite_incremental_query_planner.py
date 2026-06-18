"""
Tests for NetSuiteIncrementalQueryPlanner.

Coverage:
  - FULL load — no WHERE clause, no query_parameters
  - INCREMENTAL load — WHERE clause with :lower_bound/:upper_bound, parameters in dict
  - Watermark values in parameters NOT in query_text (OWASP A03)
  - Validation: invalid record type, field name, watermark_field
  - Missing watermark_field for INCREMENTAL raises error
  - Empty FieldContract raises error
  - bind_parameters() validates ISO-8601 values and substitutes placeholders
  - bind_parameters() rejects non-ISO8601 values (injection prevention)
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from connector_runtime.adapters.netsuite.netsuite_incremental_query_planner import (
    NetSuiteIncrementalQueryPlanner,
    NetSuiteIncrementalQueryPlannerError,
)
from connector_runtime.interfaces.connector_interface import (
    FieldContract,
    FieldDescriptor,
)
from contracts.entity_configuration_contract import LoadType

_LOWER = "2026-01-01T00:00:00Z"
_UPPER = "2026-06-12T00:00:00Z"


def _make_field_contract(field_names: list[str] | None = None) -> FieldContract:
    names = field_names or ["id", "companyname", "lastmodifieddate"]
    descriptors = tuple(
        FieldDescriptor(
            name=n,
            data_type="STRING",
            is_nullable=True,
            is_queryable=True,
        )
        for n in names
    )
    return FieldContract(
        source_id="netsuite",
        entity_id="netsuite-customer",
        fields=descriptors,
        discovery_timestamp=datetime.now(UTC),
        schema_fingerprint=FieldContract.compute_fingerprint(descriptors),
    )


class TestQueryConstruction:
    def test_full_load_has_no_where_clause(self) -> None:
        planner = NetSuiteIncrementalQueryPlanner(record_type="customer")
        contract = planner.build(
            field_contract=_make_field_contract(),
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=0,
        )
        assert "WHERE" not in contract.query_text.upper()
        assert contract.query_parameters == {}
        assert contract.watermark_field is None

    def test_incremental_load_adds_where_clause(self) -> None:
        planner = NetSuiteIncrementalQueryPlanner(record_type="customer")
        contract = planner.build(
            field_contract=_make_field_contract(),
            load_type=LoadType.INCREMENTAL,
            watermark_field="lastmodifieddate",
            watermark_lower=_LOWER,
            watermark_upper=_UPPER,
            extraction_window_days=7,
        )
        assert "WHERE" in contract.query_text.upper()
        assert ":lower_bound" in contract.query_text
        assert ":upper_bound" in contract.query_text
        assert contract.watermark_field == "lastmodifieddate"

    def test_watermark_values_in_parameters_not_in_query_text(self) -> None:
        """OWASP A03: watermark values must not be in query_text."""
        planner = NetSuiteIncrementalQueryPlanner(record_type="customer")
        contract = planner.build(
            field_contract=_make_field_contract(),
            load_type=LoadType.INCREMENTAL,
            watermark_field="lastmodifieddate",
            watermark_lower=_LOWER,
            watermark_upper=_UPPER,
            extraction_window_days=7,
        )
        assert _LOWER not in contract.query_text
        assert _UPPER not in contract.query_text
        assert contract.query_parameters["lower_bound"] == _LOWER
        assert contract.query_parameters["upper_bound"] == _UPPER

    def test_all_fields_appear_in_select(self) -> None:
        planner = NetSuiteIncrementalQueryPlanner(record_type="customer")
        fc = _make_field_contract(["id", "email", "phone"])
        contract = planner.build(
            field_contract=fc,
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=0,
        )
        assert "id" in contract.query_text
        assert "email" in contract.query_text
        assert "phone" in contract.query_text
        assert "customer" in contract.query_text


class TestInputValidation:
    def test_invalid_record_type_raises(self) -> None:
        with pytest.raises(NetSuiteIncrementalQueryPlannerError, match="record_type"):
            NetSuiteIncrementalQueryPlanner(record_type="bad record type!")

    def test_incremental_without_watermark_field_raises(self) -> None:
        planner = NetSuiteIncrementalQueryPlanner(record_type="customer")
        with pytest.raises(NetSuiteIncrementalQueryPlannerError, match="watermark_field"):
            planner.build(
                field_contract=_make_field_contract(),
                load_type=LoadType.INCREMENTAL,
                watermark_field=None,
                watermark_lower=_LOWER,
                watermark_upper=_UPPER,
                extraction_window_days=7,
            )

    def test_empty_field_contract_raises(self) -> None:
        fc = FieldContract(
            source_id="netsuite",
            entity_id="netsuite-customer",
            fields=(),
            discovery_timestamp=datetime.now(UTC),
            schema_fingerprint="",
        )
        planner = NetSuiteIncrementalQueryPlanner(record_type="customer")
        with pytest.raises(NetSuiteIncrementalQueryPlannerError, match="no queryable fields"):
            planner.build(
                field_contract=fc,
                load_type=LoadType.FULL,
                watermark_field=None,
                watermark_lower=None,
                watermark_upper=None,
                extraction_window_days=0,
            )

    def test_invalid_watermark_field_name_raises(self) -> None:
        planner = NetSuiteIncrementalQueryPlanner(record_type="customer")
        with pytest.raises(NetSuiteIncrementalQueryPlannerError, match="watermark_field"):
            planner.build(
                field_contract=_make_field_contract(),
                load_type=LoadType.INCREMENTAL,
                watermark_field="invalid field name!",
                watermark_lower=_LOWER,
                watermark_upper=_UPPER,
                extraction_window_days=7,
            )


class TestBindParameters:
    def test_bind_replaces_named_placeholders(self) -> None:
        bound = NetSuiteIncrementalQueryPlanner.bind_parameters(
            "SELECT id FROM customer WHERE lastmodifieddate >= :lower_bound"
            " AND lastmodifieddate < :upper_bound",
            {"lower_bound": _LOWER, "upper_bound": _UPPER},
        )
        assert _LOWER in bound
        assert _UPPER in bound
        assert ":lower_bound" not in bound
        assert ":upper_bound" not in bound

    def test_bind_rejects_non_iso8601_value(self) -> None:
        """OWASP A03: non-ISO8601 value must be rejected to prevent SQL injection."""
        with pytest.raises(NetSuiteIncrementalQueryPlannerError, match="ISO-8601"):
            NetSuiteIncrementalQueryPlanner.bind_parameters(
                "SELECT id FROM t WHERE modified >= :lower_bound",
                {"lower_bound": "'; DROP TABLE customer; --"},
            )

    def test_bind_empty_parameters_returns_unchanged_query(self) -> None:
        query = "SELECT id FROM customer"
        bound = NetSuiteIncrementalQueryPlanner.bind_parameters(query, {})
        assert bound == query
