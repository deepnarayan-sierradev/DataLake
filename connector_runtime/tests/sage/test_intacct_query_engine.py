"""
Tests for IntacctQueryEngine.

Coverage:
  - Constructor rejects invalid object_path → SageQueryBuildError
  - FULL load → no filters in query body
  - FULL load → query_parameters is empty dict
  - INCREMENTAL load → filters contain placeholder markers (not real values)
  - INCREMENTAL load without watermark_field → SageQueryBuildError
  - "key" always added to fields (stable pagination anchor)
  - "key" not duplicated if already in the FieldContract fields
  - orderBy: [{"key": "asc"}] always present
  - object path in query body matches constructor argument
  - Invalid field name → SageQueryBuildError (injection prevention)
  - Empty FieldContract (no fields) → SageQueryBuildError
  - watermark_field itself validated against safe pattern
  - QueryContract watermark_lower / watermark_upper preserved in query_parameters
  - bind_parameters: ISO-8601 lower bound substituted into filter
  - bind_parameters: ISO-8601 upper bound substituted into filter
  - bind_parameters: invalid lower_bound (injection) → SageQueryBuildError
  - bind_parameters: invalid upper_bound (injection) → SageQueryBuildError
  - bind_parameters: no parameters (FULL load) → no changes to query body
  - bind_parameters: partial parameters (only lower) → handled gracefully
  - PAGE_SIZE == 4000
  - Dot-notation field names accepted (e.g. "auditInfo.modifiedAt")
  - Custom field names with nsp:: prefix accepted
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from connector_runtime.adapters.sage.products.intacct.intacct_query_engine import (
    PAGE_SIZE,
    IntacctQueryEngine,
    SageQueryBuildError,
    _LOWER_BOUND_PLACEHOLDER,
    _UPPER_BOUND_PLACEHOLDER,
)
from connector_runtime.interfaces.connector_interface import (
    FieldContract,
    FieldDescriptor,
)
from contracts.entity_configuration_contract import FieldMode, LoadType

_OBJECT_PATH = "accounts-receivable/customer"
_SOURCE_ID = "sage"
_ENTITY_ID = "sage-intacct-customer"
_LOWER = "2026-01-01T00:00:00Z"
_UPPER = "2026-07-01T00:00:00Z"


def _make_field_contract(field_names: list[str] | None = None) -> FieldContract:
    names = field_names or ["key", "id", "name", "status"]
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


def _make_engine(object_path: str = _OBJECT_PATH) -> IntacctQueryEngine:
    return IntacctQueryEngine(object_path=object_path)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_page_size_is_4000(self) -> None:
        assert PAGE_SIZE == 4_000


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestConstructorValidation:
    def test_invalid_object_path_raises(self) -> None:
        with pytest.raises(SageQueryBuildError, match="object_path"):
            IntacctQueryEngine(object_path="INVALID_PATH_NO_SLASH")

    def test_path_traversal_raises(self) -> None:
        with pytest.raises(SageQueryBuildError):
            IntacctQueryEngine(object_path="../../../etc/passwd")

    def test_valid_object_path_accepted(self) -> None:
        engine = IntacctQueryEngine(object_path="order-entry/document")
        assert engine is not None

    def test_extended_path_with_document_type(self) -> None:
        """object_path with :: suffix (e.g. order-entry/document::Contract Invoice)."""
        engine = IntacctQueryEngine(object_path="order-entry/document::Contract Invoice")
        assert engine is not None


# ---------------------------------------------------------------------------
# FULL load query building
# ---------------------------------------------------------------------------


class TestFullLoad:
    def test_full_load_produces_no_filters(self) -> None:
        engine = _make_engine()
        fc = _make_field_contract()
        contract = engine.build(
            field_contract=fc,
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=0,
        )
        body = json.loads(contract.query_text)
        assert "filters" not in body

    def test_full_load_query_parameters_empty(self) -> None:
        engine = _make_engine()
        fc = _make_field_contract()
        contract = engine.build(
            field_contract=fc,
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=0,
        )
        assert contract.query_parameters == {}

    def test_full_load_object_path_in_body(self) -> None:
        engine = _make_engine()
        fc = _make_field_contract()
        contract = engine.build(
            field_contract=fc,
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=0,
        )
        body = json.loads(contract.query_text)
        assert body["object"] == _OBJECT_PATH

    def test_full_load_order_by_always_present(self) -> None:
        engine = _make_engine()
        fc = _make_field_contract()
        contract = engine.build(
            field_contract=fc,
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=0,
        )
        body = json.loads(contract.query_text)
        assert body["orderBy"] == [{"key": "asc"}]

    def test_full_load_watermark_field_is_none_in_contract(self) -> None:
        engine = _make_engine()
        fc = _make_field_contract()
        contract = engine.build(
            field_contract=fc,
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=0,
        )
        assert contract.watermark_field is None


# ---------------------------------------------------------------------------
# INCREMENTAL load query building
# ---------------------------------------------------------------------------


class TestIncrementalLoad:
    def test_incremental_load_has_filters(self) -> None:
        engine = _make_engine()
        fc = _make_field_contract(["key", "id", "auditInfo.modifiedAt"])
        contract = engine.build(
            field_contract=fc,
            load_type=LoadType.INCREMENTAL,
            watermark_field="auditInfo.modifiedAt",
            watermark_lower=_LOWER,
            watermark_upper=_UPPER,
            extraction_window_days=30,
        )
        body = json.loads(contract.query_text)
        assert "filters" in body
        assert len(body["filters"]) == 2

    def test_incremental_filters_use_placeholder_markers(self) -> None:
        engine = _make_engine()
        fc = _make_field_contract(["key", "id", "auditInfo.modifiedAt"])
        contract = engine.build(
            field_contract=fc,
            load_type=LoadType.INCREMENTAL,
            watermark_field="auditInfo.modifiedAt",
            watermark_lower=_LOWER,
            watermark_upper=_UPPER,
            extraction_window_days=30,
        )
        body = json.loads(contract.query_text)
        # Actual watermark values must NOT be in the query_text — only placeholders.
        assert _LOWER not in contract.query_text
        assert _UPPER not in contract.query_text
        assert _LOWER_BOUND_PLACEHOLDER in contract.query_text
        assert _UPPER_BOUND_PLACEHOLDER in contract.query_text

    def test_incremental_watermark_values_in_parameters(self) -> None:
        engine = _make_engine()
        fc = _make_field_contract(["key", "id", "auditInfo.modifiedAt"])
        contract = engine.build(
            field_contract=fc,
            load_type=LoadType.INCREMENTAL,
            watermark_field="auditInfo.modifiedAt",
            watermark_lower=_LOWER,
            watermark_upper=_UPPER,
            extraction_window_days=30,
        )
        assert contract.query_parameters["lower_bound"] == _LOWER
        assert contract.query_parameters["upper_bound"] == _UPPER

    def test_incremental_without_watermark_field_raises(self) -> None:
        engine = _make_engine()
        fc = _make_field_contract()
        with pytest.raises(SageQueryBuildError, match="watermark_field is required"):
            engine.build(
                field_contract=fc,
                load_type=LoadType.INCREMENTAL,
                watermark_field=None,
                watermark_lower=_LOWER,
                watermark_upper=_UPPER,
                extraction_window_days=30,
            )

    def test_invalid_watermark_field_name_raises(self) -> None:
        engine = _make_engine()
        fc = _make_field_contract()
        with pytest.raises(SageQueryBuildError, match="watermark_field"):
            engine.build(
                field_contract=fc,
                load_type=LoadType.INCREMENTAL,
                watermark_field="'; DROP TABLE users; --",
                watermark_lower=_LOWER,
                watermark_upper=_UPPER,
                extraction_window_days=30,
            )


# ---------------------------------------------------------------------------
# Field name validation
# ---------------------------------------------------------------------------


class TestFieldNameValidation:
    def test_simple_field_names_accepted(self) -> None:
        engine = _make_engine()
        fc = _make_field_contract(["key", "id", "name", "status"])
        contract = engine.build(
            field_contract=fc,
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=0,
        )
        body = json.loads(contract.query_text)
        assert "id" in body["fields"]

    def test_dot_notation_field_names_accepted(self) -> None:
        engine = _make_engine()
        fc = _make_field_contract(["key", "auditInfo.modifiedAt", "primaryContact.name"])
        # Should not raise
        contract = engine.build(
            field_contract=fc,
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=0,
        )
        body = json.loads(contract.query_text)
        assert "auditInfo.modifiedAt" in body["fields"]

    def test_custom_nsp_field_names_accepted(self) -> None:
        engine = _make_engine()
        # Custom field names with double-colon prefix (IntacctQueryEngine validates uppercase after ::)
        descriptors = tuple(
            FieldDescriptor(name=n, data_type="string", is_nullable=True, is_queryable=True)
            for n in ["key", "id", "nsp::CUSTOM_FIELD"]
        )
        fc = FieldContract(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            fields=descriptors,
            discovery_timestamp=datetime.now(UTC),
            schema_fingerprint=FieldContract.compute_fingerprint(descriptors),
        )
        contract = engine.build(
            field_contract=fc,
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=0,
        )
        body = json.loads(contract.query_text)
        assert "nsp::CUSTOM_FIELD" in body["fields"]

    def test_invalid_field_name_raises(self) -> None:
        engine = _make_engine()
        descriptors = (
            FieldDescriptor(
                name="'; DROP TABLE customers; --",
                data_type="string",
                is_nullable=True,
                is_queryable=True,
            ),
        )
        fc = FieldContract(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            fields=descriptors,
            discovery_timestamp=datetime.now(UTC),
            schema_fingerprint=FieldContract.compute_fingerprint(descriptors),
        )
        with pytest.raises(SageQueryBuildError, match="injection attempt"):
            engine.build(
                field_contract=fc,
                load_type=LoadType.FULL,
                watermark_field=None,
                watermark_lower=None,
                watermark_upper=None,
                extraction_window_days=0,
            )

    def test_empty_field_contract_raises(self) -> None:
        engine = _make_engine()
        fc = FieldContract(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            fields=(),
            discovery_timestamp=datetime.now(UTC),
            schema_fingerprint="",
        )
        with pytest.raises(SageQueryBuildError, match="no queryable fields"):
            engine.build(
                field_contract=fc,
                load_type=LoadType.FULL,
                watermark_field=None,
                watermark_lower=None,
                watermark_upper=None,
                extraction_window_days=0,
            )

    def test_key_field_added_when_not_present(self) -> None:
        engine = _make_engine()
        # "key" not in the supplied field list.
        fc = _make_field_contract(["id", "name"])
        contract = engine.build(
            field_contract=fc,
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=0,
        )
        body = json.loads(contract.query_text)
        assert body["fields"][0] == "key"

    def test_key_field_not_duplicated(self) -> None:
        engine = _make_engine()
        # "key" already in the field list.
        fc = _make_field_contract(["key", "id", "name"])
        contract = engine.build(
            field_contract=fc,
            load_type=LoadType.FULL,
            watermark_field=None,
            watermark_lower=None,
            watermark_upper=None,
            extraction_window_days=0,
        )
        body = json.loads(contract.query_text)
        assert body["fields"].count("key") == 1


# ---------------------------------------------------------------------------
# bind_parameters
# ---------------------------------------------------------------------------


class TestBindParameters:
    def _make_body_with_placeholders(self) -> dict:
        return {
            "object": _OBJECT_PATH,
            "fields": ["key", "id"],
            "filters": [
                {"$gte": {"auditInfo.modifiedAt": _LOWER_BOUND_PLACEHOLDER}},
                {"$lt": {"auditInfo.modifiedAt": _UPPER_BOUND_PLACEHOLDER}},
            ],
            "orderBy": [{"key": "asc"}],
        }

    def test_bind_substitutes_lower_bound(self) -> None:
        body = self._make_body_with_placeholders()
        result = IntacctQueryEngine.bind_parameters(body, {"lower_bound": _LOWER, "upper_bound": _UPPER})
        assert result["filters"][0]["$gte"]["auditInfo.modifiedAt"] == _LOWER

    def test_bind_substitutes_upper_bound(self) -> None:
        body = self._make_body_with_placeholders()
        result = IntacctQueryEngine.bind_parameters(body, {"lower_bound": _LOWER, "upper_bound": _UPPER})
        assert result["filters"][1]["$lt"]["auditInfo.modifiedAt"] == _UPPER

    def test_bind_does_not_mutate_original(self) -> None:
        body = self._make_body_with_placeholders()
        IntacctQueryEngine.bind_parameters(body, {"lower_bound": _LOWER, "upper_bound": _UPPER})
        # Original must remain unchanged (deep copy)
        assert body["filters"][0]["$gte"]["auditInfo.modifiedAt"] == _LOWER_BOUND_PLACEHOLDER

    def test_bind_no_parameters_returns_body_unchanged(self) -> None:
        body = {"object": _OBJECT_PATH, "fields": ["key"], "orderBy": [{"key": "asc"}]}
        result = IntacctQueryEngine.bind_parameters(body, {})
        assert result == body

    def test_bind_invalid_lower_bound_raises(self) -> None:
        body = self._make_body_with_placeholders()
        with pytest.raises(SageQueryBuildError, match="lower_bound"):
            IntacctQueryEngine.bind_parameters(
                body, {"lower_bound": "'; DROP TABLE users; --", "upper_bound": _UPPER}
            )

    def test_bind_invalid_upper_bound_raises(self) -> None:
        body = self._make_body_with_placeholders()
        with pytest.raises(SageQueryBuildError, match="upper_bound"):
            IntacctQueryEngine.bind_parameters(
                body, {"lower_bound": _LOWER, "upper_bound": "not-a-date"}
            )

    def test_bind_accepts_iso8601_with_offset(self) -> None:
        """Timestamps with timezone offsets (not Z) should also be accepted."""
        body = self._make_body_with_placeholders()
        result = IntacctQueryEngine.bind_parameters(
            body,
            {
                "lower_bound": "2026-01-01T00:00:00+05:30",
                "upper_bound": "2026-07-01T00:00:00-04:00",
            },
        )
        assert result["filters"][0]["$gte"]["auditInfo.modifiedAt"] == "2026-01-01T00:00:00+05:30"

    def test_bind_accepts_fractional_seconds(self) -> None:
        body = self._make_body_with_placeholders()
        result = IntacctQueryEngine.bind_parameters(
            body,
            {
                "lower_bound": "2026-01-01T00:00:00.123Z",
                "upper_bound": "2026-07-01T00:00:00.999Z",
            },
        )
        assert result["filters"][0]["$gte"]["auditInfo.modifiedAt"] == "2026-01-01T00:00:00.123Z"
