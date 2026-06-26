"""
Tests for SalesforceMetadataDiscoveryClient.

Covers:
  - Field discovery with FieldMode.ALL
  - FieldMode.STANDARD filters custom fields
  - FieldMode.CUSTOM returns only custom fields
  - FieldMode.INCLUDE_ONLY returns only listed fields
  - Non-queryable fields are excluded automatically
  - Compound address/location fields excluded automatically
  - exclude_fields applied regardless of mode
  - Describe response is cached (second call does not hit HTTP again)
  - New fields in Describe response appear in next fresh instance (no code change)
  - FieldContract fingerprint is deterministic
"""

from __future__ import annotations

from unittest.mock import MagicMock

from connector_runtime.adapters.salesforce.salesforce_metadata_discovery_client import (
    SalesforceMetadataDiscoveryClient,
)
from contracts.entity_configuration_contract import FieldMode

_SOURCE_ID = "salesforce"
_ENTITY_ID = "salesforce-account"
_INSTANCE_URL = "https://myorg.my.salesforce.com"


def _make_auth(token: str = "tok") -> MagicMock:  # noqa: S107
    auth = MagicMock()
    auth.get_access_token.return_value = token
    auth.instance_url = _INSTANCE_URL
    return auth


def _describe_response(fields: list[dict]) -> dict:
    return {"fields": fields}


def _std_field(name: str, *, queryable: bool = True, nullable: bool = True) -> dict:
    return {
        "name": name,
        "type": "string",
        "queryable": queryable,
        "custom": False,
        "nillable": nullable,
        "label": name,
        "length": 255,
        "precision": None,
        "scale": None,
    }


def _custom_field(name: str) -> dict:
    return {
        "name": name,
        "type": "string",
        "queryable": True,
        "custom": True,
        "nillable": True,
        "label": name,
        "length": 255,
        "precision": None,
        "scale": None,
    }


def _address_field(name: str) -> dict:
    return {
        "name": name,
        "type": "address",
        "queryable": True,  # Salesforce marks compound address as queryable=False in practice,
        "custom": False,  # but we also exclude by type
        "nillable": True,
        "label": name,
        "length": None,
        "precision": None,
        "scale": None,
    }


# ---------------------------------------------------------------------------
# Field mode filtering
# ---------------------------------------------------------------------------


class TestFieldModeFiltering:
    def test_all_mode_returns_all_queryable_fields(self, requests_mock) -> None:  # type: ignore[no-untyped-def]
        auth = _make_auth()
        requests_mock.get(
            f"{_INSTANCE_URL}/services/data/v59.0/sobjects/Account/describe",
            json=_describe_response(
                [
                    _std_field("Id"),
                    _std_field("Name"),
                    _custom_field("Revenue__c"),
                ]
            ),
        )
        client = SalesforceMetadataDiscoveryClient(auth_client=auth, object_name="Account")
        contract = client.discover_fields(_SOURCE_ID, _ENTITY_ID, FieldMode.ALL, [], [])
        names = [f.name for f in contract.fields]
        assert "Id" in names
        assert "Name" in names
        assert "Revenue__c" in names

    def test_standard_mode_excludes_custom_fields(self, requests_mock) -> None:  # type: ignore[no-untyped-def]
        auth = _make_auth()
        requests_mock.get(
            f"{_INSTANCE_URL}/services/data/v59.0/sobjects/Account/describe",
            json=_describe_response([_std_field("Id"), _custom_field("Revenue__c")]),
        )
        client = SalesforceMetadataDiscoveryClient(auth_client=auth, object_name="Account")
        contract = client.discover_fields(_SOURCE_ID, _ENTITY_ID, FieldMode.STANDARD, [], [])
        names = [f.name for f in contract.fields]
        assert "Id" in names
        assert "Revenue__c" not in names

    def test_custom_mode_returns_only_custom_fields(self, requests_mock) -> None:  # type: ignore[no-untyped-def]
        auth = _make_auth()
        requests_mock.get(
            f"{_INSTANCE_URL}/services/data/v59.0/sobjects/Account/describe",
            json=_describe_response([_std_field("Id"), _custom_field("Revenue__c")]),
        )
        client = SalesforceMetadataDiscoveryClient(auth_client=auth, object_name="Account")
        contract = client.discover_fields(_SOURCE_ID, _ENTITY_ID, FieldMode.CUSTOM, [], [])
        names = [f.name for f in contract.fields]
        assert "Revenue__c" in names
        assert "Id" not in names

    def test_include_only_returns_exact_listed_fields(self, requests_mock) -> None:  # type: ignore[no-untyped-def]
        auth = _make_auth()
        requests_mock.get(
            f"{_INSTANCE_URL}/services/data/v59.0/sobjects/Account/describe",
            json=_describe_response(
                [
                    _std_field("Id"),
                    _std_field("Name"),
                    _std_field("Phone"),
                ]
            ),
        )
        client = SalesforceMetadataDiscoveryClient(auth_client=auth, object_name="Account")
        contract = client.discover_fields(
            _SOURCE_ID, _ENTITY_ID, FieldMode.INCLUDE_ONLY, ["Id", "Phone"], []
        )
        names = [f.name for f in contract.fields]
        assert names == ["Id", "Phone"]  # preserves order of include_fields


