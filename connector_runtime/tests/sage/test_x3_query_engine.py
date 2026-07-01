"""
Tests for X3QueryEngine.

Coverage:
  - Constructor rejects invalid endpoint → X3QueryBuildError
  - Constructor accepts valid uppercase endpoint names (BPCUSTOMER, SORDER, etc.)
  - FULL load → no filter in query body; query_parameters empty
  - INCREMENTAL load → filter contains placeholder markers (not real values)
  - INCREMENTAL load without watermark_field → X3QueryBuildError
  - _x3_odata discriminant always present in query_text
  - "endpoint" in query_text matches constructor argument
  - "select" is comma-separated list of field names
  - "orderby" present in query_text
  - Invalid field name → X3QueryBuildError (injection prevention)
  - Empty FieldContract → X3QueryBuildError
  - watermark_field validated against safe pattern
  - bind_parameters: ISO-8601 lower bound substituted into filter
  - bind_parameters: ISO-8601 upper bound substituted into filter
  - bind_parameters: invalid lower_bound (injection) → X3QueryBuildError
  - bind_parameters: invalid upper_bound (injection) → X3QueryBuildError
  - bind_parameters: no parameters (FULL load) → no changes to query body
  - bind_parameters: does not mutate original query_body (deep copy)
  - X3_PAGE_SIZE == 1000
  - Dot-notation field names accepted (e.g. XBPADR.BPADES_0)
  - Lowercase endpoint rejected → X3QueryBuildError
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from connector_runtime.adapters.sage.products.x3.x3_query_engine import (
    X3_PAGE_SIZE,
    X3QueryBuildError,
    X3QueryEngine,
    _LOWER_BOUND_PLACEHOLDER,
    _UPPER_BOUND_PLACEHOLDER,
    X3_ODATA_DISCRIMINANT,
)
from connector_runtime.interfaces.connector_interface import (
    FieldContract,
    FieldDescriptor,
)
from contracts.entity_configuration_contract import FieldMode, LoadType

_ENDPOINT = "BPCUSTOMER"
_SOURCE_ID = "sage"
_ENTITY_ID = "sage-x3-customer"
_LOWER = "2026-01-01T00:00:00Z"
_UPPER = "2026-07-01T00:00:00Z"
_WATERMARK_FIELD = "MODDAT_0"


def _make_field_contract(field_names: list[str] | None = None) -> FieldContract:
    names = field_names if field_names is not None else ["BPCNUM_0", "BPCNAM_0", "MODDAT_0", "CRY_0"]
    descriptors = tuple(
        FieldDescriptor(name=n, data_type="string", is_nullable=True, is_queryable=True)
        for n in names
    )
    return FieldContract(
        source_id=_SOURCE_ID,
        entity_id=_ENTITY_ID,
        fields=descriptors,
        discovery_timestamp=datetime.now(UTC),
        schema_fingerprint=FieldContract.compute_fingerprint(descriptors),
    )


def _make_engine(object_path: str = _ENDPOINT) -> X3QueryEngine:
    return X3QueryEngine(object_path=object_path)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_x3_page_size_is_1000(self) -> None:
        assert X3_PAGE_SIZE == 1_000

    def test_discriminant_key_value(self) -> None:
        assert X3_ODATA_DISCRIMINANT == "_x3_odata"


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestConstructorValidation:
    def test_valid_uppercase_endpoint_accepted(self) -> None:
        X3QueryEngine(object_path="BPCUSTOMER")  # should not raise

    def test_valid_short_endpoint_accepted(self) -> None:
        X3QueryEngine(object_path="AB")  # 2-char minimum

    def test_valid_endpoint_with_digits_accepted(self) -> None:
        X3QueryEngine(object_path="PITM2")

    def test_lowercase_endpoint_rejected(self) -> None:
        with pytest.raises(X3QueryBuildError, match="endpoint"):
            X3QueryEngine(object_path="bpcustomer")

    def test_mixed_case_endpoint_rejected(self) -> None:
        with pytest.raises(X3QueryBuildError):
            X3QueryEngine(object_path="BpCustomer")

    def test_endpoint_with_space_rejected(self) -> None:
        with pytest.raises(X3QueryBuildError):
            X3QueryEngine(object_path="BPC USTOMER")

    def test_empty_endpoint_rejected(self) -> None:
        with pytest.raises(X3QueryBuildError):
            X3QueryEngine(object_path="")

    def test_single_char_endpoint_rejected(self) -> None:
        with pytest.raises(X3QueryBuildError):
            X3QueryEngine(object_path="B")  # minimum is 2 chars


# ---------------------------------------------------------------------------
# Full load
# ---------------------------------------------------------------------------


class TestFullLoad:
    def test_full_load_has_no_filter(self) -> None:
        engine = _make_engine()
        contract = engine.build(
            field_contract=_make_field_contract(),
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=0,
        )
        body = json.loads(contract.query_text)
        assert body.get("filter") is None

    def test_full_load_query_parameters_empty(self) -> None:
        engine = _make_engine()
        contract = engine.build(
            field_contract=_make_field_contract(),
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=0,
        )
        assert contract.query_parameters == {} or not contract.query_parameters

    def test_full_load_discriminant_present(self) -> None:
        engine = _make_engine()
        contract = engine.build(
            field_contract=_make_field_contract(),
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=0,
        )
        body = json.loads(contract.query_text)
        assert body[X3_ODATA_DISCRIMINANT] is True

    def test_full_load_endpoint_in_query_body(self) -> None:
        engine = _make_engine()
        contract = engine.build(
            field_contract=_make_field_contract(),
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=0,
        )
        body = json.loads(contract.query_text)
        assert body["endpoint"] == _ENDPOINT

    def test_full_load_select_contains_fields(self) -> None:
        engine = _make_engine()
        contract = engine.build(
            field_contract=_make_field_contract(["BPCNUM_0", "BPCNAM_0"]),
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=0,
        )
        body = json.loads(contract.query_text)
        assert "BPCNUM_0" in body["select"]
        assert "BPCNAM_0" in body["select"]

    def test_full_load_orderby_present(self) -> None:
        engine = _make_engine()
        contract = engine.build(
            field_contract=_make_field_contract(),
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=0,
        )
        body = json.loads(contract.query_text)
        assert "orderby" in body


# ---------------------------------------------------------------------------
# Incremental load
# ---------------------------------------------------------------------------


class TestIncrementalLoad:
    def _build_incremental(self, watermark_field: str = _WATERMARK_FIELD) -> dict:
        engine = _make_engine()
        contract = engine.build(
            field_contract=_make_field_contract(),
            load_type=LoadType.INCREMENTAL,
            watermark_field=watermark_field,
            watermark_lower=_LOWER,
            watermark_upper=_UPPER,
            extraction_window_days=1,
        )
        return json.loads(contract.query_text)

    def test_incremental_filter_has_lower_placeholder(self) -> None:
        body = self._build_incremental()
        assert _LOWER_BOUND_PLACEHOLDER in body["filter"]

    def test_incremental_filter_has_upper_placeholder(self) -> None:
        body = self._build_incremental()
        assert _UPPER_BOUND_PLACEHOLDER in body["filter"]

    def test_incremental_filter_no_real_values(self) -> None:
        body = self._build_incremental()
        assert _LOWER not in body["filter"]
        assert _UPPER not in body["filter"]

    def test_incremental_filter_contains_watermark_field(self) -> None:
        body = self._build_incremental()
        assert _WATERMARK_FIELD in body["filter"]

    def test_incremental_query_parameters_contain_bounds(self) -> None:
        engine = _make_engine()
        contract = engine.build(
            field_contract=_make_field_contract(),
            load_type=LoadType.INCREMENTAL,
            watermark_field=_WATERMARK_FIELD,
            watermark_lower=_LOWER,
            watermark_upper=_UPPER,
            extraction_window_days=1,
        )
        assert contract.query_parameters["lower_bound"] == _LOWER
        assert contract.query_parameters["upper_bound"] == _UPPER

    def test_incremental_without_watermark_field_raises(self) -> None:
        engine = _make_engine()
        with pytest.raises(X3QueryBuildError, match="watermark_field"):
            engine.build(
                field_contract=_make_field_contract(),
                load_type=LoadType.INCREMENTAL,
                watermark_field=None,
                watermark_lower=_LOWER,
                watermark_upper=_UPPER,
                extraction_window_days=1,
            )

    def test_incremental_invalid_watermark_field_raises(self) -> None:
        engine = _make_engine()
        with pytest.raises(X3QueryBuildError):
            engine.build(
                field_contract=_make_field_contract(),
                load_type=LoadType.INCREMENTAL,
                watermark_field="invalid; DROP TABLE",
                watermark_lower=_LOWER,
                watermark_upper=_UPPER,
                extraction_window_days=1,
            )


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------


class TestFieldValidation:
    def test_invalid_field_name_raises(self) -> None:
        engine = _make_engine()
        with pytest.raises(X3QueryBuildError, match="Field name"):
            engine.build(
                field_contract=_make_field_contract(["VALID_0", "invalid_field"]),
                load_type=LoadType.FULL,
                watermark_field=None,
                watermark_lower=None,
                watermark_upper=None,
                extraction_window_days=0,
            )

    def test_empty_field_contract_raises(self) -> None:
        engine = _make_engine()
        empty_contract = _make_field_contract([])
        with pytest.raises(X3QueryBuildError, match="no queryable fields"):
            engine.build(
                field_contract=empty_contract,
                load_type=LoadType.FULL,
                watermark_field=None,
                watermark_lower=None,
                watermark_upper=None,
                extraction_window_days=0,
            )

    def test_dot_notation_field_accepted(self) -> None:
        engine = _make_engine()
        engine.build(
            field_contract=_make_field_contract(["BPCNUM_0", "XBPADR.BPADES_0"]),
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=0,
        )  # should not raise

    def test_lowercase_field_rejected(self) -> None:
        engine = _make_engine()
        with pytest.raises(X3QueryBuildError, match="Field name"):
            engine.build(
                field_contract=_make_field_contract(["bpcnum_0"]),
                load_type=LoadType.FULL,
                watermark_field=None,
                watermark_lower=None,
                watermark_upper=None,
                extraction_window_days=0,
            )


# ---------------------------------------------------------------------------
# bind_parameters
# ---------------------------------------------------------------------------


class TestBindParameters:
    def _build_incremental_body(self) -> dict:
        engine = _make_engine()
        contract = engine.build(
            field_contract=_make_field_contract(),
            load_type=LoadType.INCREMENTAL,
            watermark_field=_WATERMARK_FIELD,
            watermark_lower=_LOWER,
            watermark_upper=_UPPER,
            extraction_window_days=1,
        )
        return json.loads(contract.query_text)

    def test_bind_substitutes_lower_bound(self) -> None:
        body = self._build_incremental_body()
        bound = X3QueryEngine.bind_parameters(body, {"lower_bound": _LOWER, "upper_bound": _UPPER})
        assert _LOWER in bound["filter"]

    def test_bind_substitutes_upper_bound(self) -> None:
        body = self._build_incremental_body()
        bound = X3QueryEngine.bind_parameters(body, {"lower_bound": _LOWER, "upper_bound": _UPPER})
        assert _UPPER in bound["filter"]

    def test_bind_removes_placeholders(self) -> None:
        body = self._build_incremental_body()
        bound = X3QueryEngine.bind_parameters(body, {"lower_bound": _LOWER, "upper_bound": _UPPER})
        assert _LOWER_BOUND_PLACEHOLDER not in bound["filter"]
        assert _UPPER_BOUND_PLACEHOLDER not in bound["filter"]

    def test_bind_does_not_mutate_original(self) -> None:
        body = self._build_incremental_body()
        original_filter = body["filter"]
        X3QueryEngine.bind_parameters(body, {"lower_bound": _LOWER, "upper_bound": _UPPER})
        assert body["filter"] == original_filter  # original unchanged

    def test_bind_invalid_lower_bound_raises(self) -> None:
        body = self._build_incremental_body()
        with pytest.raises(X3QueryBuildError, match="lower_bound"):
            X3QueryEngine.bind_parameters(body, {"lower_bound": "'; DROP TABLE --", "upper_bound": _UPPER})

    def test_bind_invalid_upper_bound_raises(self) -> None:
        body = self._build_incremental_body()
        with pytest.raises(X3QueryBuildError, match="upper_bound"):
            X3QueryEngine.bind_parameters(body, {"lower_bound": _LOWER, "upper_bound": "not-a-date"})

    def test_bind_empty_parameters_full_load_no_change(self) -> None:
        engine = _make_engine()
        contract = engine.build(
            field_contract=_make_field_contract(),
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=0,
        )
        body = json.loads(contract.query_text)
        bound = X3QueryEngine.bind_parameters(body, {})
        assert bound["filter"] is None
