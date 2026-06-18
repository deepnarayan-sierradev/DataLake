"""
Tests for NetSuiteMetadataAdapter.

Coverage:
  - Field discovery happy path (FieldMode.ALL)
  - FieldMode filtering: STANDARD, CUSTOM, INCLUDE_ONLY
  - Non-queryable field types excluded automatically
  - Describe response cached after first call
  - invalidate_cache() forces re-fetch
  - HTTP errors → NetSuiteMetadataAdapterError
  - Empty properties → NetSuiteMetadataAdapterError
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests_mock as requests_mock_lib

from connector_runtime.adapters.netsuite.netsuite_metadata_adapter import (
    NetSuiteMetadataAdapter,
    NetSuiteMetadataAdapterError,
)
from connector_runtime.interfaces.connector_interface import FieldContract
from contracts.entity_configuration_contract import FieldMode

_ACCOUNT_ID = "1234567"
_RECORD_TYPE = "customer"
_SOURCE_ID = "netsuite"
_ENTITY_ID = "netsuite-customer"

_METADATA_URL = (
    f"https://{_ACCOUNT_ID}.suitetalk.api.netsuite.com"
    f"/services/rest/record/v1/metadata-catalog/{_RECORD_TYPE}"
)

_STANDARD_RESPONSE: dict = {
    "properties": {
        "id": {
            "title": "Internal ID",
            "type": "integer",
            "nullable": False,
            "x-ns-custom-field": False,
        },
        "companyname": {
            "title": "Company Name",
            "type": "string",
            "nullable": True,
            "maxLength": 83,
            "x-ns-custom-field": False,
        },
        "lastmodifieddate": {
            "title": "Last Modified",
            "type": "string",
            "nullable": True,
            "x-ns-custom-field": False,
        },
        "custentity_loyalty_tier": {
            "title": "Loyalty Tier",
            "type": "string",
            "nullable": True,
            "x-ns-custom-field": True,
        },
        "logo": {
            "title": "Logo",
            "type": "DOCUMENT",
            "nullable": True,
            "x-ns-custom-field": False,
        },
    }
}


def _make_auth(token: str = "tok") -> MagicMock:  # noqa: S107
    auth = MagicMock()
    auth.account_id = _ACCOUNT_ID
    auth.get_auth_headers.return_value = {"Authorization": f"OAuth realm={token}"}
    return auth


class TestFieldModeFiltering:
    def test_field_mode_all_returns_queryable_fields(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.get(_METADATA_URL, json=_STANDARD_RESPONSE)
        adapter = NetSuiteMetadataAdapter(auth_client=_make_auth(), record_type=_RECORD_TYPE)
        contract = adapter.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        field_names = {f.name for f in contract.fields}
        # 'logo' has type DOCUMENT → non-queryable, excluded
        assert "logo" not in field_names
        assert "id" in field_names
        assert "companyname" in field_names
        assert "custentity_loyalty_tier" in field_names

    def test_field_mode_standard_excludes_custom_fields(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.get(_METADATA_URL, json=_STANDARD_RESPONSE)
        adapter = NetSuiteMetadataAdapter(auth_client=_make_auth(), record_type=_RECORD_TYPE)
        contract = adapter.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            field_mode=FieldMode.STANDARD,
            include_fields=[],
            exclude_fields=[],
        )
        field_names = {f.name for f in contract.fields}
        assert "custentity_loyalty_tier" not in field_names
        assert "id" in field_names

    def test_field_mode_custom_returns_only_custom(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.get(_METADATA_URL, json=_STANDARD_RESPONSE)
        adapter = NetSuiteMetadataAdapter(auth_client=_make_auth(), record_type=_RECORD_TYPE)
        contract = adapter.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            field_mode=FieldMode.CUSTOM,
            include_fields=[],
            exclude_fields=[],
        )
        field_names = {f.name for f in contract.fields}
        assert field_names == {"custentity_loyalty_tier"}

    def test_field_mode_include_only_returns_exact_set(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.get(_METADATA_URL, json=_STANDARD_RESPONSE)
        adapter = NetSuiteMetadataAdapter(auth_client=_make_auth(), record_type=_RECORD_TYPE)
        contract = adapter.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            field_mode=FieldMode.INCLUDE_ONLY,
            include_fields=["id", "lastmodifieddate"],
            exclude_fields=[],
        )
        field_names = {f.name for f in contract.fields}
        assert field_names == {"id", "lastmodifieddate"}

    def test_exclude_fields_removes_listed_fields(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.get(_METADATA_URL, json=_STANDARD_RESPONSE)
        adapter = NetSuiteMetadataAdapter(auth_client=_make_auth(), record_type=_RECORD_TYPE)
        contract = adapter.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=["companyname"],
        )
        field_names = {f.name for f in contract.fields}
        assert "companyname" not in field_names


class TestCaching:
    def test_metadata_cached_after_first_call(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.get(_METADATA_URL, json=_STANDARD_RESPONSE)
        adapter = NetSuiteMetadataAdapter(auth_client=_make_auth(), record_type=_RECORD_TYPE)
        adapter.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        adapter.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        # Only one HTTP request should have been made.
        assert requests_mock.call_count == 1

    def test_invalidate_cache_forces_refetch(self, requests_mock: requests_mock_lib.Mocker) -> None:
        requests_mock.get(_METADATA_URL, json=_STANDARD_RESPONSE)
        adapter = NetSuiteMetadataAdapter(auth_client=_make_auth(), record_type=_RECORD_TYPE)
        adapter.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        adapter.invalidate_cache()
        adapter.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        assert requests_mock.call_count == 2


class TestErrorHandling:
    def test_http_error_raises_adapter_error(self, requests_mock: requests_mock_lib.Mocker) -> None:
        requests_mock.get(_METADATA_URL, status_code=404)
        adapter = NetSuiteMetadataAdapter(auth_client=_make_auth(), record_type=_RECORD_TYPE)
        with pytest.raises(NetSuiteMetadataAdapterError, match="HTTP 404"):
            adapter.discover_fields(
                source_id=_SOURCE_ID,
                entity_id=_ENTITY_ID,
                field_mode=FieldMode.ALL,
                include_fields=[],
                exclude_fields=[],
            )

    def test_empty_properties_raises_adapter_error(
        self, requests_mock: requests_mock_lib.Mocker
    ) -> None:
        requests_mock.get(_METADATA_URL, json={"properties": {}})
        adapter = NetSuiteMetadataAdapter(auth_client=_make_auth(), record_type=_RECORD_TYPE)
        with pytest.raises(NetSuiteMetadataAdapterError, match="no 'properties'"):
            adapter.discover_fields(
                source_id=_SOURCE_ID,
                entity_id=_ENTITY_ID,
                field_mode=FieldMode.ALL,
                include_fields=[],
                exclude_fields=[],
            )

    def test_empty_record_type_raises(self) -> None:
        with pytest.raises(ValueError, match="record_type"):
            NetSuiteMetadataAdapter(auth_client=_make_auth(), record_type="")


class TestFieldContractProperties:
    def test_fingerprint_deterministic(self, requests_mock: requests_mock_lib.Mocker) -> None:
        requests_mock.get(_METADATA_URL, json=_STANDARD_RESPONSE)
        adapter = NetSuiteMetadataAdapter(auth_client=_make_auth(), record_type=_RECORD_TYPE)
        c1 = adapter.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        adapter.invalidate_cache()
        c2 = adapter.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        assert c1.schema_fingerprint == c2.schema_fingerprint

    def test_field_contract_is_frozen(self, requests_mock: requests_mock_lib.Mocker) -> None:
        requests_mock.get(_METADATA_URL, json=_STANDARD_RESPONSE)
        adapter = NetSuiteMetadataAdapter(auth_client=_make_auth(), record_type=_RECORD_TYPE)
        contract = adapter.discover_fields(
            source_id=_SOURCE_ID,
            entity_id=_ENTITY_ID,
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
        assert isinstance(contract, FieldContract)
        with pytest.raises((AttributeError, TypeError)):
            contract.source_id = "changed"  # type: ignore[misc]