# ---------------------------------------------------------------------------
# Automatic exclusions
# ---------------------------------------------------------------------------


class TestAutomaticExclusions:
    def test_non_queryable_field_excluded(self, requests_mock) -> None:  # type: ignore[no-untyped-def]
        auth = _make_auth()
        requests_mock.get(
            f"{_INSTANCE_URL}/services/data/v59.0/sobjects/Account/describe",
            json=_describe_response(
                [
                    _std_field("Id"),
                    _std_field("HiddenField", queryable=False),
                ]
            ),
        )
        client = SalesforceMetadataDiscoveryClient(auth_client=auth, object_name="Account")
        contract = client.discover_fields(_SOURCE_ID, _ENTITY_ID, FieldMode.ALL, [], [])
        names = [f.name for f in contract.fields]
        assert "HiddenField" not in names

    def test_missing_queryable_attribute_defaults_to_queryable(self, requests_mock) -> None:  # type: ignore[no-untyped-def]
        auth = _make_auth()
        requests_mock.get(
            f"{_INSTANCE_URL}/services/data/v59.0/sobjects/Account/describe",
            json=_describe_response(
                [
                    {
                        "name": "Id",
                        "type": "id",
                        # queryable intentionally omitted (seen in real org payloads)
                        "custom": False,
                        "nillable": False,
                        "label": "Account ID",
                    },
                    {
                        "name": "Name",
                        "type": "string",
                        # queryable intentionally omitted
                        "custom": False,
                        "nillable": True,
                        "label": "Account Name",
                    },
                ]
            ),
        )
        client = SalesforceMetadataDiscoveryClient(auth_client=auth, object_name="Account")
        contract = client.discover_fields(_SOURCE_ID, _ENTITY_ID, FieldMode.ALL, [], [])
        names = [f.name for f in contract.fields]
        assert "Id" in names
        assert "Name" in names

    def test_compound_address_field_excluded(self, requests_mock) -> None:  # type: ignore[no-untyped-def]
        auth = _make_auth()
        requests_mock.get(
            f"{_INSTANCE_URL}/services/data/v59.0/sobjects/Account/describe",
            json=_describe_response(
                [
                    _std_field("Id"),
                    _address_field("BillingAddress"),
                ]
            ),
        )
        client = SalesforceMetadataDiscoveryClient(auth_client=auth, object_name="Account")
        contract = client.discover_fields(_SOURCE_ID, _ENTITY_ID, FieldMode.ALL, [], [])
        names = [f.name for f in contract.fields]
        assert "BillingAddress" not in names

    def test_exclude_fields_applied_regardless_of_mode(self, requests_mock) -> None:  # type: ignore[no-untyped-def]
        auth = _make_auth()
        requests_mock.get(
            f"{_INSTANCE_URL}/services/data/v59.0/sobjects/Account/describe",
            json=_describe_response(
                [
                    _std_field("Id"),
                    _std_field("IsDeleted"),
                    _std_field("Name"),
                ]
            ),
        )
        client = SalesforceMetadataDiscoveryClient(auth_client=auth, object_name="Account")
        contract = client.discover_fields(_SOURCE_ID, _ENTITY_ID, FieldMode.ALL, [], ["IsDeleted"])
        names = [f.name for f in contract.fields]
        assert "IsDeleted" not in names
        assert "Id" in names


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


