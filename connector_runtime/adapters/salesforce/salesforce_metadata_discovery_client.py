"""
Salesforce metadata discovery client.

Calls the Salesforce Describe API to discover all queryable fields for an
entity (object).  Applies field mode filtering (all, standard, custom,
includeOnly) and excludes non-queryable and unsupported fields automatically.

No hardcoded field lists anywhere in this module — every field set is
derived from Salesforce metadata at runtime, satisfying the spec requirement:
  "Handle custom fields and newly added fields without code changes."

Security (OWASP A03, A09):
  - API call is authenticated with a short-lived token from SalesforceAuthClient.
  - Token is passed in the Authorization header only; never logged or stored.
  - Describe response is never written to durable storage.

Naming per spec: salesforce_metadata_discovery_client
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import requests

from connector_runtime.adapters.salesforce.salesforce_auth_protocol import SalesforceAuthProtocol
from connector_runtime.interfaces.connector_interface import FieldContract, FieldDescriptor
from contracts.entity_configuration_contract import FieldMode
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

_DESCRIBE_PATH_TEMPLATE: Final[str] = "/services/data/v59.0/sobjects/{object_name}/describe"

# Salesforce compound fields (e.g. BillingAddress) are not directly queryable
# in SOQL — their sub-fields are.  We filter them to prevent invalid queries.
_NON_QUERYABLE_COMPOUND_TYPES: Final[frozenset[str]] = frozenset({"address", "location"})


@dataclass(frozen=True)
class SalesforceFieldMetadata:
    """
    Raw Salesforce field descriptor as returned by the Describe API.

    Only the attributes needed for FieldContract construction are captured.
    """

    name: str
    data_type: str  # Salesforce field type string, e.g. "string", "datetime", "id"
    is_queryable: bool
    is_custom: bool
    is_nullable: bool  # nillable in Salesforce terminology
    label: str
    length: int | None
    precision: int | None
    scale: int | None


class SalesforceMetadataDiscoveryClient:
    """
    Discovers queryable Salesforce object fields via the Describe REST API.

    One instance is created per extraction run and per entity.
    The Describe response is cached for the lifetime of the instance to avoid
    redundant API calls during the same run.

    Usage::

        client = SalesforceMetadataDiscoveryClient(
            auth_client=auth,
            object_name="Account",
        )
        contract = client.discover_fields(
            source_id="salesforce",
            entity_id="salesforce-account",
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=["IsDeleted"],
        )
    """

    def __init__(
        self,
        auth_client: SalesforceAuthProtocol,
        object_name: str,
    ) -> None:
        if not object_name:
            raise ValueError("object_name must not be empty.")
        self._auth = auth_client
        self._object_name = object_name
        self._cached_fields: list[SalesforceFieldMetadata] | None = None

    def invalidate_cache(self) -> None:
        """
        Clear the cached Describe response.

        Call this when a cache refresh is needed within the same instance
        lifetime (e.g. after detecting a schema change mid-run).
        """
        self._cached_fields = None

    def discover_fields(
        self,
        source_id: str,
        entity_id: str,
        field_mode: FieldMode,
        include_fields: list[str],
        exclude_fields: list[str],
    ) -> FieldContract:
        """
        Discover queryable fields and return a FieldContract.

        Steps:
          1. Call Salesforce Describe API (cached after first call this instance).
          2. Filter by field_mode (all / standard / custom / includeOnly).
          3. Exclude fields in exclude_fields regardless of mode.
          4. Drop non-queryable and compound/unsupported fields automatically.
          5. Compute deterministic schema_fingerprint.

        Returns:
            FieldContract with all qualifying fields and a fingerprint.

        Raises:
            requests.HTTPError: on non-2xx Describe response.
        """
        from datetime import UTC, datetime

        raw_fields = self._fetch_describe()
        filtered = self._apply_field_mode(raw_fields, field_mode, include_fields)
        excluded = [f for f in filtered if f.name not in set(exclude_fields)]

        descriptors = tuple(
            FieldDescriptor(
                name=f.name,
                data_type=f.data_type,
                is_nullable=f.is_nullable,
                is_queryable=f.is_queryable,
                length=f.length,
                precision=f.precision,
                scale=f.scale,
                is_custom=f.is_custom,
                source_label=f.label,
            )
            for f in excluded
        )

        fingerprint = FieldContract.compute_fingerprint(descriptors)

        _logger.info(
            "salesforce_fields_discovered",
            source_id=source_id,
            entity_id=entity_id,
            object_name=self._object_name,
            field_count=len(descriptors),
            field_mode=str(field_mode),
            fingerprint=fingerprint,
        )

        return FieldContract(
            source_id=source_id,
            entity_id=entity_id,
            fields=descriptors,
            discovery_timestamp=datetime.now(UTC),
            schema_fingerprint=fingerprint,
        )

    # ── Private ────────────────────────────────────────────────────────────────

    def _fetch_describe(self) -> list[SalesforceFieldMetadata]:
        """
        Fetch and parse the Describe response, caching for the lifetime of this instance.

        The token is obtained from auth_client.get_access_token() and passed
        in the Authorization header — never stored in the request URL.
        """
        if self._cached_fields is not None:
            return self._cached_fields

        token = self._auth.get_access_token()
        url = (
            f"{self._auth.instance_url}"
            f"{_DESCRIBE_PATH_TEMPLATE.format(object_name=self._object_name)}"
        )

        response = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=30,
        )
        response.raise_for_status()

        raw: dict[str, Any] = response.json()
        self._cached_fields = [
            self._parse_field(f) for f in raw.get("fields", []) if self._is_usable(f)
        ]

        _logger.info(
            "salesforce_describe_fetched",
            object_name=self._object_name,
            raw_field_count=len(raw.get("fields", [])),
            usable_field_count=len(self._cached_fields),
        )
        return self._cached_fields

    @staticmethod
    def _is_usable(field: dict[str, Any]) -> bool:
        """
        True when the field can be included in a SOQL query.

        Rejects compound address/location types which Salesforce does not allow
        in SOQL SELECT directly (their sub-fields are queryable instead).
        """
        sf_type: str = str(field.get("type", "")).lower()
        return bool(field.get("queryable", False)) and sf_type not in _NON_QUERYABLE_COMPOUND_TYPES

    @staticmethod
    def _parse_field(field: dict[str, Any]) -> SalesforceFieldMetadata:
        """Parse a Salesforce Describe field dict into a typed value object."""
        return SalesforceFieldMetadata(
            name=str(field["name"]),
            data_type=str(field.get("type", "string")),
            is_queryable=bool(field.get("queryable", False)),
            is_custom=bool(field.get("custom", False)),
            is_nullable=bool(field.get("nillable", True)),
            label=str(field.get("label", field["name"])),
            length=field.get("length") or None,
            precision=field.get("precision") or None,
            scale=field.get("scale") or None,
        )

    @staticmethod
    def _apply_field_mode(
        fields: list[SalesforceFieldMetadata],
        field_mode: FieldMode,
        include_fields: list[str],
    ) -> list[SalesforceFieldMetadata]:
        """
        Apply the configured FieldMode filter to the full field list.

        FieldMode.ALL:          All queryable fields (no filtering beyond queryable check).
        FieldMode.STANDARD:     Standard (non-custom) fields only.
        FieldMode.CUSTOM:       Custom (API name ends with __c) fields only.
        FieldMode.INCLUDE_ONLY: Exactly the fields listed in include_fields.
        """
        if field_mode == FieldMode.ALL:
            return fields
        if field_mode == FieldMode.STANDARD:
            return [f for f in fields if not f.is_custom]
        if field_mode == FieldMode.CUSTOM:
            return [f for f in fields if f.is_custom]
        # INCLUDE_ONLY — preserve ordering from include_fields list
        index = {f.name: f for f in fields}
        return [
            index[name] for name in include_fields if name in index and index[name].is_queryable
        ]
