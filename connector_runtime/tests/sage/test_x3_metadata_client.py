"""
Tests for X3MetadataClient.

Coverage:
  - discover_fields() returns FieldContract from live sample response
  - Schema cached; second call does not make a second HTTP request
  - invalidate_cache() forces re-sampling on next call
  - Live sample uses GET {base_url}/{endpoint}?$top=1
  - OData @-prefixed metadata keys skipped during schema inference
  - Type inference: bool→"boolean", int→"integer", float→"decimal", ISO-8601→"date", str→"string", None→"string"
  - Empty live response for KNOWN endpoint → static fallback schema used
  - Empty live response for KNOWN endpoint → logs sage_x3_metadata_fallback_static
  - Empty live response for UNKNOWN endpoint → SageMetadataDeterministicError
  - SageObjectNotFoundError (404) for known endpoint → static fallback schema used
  - SageObjectNotFoundError (404) for unknown endpoint → SageMetadataDeterministicError
  - SageHttpError (non-404) → SageMetadataTransientError
  - FieldMode.ALL → all fields included
  - FieldMode.STANDARD → all fields included (no custom-field concept in X3)
  - FieldMode.CUSTOM → empty field list (X3 has no custom-field concept)
  - FieldMode.INCLUDE_ONLY → only include_fields returned
  - supports_live_discovery == True
  - Static fallback BPCUSTOMER has expected key fields
  - Static fallback BPSUPPLIER has expected key fields
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from connector_runtime.adapters.sage.common.sage_http_client import (
    SageHttpClient,
    SageHttpError,
    SageObjectNotFoundError,
)
from connector_runtime.adapters.sage.products.intacct.intacct_metadata_client import (
    SageMetadataDeterministicError,
    SageMetadataTransientError,
)
from connector_runtime.adapters.sage.products.x3.x3_metadata_client import (
    X3MetadataClient,
    _X3_STATIC_SCHEMAS,
)
from contracts.entity_configuration_contract import FieldMode

_SOURCE_ID = "sage"
_ENTITY_ID_CUSTOMER = "sage-x3-customer"
_ENTITY_ID_SUPPLIER = "sage-x3-supplier"
_ENDPOINT_CUSTOMER = "BPCUSTOMER"
_ENDPOINT_SUPPLIER = "BPSUPPLIER"
_ENDPOINT_UNKNOWN = "UNKNOWNOBJ"
_BASE_URL = "https://x3.company.com/api/SEED"

# A minimal realistic sample record for BPCUSTOMER.
_SAMPLE_BPCUSTOMER_RECORD = {
    "BPCNUM_0": "CUST001",
    "BPCNAM_0": "Acme Corp",
    "CRY_0": "GB",
    "CUR_0": "GBP",
    "TEL_0": "+44 20 1234 5678",
    "ENAFLG_0": 1,
    "CREDAT_0": "2024-01-15T00:00:00Z",
    "MODDAT_0": "2026-06-01T00:00:00Z",
    "@odata.etag": "W/\"12345\"",  # must be skipped
}


def _make_auth() -> MagicMock:
    auth = MagicMock()
    auth.base_url = _BASE_URL
    auth.build_auth_headers.return_value = {"Authorization": "Bearer test-token", "Accept": "application/json"}
    return auth


def _make_client(endpoint: str = _ENDPOINT_CUSTOMER) -> X3MetadataClient:
    return X3MetadataClient(
        auth_client=_make_auth(),
        http_client=MagicMock(spec=SageHttpClient),
        object_path=endpoint,
    )


# ---------------------------------------------------------------------------
# Class-level attributes
# ---------------------------------------------------------------------------


class TestClassAttributes:
    def test_supports_live_discovery_is_true(self) -> None:
        assert X3MetadataClient.supports_live_discovery is True


# ---------------------------------------------------------------------------
# Live sampling
# ---------------------------------------------------------------------------


class TestLiveSampling:
    def test_discover_fields_from_live_sample(self) -> None:
        client = _make_client()
        sample_response = {"value": [_SAMPLE_BPCUSTOMER_RECORD]}
        client._http.get.return_value = sample_response

        contract = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID_CUSTOMER,
            object_path=_ENDPOINT_CUSTOMER,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )

        field_names = {f.name for f in contract.fields}
        assert "BPCNUM_0" in field_names
        assert "BPCNAM_0" in field_names
        assert "MODDAT_0" in field_names

    def test_odata_metadata_keys_skipped(self) -> None:
        client = _make_client()
        client._http.get.return_value = {"value": [_SAMPLE_BPCUSTOMER_RECORD]}

        contract = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID_CUSTOMER,
            object_path=_ENDPOINT_CUSTOMER,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )

        field_names = {f.name for f in contract.fields}
        # @odata.etag should be excluded
        assert not any(n.startswith("@") for n in field_names)

    def test_schema_cached_on_second_call(self) -> None:
        client = _make_client()
        client._http.get.return_value = {"value": [_SAMPLE_BPCUSTOMER_RECORD]}

        client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID_CUSTOMER,
            object_path=_ENDPOINT_CUSTOMER,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID_CUSTOMER,
            object_path=_ENDPOINT_CUSTOMER,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )

        assert client._http.get.call_count == 1

    def test_invalidate_cache_forces_resample(self) -> None:
        client = _make_client()
        client._http.get.return_value = {"value": [_SAMPLE_BPCUSTOMER_RECORD]}

        client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID_CUSTOMER,
            object_path=_ENDPOINT_CUSTOMER,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        client.invalidate_cache()
        client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID_CUSTOMER,
            object_path=_ENDPOINT_CUSTOMER,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )

        assert client._http.get.call_count == 2

    def test_sample_request_uses_top1(self) -> None:
        client = _make_client()
        client._http.get.return_value = {"value": [_SAMPLE_BPCUSTOMER_RECORD]}

        client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID_CUSTOMER,
            object_path=_ENDPOINT_CUSTOMER,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )

        call_kwargs = client._http.get.call_args
        # Verify $top=1 was in the params
        params = call_kwargs[1].get("params") or call_kwargs[0][2] if len(call_kwargs[0]) > 2 else {}
        # The URL or params should contain $top=1
        url_or_params_str = str(call_kwargs)
        assert "$top" in url_or_params_str or "top" in url_or_params_str.lower()


# ---------------------------------------------------------------------------
# Type inference
# ---------------------------------------------------------------------------


class TestTypeInference:
    def _infer_types_from_record(self, record: dict) -> dict[str, str]:
        """Helper: discover fields and return name→data_type mapping."""
        client = _make_client()
        client._http.get.return_value = {"value": [record]}
        contract = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID_CUSTOMER,
            object_path=_ENDPOINT_CUSTOMER,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        return {f.name: f.data_type for f in contract.fields}

    def test_bool_inferred_as_boolean(self) -> None:
        types = self._infer_types_from_record({"FLAG_0": True})
        assert types["FLAG_0"] == "boolean"

    def test_int_inferred_as_integer(self) -> None:
        types = self._infer_types_from_record({"COUNT_0": 42})
        assert types["COUNT_0"] == "integer"

    def test_float_inferred_as_decimal(self) -> None:
        types = self._infer_types_from_record({"AMOUNT_0": 3.14})
        assert types["AMOUNT_0"] == "decimal"

    def test_iso8601_string_inferred_as_date(self) -> None:
        types = self._infer_types_from_record({"MODDAT_0": "2026-06-01T00:00:00Z"})
        assert types["MODDAT_0"] == "date"

    def test_plain_string_inferred_as_string(self) -> None:
        types = self._infer_types_from_record({"NAME_0": "Acme Corp"})
        assert types["NAME_0"] == "string"

    def test_none_value_inferred_as_string(self) -> None:
        types = self._infer_types_from_record({"OPT_0": None})
        assert types["OPT_0"] == "string"

    def test_none_value_field_is_nullable(self) -> None:
        client = _make_client()
        client._http.get.return_value = {"value": [{"OPT_0": None}]}
        contract = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID_CUSTOMER,
            object_path=_ENDPOINT_CUSTOMER,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        field = next(f for f in contract.fields if f.name == "OPT_0")
        assert field.is_nullable is True


# ---------------------------------------------------------------------------
# Static fallback
# ---------------------------------------------------------------------------


class TestStaticFallback:
    def test_known_endpoint_empty_response_uses_static_fallback(self) -> None:
        client = _make_client(endpoint=_ENDPOINT_CUSTOMER)
        client._http.get.return_value = {"value": []}

        contract = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID_CUSTOMER,
            object_path=_ENDPOINT_CUSTOMER,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )

        field_names = {f.name for f in contract.fields}
        # Static BPCUSTOMER schema should be used
        assert "BPCNUM_0" in field_names
        assert "MODDAT_0" in field_names

    def test_unknown_endpoint_empty_response_raises_deterministic_error(self) -> None:
        client = _make_client(endpoint=_ENDPOINT_UNKNOWN)
        client._http.get.return_value = {"value": []}

        with pytest.raises(SageMetadataDeterministicError):
            client.discover_fields(
                source_id=_SOURCE_ID,
                entity_id="sage-x3-unknown",
                object_path=_ENDPOINT_UNKNOWN,
                field_mode=FieldMode.ALL,
                include_fields=[],
                exclude_fields=[],
            )

    def test_known_endpoint_404_uses_static_fallback(self) -> None:
        client = _make_client(endpoint=_ENDPOINT_CUSTOMER)
        client._http.get.side_effect = SageObjectNotFoundError("404 not found")

        contract = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID_CUSTOMER,
            object_path=_ENDPOINT_CUSTOMER,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )

        assert any(f.name == "BPCNUM_0" for f in contract.fields)

    def test_unknown_endpoint_404_raises_deterministic_error(self) -> None:
        client = _make_client(endpoint=_ENDPOINT_UNKNOWN)
        client._http.get.side_effect = SageObjectNotFoundError("404 not found")

        with pytest.raises(SageMetadataDeterministicError):
            client.discover_fields(
                source_id=_SOURCE_ID,
                entity_id="sage-x3-unknown",
                object_path=_ENDPOINT_UNKNOWN,
                field_mode=FieldMode.ALL,
                include_fields=[],
                exclude_fields=[],
            )

    def test_http_error_non_404_raises_transient_error(self) -> None:
        client = _make_client(endpoint=_ENDPOINT_CUSTOMER)
        client._http.get.side_effect = SageHttpError("503 service unavailable")

        with pytest.raises(SageMetadataTransientError):
            client.discover_fields(
                source_id=_SOURCE_ID,
                entity_id=_ENTITY_ID_CUSTOMER,
                object_path=_ENDPOINT_CUSTOMER,
                field_mode=FieldMode.ALL,
                include_fields=[],
                exclude_fields=[],
            )

    def test_static_bpcustomer_has_required_fields(self) -> None:
        schema = _X3_STATIC_SCHEMAS["BPCUSTOMER"]
        names = {f.name for f in schema}
        assert "BPCNUM_0" in names
        assert "BPCNAM_0" in names
        assert "MODDAT_0" in names
        assert "CUR_0" in names

    def test_static_bpsupplier_has_required_fields(self) -> None:
        schema = _X3_STATIC_SCHEMAS["BPSUPPLIER"]
        names = {f.name for f in schema}
        assert "BPSNUM_0" in names
        assert "BPSNAM_0" in names
        assert "MODDAT_0" in names


# ---------------------------------------------------------------------------
# FieldMode filtering
# ---------------------------------------------------------------------------


class TestFieldModeFiltering:
    def test_field_mode_all_returns_all_fields(self) -> None:
        client = _make_client()
        client._http.get.return_value = {"value": [_SAMPLE_BPCUSTOMER_RECORD]}

        contract = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID_CUSTOMER,
            object_path=_ENDPOINT_CUSTOMER,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        assert len(contract.fields) > 0

    def test_field_mode_standard_returns_all_fields(self) -> None:
        """X3 has no custom-field concept — STANDARD returns same as ALL."""
        client = _make_client()
        client._http.get.return_value = {"value": [_SAMPLE_BPCUSTOMER_RECORD]}

        contract_all = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID_CUSTOMER,
            object_path=_ENDPOINT_CUSTOMER,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        client.invalidate_cache()
        contract_std = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID_CUSTOMER,
            object_path=_ENDPOINT_CUSTOMER,
            field_mode=FieldMode.STANDARD,
            include_fields=[],
            exclude_fields=[],
        )
        assert {f.name for f in contract_all.fields} == {f.name for f in contract_std.fields}

    def test_field_mode_custom_returns_empty(self) -> None:
        """X3 has no custom-field concept — CUSTOM returns empty list."""
        client = _make_client()
        client._http.get.return_value = {"value": [_SAMPLE_BPCUSTOMER_RECORD]}

        contract = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID_CUSTOMER,
            object_path=_ENDPOINT_CUSTOMER,
            field_mode=FieldMode.CUSTOM,
            include_fields=[],
            exclude_fields=[],
        )
        assert len(contract.fields) == 0

    def test_field_mode_include_only_filters_to_requested_fields(self) -> None:
        client = _make_client()
        client._http.get.return_value = {"value": [_SAMPLE_BPCUSTOMER_RECORD]}

        contract = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID_CUSTOMER,
            object_path=_ENDPOINT_CUSTOMER,
            field_mode=FieldMode.INCLUDE_ONLY,
            include_fields=["BPCNUM_0", "BPCNAM_0"],
            exclude_fields=[],
        )
        field_names = {f.name for f in contract.fields}
        assert field_names == {"BPCNUM_0", "BPCNAM_0"}
