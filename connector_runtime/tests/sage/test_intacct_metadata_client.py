"""
Tests for IntacctMetadataClient.

Coverage:
  - Constructor rejects invalid object_path → SageMetadataError
  - discover_fields(ALL) → returns all queryable fields
  - discover_fields(STANDARD) → only non-custom fields
  - discover_fields(CUSTOM) → only custom (nsp::) fields
  - discover_fields(INCLUDE_ONLY) → only fields in include_fields list
  - exclude_fields removes named fields from any mode
  - writeOnly=true fields excluded from FieldContract
  - deprecated=true fields excluded from FieldContract
  - custom=true in response marks field as is_custom
  - nsp:: prefix marks field as is_custom
  - Field max_length preserved in FieldDescriptor
  - Per-instance cache hit: no second HTTP call
  - invalidate_cache(): forces re-fetch on next discover_fields()
  - SageObjectNotFoundError → wrapped as SageMetadataError
  - Other HTTP error → SageMetadataError
  - Empty fields list in response → SageMetadataError
  - Schema fingerprint computed and present
  - supports_live_discovery is True
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from connector_runtime.adapters.sage.common.sage_http_client import (
    SageHttpClient,
    SageObjectNotFoundError,
    SageRateLimitError,
)
from connector_runtime.adapters.sage.products.intacct.intacct_auth import IntacctAuthClient
from connector_runtime.adapters.sage.products.intacct.intacct_metadata_client import (
    IntacctMetadataClient,
    SageMetadataError,
)
from contracts.entity_configuration_contract import FieldMode

_OBJECT_PATH = "accounts-receivable/customer"
_SOURCE_ID = "sage"
_ENTITY_ID = "sage-intacct-customer"
_BASE_URL = "https://api.intacct.com/ia/api/v1"
_MODELS_URL = f"{_BASE_URL}/objects/{_OBJECT_PATH}"

# Realistic Models API response body.
_MODELS_RESPONSE = {
    "ia::result": {
        "object": "accounts-receivable/customer",
        "fields": [
            {
                "name": "key",
                "label": "Record Number",
                "type": "integer",
                "queryable": True,
                "nullable": False,
                "deprecated": False,
                "writeOnly": False,
                "custom": False,
                "maxLength": None,
            },
            {
                "name": "id",
                "label": "Customer ID",
                "type": "string",
                "queryable": True,
                "nullable": True,
                "deprecated": False,
                "writeOnly": False,
                "custom": False,
                "maxLength": 20,
            },
            {
                "name": "name",
                "label": "Company Name",
                "type": "string",
                "queryable": True,
                "nullable": True,
                "deprecated": False,
                "writeOnly": False,
                "custom": False,
                "maxLength": 100,
            },
            {
                "name": "status",
                "label": "Status",
                "type": "string",
                "queryable": True,
                "nullable": True,
                "deprecated": False,
                "writeOnly": False,
                "custom": False,
                "maxLength": 10,
            },
            {
                "name": "nsp::CUSTOM_TIER",
                "label": "Custom Tier",
                "type": "string",
                "queryable": True,
                "nullable": True,
                "deprecated": False,
                "writeOnly": False,
                "custom": True,
                "maxLength": 50,
            },
            {
                "name": "internalNotes",
                "label": "Internal Notes",
                "type": "string",
                "queryable": False,  # writeOnly-like
                "nullable": True,
                "deprecated": False,
                "writeOnly": True,  # non-queryable — must be excluded
                "custom": False,
                "maxLength": None,
            },
            {
                "name": "legacyCode",
                "label": "Legacy Code",
                "type": "string",
                "queryable": False,
                "nullable": True,
                "deprecated": True,  # deprecated — must be excluded
                "writeOnly": False,
                "custom": False,
                "maxLength": None,
            },
        ],
    }
}


def _make_mock_auth() -> MagicMock:
    auth = MagicMock(spec=IntacctAuthClient)
    auth.base_url = _BASE_URL
    auth.build_auth_headers.return_value = {
        "Authorization": "Bearer test-token",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    return auth


def _make_mock_http(response: dict | None = None) -> MagicMock:
    http = MagicMock(spec=SageHttpClient)
    http.get.return_value = response or _MODELS_RESPONSE
    return http


def _make_client(
    object_path: str = _OBJECT_PATH,
    http_response: dict | None = None,
) -> IntacctMetadataClient:
    return IntacctMetadataClient(
        auth_client=_make_mock_auth(),
        http_client=_make_mock_http(http_response),
        object_path=object_path,
    )


# ---------------------------------------------------------------------------
# Class attribute
# ---------------------------------------------------------------------------


class TestClassAttributes:
    def test_supports_live_discovery_is_true(self) -> None:
        assert IntacctMetadataClient.supports_live_discovery is True


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestConstructorValidation:
    def test_invalid_object_path_raises(self) -> None:
        with pytest.raises(SageMetadataError, match="object_path"):
            IntacctMetadataClient(
                auth_client=_make_mock_auth(),
                http_client=_make_mock_http(),
                object_path="INVALID_NO_SLASH",
            )

    def test_path_traversal_raises(self) -> None:
        with pytest.raises(SageMetadataError):
            IntacctMetadataClient(
                auth_client=_make_mock_auth(),
                http_client=_make_mock_http(),
                object_path="../../../etc/passwd",
            )

    def test_valid_object_path_accepted(self) -> None:
        client = _make_client()
        assert client is not None


# ---------------------------------------------------------------------------
# Field mode filtering
# ---------------------------------------------------------------------------


class TestFieldModeAll:
    def test_all_mode_returns_all_queryable_fields(self) -> None:
        client = _make_client()
        fc = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            object_path=_OBJECT_PATH,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        names = {f.name for f in fc.fields}
        # key, id, name, status, nsp::CUSTOM_TIER should be included.
        assert "key" in names
        assert "id" in names
        assert "nsp::CUSTOM_TIER" in names

    def test_all_mode_excludes_write_only(self) -> None:
        client = _make_client()
        fc = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            object_path=_OBJECT_PATH,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        names = {f.name for f in fc.fields}
        assert "internalNotes" not in names

    def test_all_mode_excludes_deprecated(self) -> None:
        client = _make_client()
        fc = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            object_path=_OBJECT_PATH,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        names = {f.name for f in fc.fields}
        assert "legacyCode" not in names


class TestFieldModeStandard:
    def test_standard_mode_excludes_custom_fields(self) -> None:
        client = _make_client()
        fc = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            object_path=_OBJECT_PATH,
            field_mode=FieldMode.STANDARD,
            include_fields=[],
            exclude_fields=[],
        )
        names = {f.name for f in fc.fields}
        assert "nsp::CUSTOM_TIER" not in names
        assert "key" in names
        assert "id" in names

    def test_standard_mode_includes_standard_fields(self) -> None:
        client = _make_client()
        fc = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            object_path=_OBJECT_PATH,
            field_mode=FieldMode.STANDARD,
            include_fields=[],
            exclude_fields=[],
        )
        names = {f.name for f in fc.fields}
        assert "name" in names
        assert "status" in names


class TestFieldModeCustom:
    def test_custom_mode_returns_only_custom_fields(self) -> None:
        client = _make_client()
        fc = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            object_path=_OBJECT_PATH,
            field_mode=FieldMode.CUSTOM,
            include_fields=[],
            exclude_fields=[],
        )
        names = {f.name for f in fc.fields}
        assert names == {"nsp::CUSTOM_TIER"}

    def test_custom_mode_excludes_standard_fields(self) -> None:
        client = _make_client()
        fc = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            object_path=_OBJECT_PATH,
            field_mode=FieldMode.CUSTOM,
            include_fields=[],
            exclude_fields=[],
        )
        names = {f.name for f in fc.fields}
        assert "id" not in names
        assert "name" not in names


class TestFieldModeIncludeOnly:
    def test_include_only_mode_returns_exact_fields(self) -> None:
        client = _make_client()
        fc = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            object_path=_OBJECT_PATH,
            field_mode=FieldMode.INCLUDE_ONLY,
            include_fields=["key", "id"],
            exclude_fields=[],
        )
        names = {f.name for f in fc.fields}
        assert names == {"key", "id"}

    def test_include_only_does_not_include_non_listed_fields(self) -> None:
        client = _make_client()
        fc = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            object_path=_OBJECT_PATH,
            field_mode=FieldMode.INCLUDE_ONLY,
            include_fields=["key"],
            exclude_fields=[],
        )
        names = {f.name for f in fc.fields}
        assert "name" not in names
        assert "status" not in names


class TestExcludeFields:
    def test_exclude_fields_removes_named_fields(self) -> None:
        client = _make_client()
        fc = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            object_path=_OBJECT_PATH,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=["status"],
        )
        names = {f.name for f in fc.fields}
        assert "status" not in names
        assert "id" in names

    def test_exclude_fields_respected_in_include_only(self) -> None:
        client = _make_client()
        fc = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            object_path=_OBJECT_PATH,
            field_mode=FieldMode.INCLUDE_ONLY,
            include_fields=["key", "id"],
            exclude_fields=["id"],
        )
        names = {f.name for f in fc.fields}
        assert "id" not in names
        assert "key" in names


# ---------------------------------------------------------------------------
# FieldDescriptor attributes
# ---------------------------------------------------------------------------


class TestFieldDescriptorAttributes:
    def test_custom_field_marked_is_custom(self) -> None:
        client = _make_client()
        fc = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            object_path=_OBJECT_PATH,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        custom = next(f for f in fc.fields if f.name == "nsp::CUSTOM_TIER")
        assert custom.is_custom is True

    def test_standard_field_not_is_custom(self) -> None:
        client = _make_client()
        fc = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            object_path=_OBJECT_PATH,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        id_field = next(f for f in fc.fields if f.name == "id")
        assert id_field.is_custom is False

    def test_max_length_preserved(self) -> None:
        client = _make_client()
        fc = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            object_path=_OBJECT_PATH,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        id_field = next(f for f in fc.fields if f.name == "id")
        assert id_field.length == 20

    def test_schema_fingerprint_present(self) -> None:
        client = _make_client()
        fc = client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            object_path=_OBJECT_PATH,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        assert fc.schema_fingerprint
        assert len(fc.schema_fingerprint) == 64  # SHA-256 hex


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


class TestCaching:
    def test_second_discover_fields_uses_cache(self) -> None:
        mock_http = _make_mock_http()
        client = IntacctMetadataClient(
            auth_client=_make_mock_auth(),
            http_client=mock_http,
            object_path=_OBJECT_PATH,
        )
        client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            object_path=_OBJECT_PATH,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            object_path=_OBJECT_PATH,
            field_mode=FieldMode.STANDARD,
            include_fields=[],
            exclude_fields=[],
        )
        # HTTP get should only be called once.
        assert mock_http.get.call_count == 1

    def test_invalidate_cache_triggers_refetch(self) -> None:
        mock_http = _make_mock_http()
        client = IntacctMetadataClient(
            auth_client=_make_mock_auth(),
            http_client=mock_http,
            object_path=_OBJECT_PATH,
        )
        client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            object_path=_OBJECT_PATH,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        client.invalidate_cache()
        client.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            object_path=_OBJECT_PATH,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        assert mock_http.get.call_count == 2


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestErrorCases:
    def test_object_not_found_wrapped_as_metadata_error(self) -> None:
        mock_http = MagicMock(spec=SageHttpClient)
        mock_http.get.side_effect = SageObjectNotFoundError("404")
        client = IntacctMetadataClient(
            auth_client=_make_mock_auth(),
            http_client=mock_http,
            object_path=_OBJECT_PATH,
        )
        with pytest.raises(SageMetadataError, match="not found"):
            client.discover_fields(
                source_id=_SOURCE_ID,
                entity_id=_ENTITY_ID,
                object_path=_OBJECT_PATH,
                field_mode=FieldMode.ALL,
                include_fields=[],
                exclude_fields=[],
            )

    def test_generic_http_error_wrapped_as_metadata_error(self) -> None:
        mock_http = MagicMock(spec=SageHttpClient)
        mock_http.get.side_effect = SageRateLimitError("429")
        client = IntacctMetadataClient(
            auth_client=_make_mock_auth(),
            http_client=mock_http,
            object_path=_OBJECT_PATH,
        )
        with pytest.raises(SageMetadataError, match="Models endpoint"):
            client.discover_fields(
                source_id=_SOURCE_ID,
                entity_id=_ENTITY_ID,
                object_path=_OBJECT_PATH,
                field_mode=FieldMode.ALL,
                include_fields=[],
                exclude_fields=[],
            )

    def test_empty_fields_in_response_raises_metadata_error(self) -> None:
        empty_response = {"ia::result": {"object": _OBJECT_PATH, "fields": []}}
        client = _make_client(http_response=empty_response)
        with pytest.raises(SageMetadataError, match="no 'fields' section"):
            client.discover_fields(
                source_id=_SOURCE_ID,
                entity_id=_ENTITY_ID,
                object_path=_OBJECT_PATH,
                field_mode=FieldMode.ALL,
                include_fields=[],
                exclude_fields=[],
            )

    def test_missing_fields_key_raises_metadata_error(self) -> None:
        """Response with ia::result but no 'fields' key → SageMetadataError."""
        response_no_fields = {"ia::result": {"object": _OBJECT_PATH}}
        client = _make_client(http_response=response_no_fields)
        with pytest.raises(SageMetadataError, match="no 'fields' section"):
            client.discover_fields(
                source_id=_SOURCE_ID,
                entity_id=_ENTITY_ID,
                object_path=_OBJECT_PATH,
                field_mode=FieldMode.ALL,
                include_fields=[],
                exclude_fields=[],
            )
