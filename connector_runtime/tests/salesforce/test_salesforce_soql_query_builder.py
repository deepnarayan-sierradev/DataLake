"""
Tests for SalesforceSoqlQueryBuilder.

Covers:
  - Incremental query contains correct WHERE clause with :param placeholders
  - Watermark values in query_parameters, never interpolated into query_text
  - Full load query has no WHERE clause
  - Field list built from FieldContract (no hardcoded fields)
  - Invalid object_name rejected at construction (injection prevention)
  - Invalid watermark_field name rejected (injection prevention)
  - Missing watermark inputs for incremental load raises error
  - Empty field set raises error
  - Field name validation rejects invalid characters
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from connector_runtime.interfaces.connector_interface import FieldContract, FieldDescriptor
from connector_runtime.query_builders.salesforce_soql_query_builder import (
    SalesforceSoqlQueryBuilder,
    SalesforceSoqlQueryBuilderError,
)
from contracts.entity_configuration_contract import LoadType

_SOURCE_ID = "salesforce"
_ENTITY_ID = "salesforce-account"
_LOWER = "2026-06-01T00:00:00+00:00"
_UPPER = "2026-06-02T00:00:00+00:00"


def _make_contract(*names: str) -> FieldContract:
    fields = tuple(
        FieldDescriptor(name=n, data_type="string", is_nullable=True, is_queryable=True)
        for n in names
    )
    return FieldContract(
        source_id=_SOURCE_ID,
        entity_id=_ENTITY_ID,
        fields=fields,
        discovery_timestamp=datetime.now(UTC),
        schema_fingerprint=FieldContract.compute_fingerprint(fields),
    )


# ---------------------------------------------------------------------------
# Query construction
# ---------------------------------------------------------------------------


class TestSoqlQueryConstruction:
    def test_incremental_query_has_watermark_where_clause(self) -> None:
        builder = SalesforceSoqlQueryBuilder("Account")
        contract = _make_contract("Id", "Name", "SystemModstamp")
        query = builder.build(
            field_contract=contract,
            load_type=LoadType.INCREMENTAL,
            watermark_field="SystemModstamp",
            watermark_lower=_LOWER,
            watermark_upper=_UPPER,
            extraction_window_days=1,
        )
        assert "WHERE SystemModstamp >= :lower_bound" in query.query_text
        assert "AND SystemModstamp < :upper_bound" in query.query_text

    def test_watermark_values_in_parameters_not_in_query_text(self) -> None:
        """Regression: watermark dates must NEVER be interpolated into query_text."""
        builder = SalesforceSoqlQueryBuilder("Account")
        contract = _make_contract("Id", "SystemModstamp")
        query = builder.build(
            field_contract=contract,
            load_type=LoadType.INCREMENTAL,
            watermark_field="SystemModstamp",
            watermark_lower=_LOWER,
            watermark_upper=_UPPER,
            extraction_window_days=1,
        )
        assert _LOWER not in query.query_text
        assert _UPPER not in query.query_text
        assert query.query_parameters["lower_bound"] == _LOWER
        assert query.query_parameters["upper_bound"] == _UPPER

    def test_full_load_has_no_where_clause(self) -> None:
        builder = SalesforceSoqlQueryBuilder("Account")
        contract = _make_contract("Id", "Name")
        query = builder.build(
            field_contract=contract,
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=1,
        )
        assert "WHERE" not in query.query_text
        assert query.query_parameters == {}

    def test_field_list_derived_from_field_contract(self) -> None:
        builder = SalesforceSoqlQueryBuilder("Account")
        contract = _make_contract("Id", "Name", "Phone")
        query = builder.build(
            field_contract=contract,
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=1,
        )
        assert "Id, Name, Phone" in query.query_text

    def test_object_name_in_from_clause(self) -> None:
        builder = SalesforceSoqlQueryBuilder("Opportunity")
        contract = _make_contract("Id")
        query = builder.build(
            field_contract=contract,
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=1,
        )
        assert "FROM Opportunity" in query.query_text

    def test_query_contract_source_and_entity_ids(self) -> None:
        builder = SalesforceSoqlQueryBuilder("Account")
        contract = _make_contract("Id")
        query = builder.build(
            field_contract=contract,
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=1,
        )
        assert query.source_id == _SOURCE_ID
        assert query.entity_id == _ENTITY_ID


# ---------------------------------------------------------------------------
# Input validation (injection prevention)
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_invalid_object_name_rejected(self) -> None:
        with pytest.raises(SalesforceSoqlQueryBuilderError, match="not permitted"):
            SalesforceSoqlQueryBuilder("Account'; DROP TABLE")

    def test_object_name_with_spaces_rejected(self) -> None:
        with pytest.raises(SalesforceSoqlQueryBuilderError):
            SalesforceSoqlQueryBuilder("Account Name")

    def test_invalid_watermark_field_name_rejected(self) -> None:
        builder = SalesforceSoqlQueryBuilder("Account")
        contract = _make_contract("Id")
        with pytest.raises(SalesforceSoqlQueryBuilderError, match="not permitted"):
            builder.build(
                field_contract=contract,
                load_type=LoadType.INCREMENTAL,
                watermark_field="SystemModstamp; DELETE FROM",
                watermark_lower=_LOWER,
                watermark_upper=_UPPER,
                extraction_window_days=1,
            )

    def test_missing_watermark_lower_for_incremental_raises(self) -> None:
        builder = SalesforceSoqlQueryBuilder("Account")
        contract = _make_contract("Id")
        with pytest.raises(SalesforceSoqlQueryBuilderError, match="required"):
            builder.build(
                field_contract=contract,
                load_type=LoadType.INCREMENTAL,
                watermark_field="SystemModstamp",
                watermark_lower=None,
                watermark_upper=_UPPER,
                extraction_window_days=1,
            )

    def test_missing_watermark_field_for_incremental_raises(self) -> None:
        builder = SalesforceSoqlQueryBuilder("Account")
        contract = _make_contract("Id")
        with pytest.raises(SalesforceSoqlQueryBuilderError, match="required"):
            builder.build(
                field_contract=contract,
                load_type=LoadType.INCREMENTAL,
                watermark_field=None,
                watermark_lower=_LOWER,
                watermark_upper=_UPPER,
                extraction_window_days=1,
            )

    def test_empty_field_contract_raises(self) -> None:
        builder = SalesforceSoqlQueryBuilder("Account")
        contract = _make_contract()  # zero fields
        with pytest.raises(SalesforceSoqlQueryBuilderError, match="No valid fields"):
            builder.build(
                field_contract=contract,
                load_type=LoadType.FULL,
                watermark_field=None,
                watermark_lower=None,
                watermark_upper=None,
                extraction_window_days=1,
            )