class TestDescribeCaching:
    def test_describe_called_once_per_instance(self, requests_mock) -> None:  # type: ignore[no-untyped-def]
        auth = _make_auth()
        requests_mock.get(
            f"{_INSTANCE_URL}/services/data/v59.0/sobjects/Account/describe",
            json=_describe_response([_std_field("Id")]),
        )
        client = SalesforceMetadataDiscoveryClient(auth_client=auth, object_name="Account")
        client.discover_fields(_SOURCE_ID, _ENTITY_ID, FieldMode.ALL, [], [])
        client.discover_fields(_SOURCE_ID, _ENTITY_ID, FieldMode.ALL, [], [])
        assert requests_mock.call_count == 1  # HTTP called exactly once


# ---------------------------------------------------------------------------
# FieldContract properties
# ---------------------------------------------------------------------------


class TestFieldContractProperties:
    def test_fingerprint_is_deterministic(self, requests_mock) -> None:  # type: ignore[no-untyped-def]
        requests_mock.get(
            f"{_INSTANCE_URL}/services/data/v59.0/sobjects/Account/describe",
            json=_describe_response([_std_field("Id"), _std_field("Name")]),
        )
        c1 = SalesforceMetadataDiscoveryClient(auth_client=_make_auth(), object_name="Account")
        requests_mock.get(
            f"{_INSTANCE_URL}/services/data/v59.0/sobjects/Account/describe",
            json=_describe_response([_std_field("Id"), _std_field("Name")]),
        )
        c2 = SalesforceMetadataDiscoveryClient(auth_client=_make_auth(), object_name="Account")
        fp1 = c1.discover_fields(_SOURCE_ID, _ENTITY_ID, FieldMode.ALL, [], []).schema_fingerprint
        fp2 = c2.discover_fields(_SOURCE_ID, _ENTITY_ID, FieldMode.ALL, [], []).schema_fingerprint
        assert fp1 == fp2

    def test_new_field_in_describe_changes_fingerprint(self, requests_mock) -> None:  # type: ignore[no-untyped-def]
        """New fields added to Salesforce are picked up automatically — no code change needed."""
        auth1 = _make_auth()
        requests_mock.get(
            f"{_INSTANCE_URL}/services/data/v59.0/sobjects/Account/describe",
            json=_describe_response([_std_field("Id"), _std_field("Name")]),
        )
        c1 = SalesforceMetadataDiscoveryClient(auth_client=auth1, object_name="Account")
        fp1 = c1.discover_fields(_SOURCE_ID, _ENTITY_ID, FieldMode.ALL, [], []).schema_fingerprint

        # Simulate a new field added to Salesforce
        auth2 = _make_auth()
        requests_mock.get(
            f"{_INSTANCE_URL}/services/data/v59.0/sobjects/Account/describe",
            json=_describe_response(
                [
                    _std_field("Id"),
                    _std_field("Name"),
                    _std_field("NewField__c"),
                ]
            ),
        )
        c2 = SalesforceMetadataDiscoveryClient(auth_client=auth2, object_name="Account")
        fp2 = c2.discover_fields(_SOURCE_ID, _ENTITY_ID, FieldMode.ALL, [], []).schema_fingerprint

        assert fp1 != fp2
