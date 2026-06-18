"""
NetSuite metadata adapter.

Discovers available fields for a NetSuite record type via the REST Metadata
Catalog endpoint.  Applies field mode filtering and excludes system-internal
fields that are not meaningful for business extraction.

No hardcoded field lists — field metadata is fetched from NetSuite at runtime,
satisfying the spec requirement: "Handle custom fields and newly added fields
without code changes."

API used:
  GET https://{account_id}.suitetalk.api.netsuite.com
      /services/rest/record/v1/metadata-catalog/{record_type}

Security (OWASP A03, A09):
  - API call is authenticated with a per-request OAuth 1.0a TBA signature.
  - Credentials never appear in logs or exception messages.
  - Metadata response is never written to durable storage.

Naming per spec: netsuite_metadata_adapter → NetSuiteMetadataAdapter
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import requests

from connector_runtime.adapters.netsuite.netsuite_auth_protocol import NetSuiteAuthProtocol
from connector_runtime.interfaces.connector_interface import FieldContract, FieldDescriptor
from contracts.entity_configuration_contract import FieldMode
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# Base URL template for the NetSuite REST Metadata Catalog endpoint.
# account_id is the NetSuite account identifier (numeric or TSTDRV prefix).
_METADATA_URL_TEMPLATE: Final[str] = (
    "https://{account_id}.suitetalk.api.netsuite.com"
    "/services/rest/record/v1/metadata-catalog/{record_type}"
)

# Field types that are not directly extractable in SuiteQL (compound/internal types).
# SuiteQL does not support querying these column types directly.
_NON_QUERYABLE_FIELD_TYPES: Final[frozenset[str]] = frozenset(
    {
        "SELECT",  # multi-select enum (not directly queryable)
        "MULTISELECT",  # multi-select list
        "DOCUMENT",  # file references
        "SUMMARY",  # roll-up fields
    }
)

# NetSuite internal/system fields that should not appear in extraction payloads.
_SYSTEM_FIELD_PREFIXES: Final[tuple[str, ...]] = ("system_",)


@dataclass(frozen=True)
class NetSuiteFieldMetadata:
    """
    Raw NetSuite field descriptor as returned by the Metadata Catalog API.

    Only the attributes needed for FieldContract construction are captured.
    """

    name: str
    data_type: str  # NetSuite type string, e.g. "STRING", "DATETIME", "INTEGER"
    is_queryable: bool
    is_custom: bool  # True for custom fields (prefixed with "custrecord_" etc.)
    is_nullable: bool  # True if the field can be null
    label: str
    length: int | None
    precision: int | None
    scale: int | None


class NetSuiteMetadataAdapterError(Exception):
    """Raised when field discovery via the Metadata Catalog API fails."""


class NetSuiteMetadataAdapter:
    """
    Discovers queryable fields for a NetSuite record type at runtime.

    One instance per connector instance (one record type per adapter).
    The Metadata Catalog response is cached in-memory per instance to
    avoid redundant API calls within the same extraction run.

    Usage::

        adapter = NetSuiteMetadataAdapter(
            auth_client=auth,
            record_type="customer",
        )
        field_contract = adapter.discover_fields(
            source_id="netsuite",
            entity_id="netsuite-customer",
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
    """

    def __init__(
        self,
        auth_client: NetSuiteAuthProtocol,
        record_type: str,
    ) -> None:
        if not record_type:
            raise ValueError("record_type must not be empty.")
        self._auth = auth_client
        self._record_type = record_type
        self._cached_fields: list[NetSuiteFieldMetadata] | None = None

    def discover_fields(
        self,
        source_id: str,
        entity_id: str,
        field_mode: FieldMode,
        include_fields: list[str],
        exclude_fields: list[str],
    ) -> FieldContract:
        """
        Discover all queryable fields for this record type from the Metadata Catalog.

        Caches the raw metadata response within the instance lifetime.
        Call invalidate_cache() to force a fresh fetch on the next call.

        Args:
            source_id: Platform stable source identifier.
            entity_id: Platform stable entity identifier.
            field_mode: Controls which fields to include (ALL, STANDARD, CUSTOM, INCLUDE_ONLY).
            include_fields: When field_mode=INCLUDE_ONLY, only these field names are returned.
            exclude_fields: Field names to unconditionally exclude from the contract.

        Returns:
            FieldContract with all applicable fields and a computed fingerprint.

        Raises:
            NetSuiteMetadataAdapterError: on API failure or unexpected response format.
        """
        from datetime import UTC, datetime  # local import to avoid circular dependency

        raw_fields = self._fetch_fields()
        filtered = self._apply_field_mode(raw_fields, field_mode, include_fields, exclude_fields)

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
            for f in filtered
            if f.is_queryable
        )

        fingerprint = FieldContract.compute_fingerprint(descriptors)
        contract = FieldContract(
            source_id=source_id,
            entity_id=entity_id,
            fields=descriptors,
            discovery_timestamp=datetime.now(UTC),
            schema_fingerprint=fingerprint,
        )

        _logger.info(
            "netsuite_fields_discovered",
            source_id=source_id,
            entity_id=entity_id,
            record_type=self._record_type,
            field_count=len(descriptors),
            field_mode=str(field_mode),
        )
        return contract

    def invalidate_cache(self) -> None:
        """Force the next discover_fields() call to re-fetch from the Metadata Catalog API."""
        self._cached_fields = None

    # ── Private ────────────────────────────────────────────────────────────────

    def _fetch_fields(self) -> list[NetSuiteFieldMetadata]:
        """
        Fetch field metadata from the Metadata Catalog endpoint.

        The response is cached after the first successful fetch.  The cache
        is per-instance (i.e., per extraction run), preventing stale metadata
        from persisting across runs.

        Raises:
            NetSuiteMetadataAdapterError: on HTTP error or unexpected response structure.
        """
        if self._cached_fields is not None:
            return self._cached_fields

        url = _METADATA_URL_TEMPLATE.format(
            account_id=self._auth.account_id,
            record_type=self._record_type,
        )
        headers = self._auth.get_auth_headers("GET", url)
        headers["Accept"] = "application/json"

        try:
            response = requests.get(url, headers=headers, timeout=30)
        except requests.RequestException as exc:
            raise NetSuiteMetadataAdapterError(
                f"Metadata Catalog request failed for record_type={self._record_type!r}: "
                f"{type(exc).__name__}"
            ) from None

        if not response.ok:
            raise NetSuiteMetadataAdapterError(
                f"Metadata Catalog endpoint returned HTTP {response.status_code} "
                f"for record_type={self._record_type!r}."
            )

        body: dict[str, Any] = response.json()
        raw_fields = self._parse_metadata_response(body)
        self._cached_fields = raw_fields
        return raw_fields

    def _parse_metadata_response(self, body: dict[str, Any]) -> list[NetSuiteFieldMetadata]:
        """
        Parse the Metadata Catalog API response into NetSuiteFieldMetadata objects.

        The Metadata Catalog response schema:
            {
              "properties": {
                "fieldName": {
                  "title": "Field Label",
                  "type": "string|integer|number|boolean",
                  "nullable": true|false,
                  "readOnly": true|false,
                  "x-ns-custom-field": true|false,
                  "maxLength": 300,
                  ...
                }
              }
            }

        Raises:
            NetSuiteMetadataAdapterError: response structure is not as expected.
        """
        properties: dict[str, Any] = body.get("properties", {})
        if not properties:
            raise NetSuiteMetadataAdapterError(
                f"Metadata Catalog response for {self._record_type!r} "
                "contained no 'properties' section."
            )

        fields: list[NetSuiteFieldMetadata] = []
        for field_name, field_def in properties.items():
            # Skip internal system fields.
            if any(field_name.startswith(prefix) for prefix in _SYSTEM_FIELD_PREFIXES):
                continue

            ns_type = str(field_def.get("type", "STRING")).upper()
            is_non_queryable = ns_type in _NON_QUERYABLE_FIELD_TYPES

            fields.append(
                NetSuiteFieldMetadata(
                    name=field_name,
                    data_type=ns_type,
                    is_queryable=not is_non_queryable,
                    is_custom=bool(field_def.get("x-ns-custom-field", False)),
                    is_nullable=bool(field_def.get("nullable", True)),
                    label=str(field_def.get("title", field_name)),
                    length=field_def.get("maxLength"),
                    precision=field_def.get("precision"),
                    scale=field_def.get("scale"),
                )
            )

        return fields

    @staticmethod
    def _apply_field_mode(
        fields: list[NetSuiteFieldMetadata],
        field_mode: FieldMode,
        include_fields: list[str],
        exclude_fields: list[str],
    ) -> list[NetSuiteFieldMetadata]:
        """
        Filter discovered fields according to the configured FieldMode.

        FieldMode semantics:
          ALL          → all queryable fields minus exclude_fields
          STANDARD     → non-custom fields minus exclude_fields
          CUSTOM       → custom fields (x-ns-custom-field=true) minus exclude_fields
          INCLUDE_ONLY → exactly the fields in include_fields (exclude_fields ignored)
        """
        exclude_set = set(exclude_fields)

        if field_mode == FieldMode.INCLUDE_ONLY:
            include_set = set(include_fields)
            return [f for f in fields if f.name in include_set]

        if field_mode == FieldMode.STANDARD:
            result = [f for f in fields if not f.is_custom]
        elif field_mode == FieldMode.CUSTOM:
            result = [f for f in fields if f.is_custom]
        else:
            # FieldMode.ALL
            result = list(fields)

        return [f for f in result if f.name not in exclude_set]
