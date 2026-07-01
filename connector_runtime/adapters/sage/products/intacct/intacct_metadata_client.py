"""
Sage Intacct metadata client — discovers queryable fields via the Models endpoint.

Implements SageMetadataProtocol for Sage Intacct REST API.

Intacct REST API Models endpoint:
    GET {base_url}/objects/{object_path}

The endpoint returns a schema definition describing all fields available on the
object, their data types, whether they are queryable, and whether they are custom
fields.  This adapter parses the response and returns a platform FieldContract.

Design:
    - Fields are discovered at runtime — no hardcoded field lists.
    - Metadata is cached per-instance (per extraction run) to avoid redundant
      API calls within the same run.  Call invalidate_cache() to force re-fetch.
    - Custom fields (nsp:: prefix in Intacct) are exposed with is_custom=True,
      consistent with the NetSuite metadata adapter convention.
    - Non-queryable fields (writeOnly, deprecated) are filtered out before the
      FieldContract is built.

Security (OWASP A03):
    - object_path is validated against _SAFE_OBJECT_PATH_PATTERN before being
      interpolated into the metadata URL to prevent path traversal.
    - The metadata response is never written to durable storage.
    - Auth credentials are never accessed directly — only auth_client is used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

from connector_runtime.adapters.sage.common.sage_http_client import (
    SageHttpClient,
    SageHttpError,
    SageObjectNotFoundError,
)
from connector_runtime.adapters.sage.products.intacct.intacct_auth import IntacctAuthClient
from connector_runtime.interfaces.connector_interface import FieldContract, FieldDescriptor
from contracts.entity_configuration_contract import FieldMode
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# Validates Intacct object paths like "accounts-receivable/customer" and
# derived paths like "order-entry/document::Contract Invoice".
# Prevents path traversal (no "..", no leading slash, no query string chars).
_SAFE_OBJECT_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-z][a-z0-9\-]+/[a-z][a-z0-9\-]+(::[A-Za-z0-9 ]+)?$"
)

# Intacct custom field name prefix.
_CUSTOM_FIELD_PREFIX: Final[str] = "nsp::"

# Field attribute names that mark a field as non-queryable in the Models response.
_NON_QUERYABLE_ATTRIBUTES: Final[frozenset[str]] = frozenset({"writeOnly", "deprecated"})


# These shared exception types are defined in common/sage_errors.py and
# re-exported here so existing imports from this module keep working.
from connector_runtime.adapters.sage.common.sage_errors import (  # noqa: F401
    SageMetadataDeterministicError,
    SageMetadataError,
    SageMetadataTransientError,
)


@dataclass(frozen=True)
class IntacctFieldSchema:
    """
    Raw Intacct field descriptor as returned by the Models endpoint.
    Only the attributes needed for FieldContract construction are captured.
    """

    name: str
    data_type: str  # Intacct type string: "string", "integer", "number", "boolean", "date", etc.
    is_queryable: bool
    is_custom: bool  # True for fields with the nsp:: prefix
    is_nullable: bool
    label: str
    max_length: int | None


class IntacctMetadataClient:
    """
    Discovers queryable fields for a Sage Intacct object via the Models endpoint.

    Implements SageMetadataProtocol (structural typing via Protocol).

    One instance per connector instance (one object path per client).
    The Models API response is cached in-memory per instance to avoid
    redundant API calls within the same extraction run.

    Usage::

        client = IntacctMetadataClient(
            auth_client=intacct_auth,
            http_client=SageHttpClient(),
            object_path="accounts-receivable/customer",
        )
        field_contract = client.discover_fields(
            source_id="sage",
            entity_id="sage-intacct-customer",
            object_path="accounts-receivable/customer",
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
    """

    # This client queries the live Intacct Models endpoint.
    supports_live_discovery: bool = True

    def __init__(
        self,
        auth_client: IntacctAuthClient,
        http_client: SageHttpClient,
        object_path: str,
    ) -> None:
        if not _SAFE_OBJECT_PATH_PATTERN.match(object_path):
            raise SageMetadataError(
                f"object_path {object_path!r} does not match the required safe pattern. "
                "Use the Intacct module/object-name format (e.g. 'accounts-receivable/customer')."
            )
        self._auth = auth_client
        self._http = http_client
        self._object_path = object_path
        self._cached_fields: list[IntacctFieldSchema] | None = None

    def discover_fields(
        self,
        source_id: str,
        entity_id: str,
        object_path: str,
        field_mode: FieldMode,
        include_fields: list[str],
        exclude_fields: list[str],
    ) -> FieldContract:
        """
        Discover all queryable fields for this Intacct object via the Models endpoint.

        New fields added to the Intacct object appear automatically in the next
        run without any code changes — dynamic discovery at runtime (platform spec).

        Args:
            source_id:      Platform stable source identifier.
            entity_id:      Platform stable entity identifier.
            object_path:    Intacct object path (validated on construction).
            field_mode:     Controls which fields to include (ALL, STANDARD, CUSTOM, INCLUDE_ONLY).
            include_fields: When field_mode=INCLUDE_ONLY, only these field names are returned.
            exclude_fields: Field names to unconditionally exclude.

        Returns:
            FieldContract with the applicable fields and a deterministic fingerprint.

        Raises:
            SageMetadataError: on API failure, unrecognised object path, or response parse error.
        """
        raw_fields = self._fetch_fields()
        filtered = self._apply_field_mode(raw_fields, field_mode, include_fields, exclude_fields)

        descriptors = tuple(
            FieldDescriptor(
                name=f.name,
                data_type=f.data_type,
                is_nullable=f.is_nullable,
                is_queryable=f.is_queryable,
                length=f.max_length,
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
            "sage_intacct_fields_discovered",
            source_id=source_id,
            entity_id=entity_id,
            object_path=self._object_path,
            field_count=len(descriptors),
            field_mode=str(field_mode),
        )
        return contract

    def invalidate_cache(self) -> None:
        """Force the next discover_fields() call to re-fetch from the Models endpoint."""
        self._cached_fields = None

    # ── Private ────────────────────────────────────────────────────────────────

    def _fetch_fields(self) -> list[IntacctFieldSchema]:
        """
        Fetch field schema from the Intacct Models endpoint.

        The response is cached after the first successful fetch.
        Cache is per-instance (per extraction run) — stale metadata cannot
        persist across runs.

        Raises:
            SageMetadataError: on HTTP error or unexpected response structure.
        """
        if self._cached_fields is not None:
            return self._cached_fields

        # Build the models URL: {base_url}/objects/{object_path}
        # object_path is validated in __init__ — safe to interpolate.
        models_url = f"{self._auth.base_url}/objects/{self._object_path}"
        headers = self._auth.build_auth_headers()

        try:
            response_body = self._http.get(url=models_url, headers=headers)
        except SageObjectNotFoundError:
            raise SageMetadataDeterministicError(
                f"Intacct object path {self._object_path!r} was not found. "
                "Verify the object path in connector_params.object_path."
            ) from None
        except SageHttpError as exc:
            raise SageMetadataTransientError(
                f"Models endpoint request failed for {self._object_path!r}: "
                f"{type(exc).__name__}"
            ) from None

        parsed = self._parse_models_response(response_body)
        self._cached_fields = parsed
        return parsed

    def _parse_models_response(
        self, body: dict[str, Any]
    ) -> list[IntacctFieldSchema]:
        """
        Parse the Intacct Models API response into IntacctFieldSchema objects.

        The Models endpoint returns a schema definition.  The exact response
        structure for Intacct REST API v1:
            {
              "ia::result": {
                "object": "accounts-receivable/customer",
                "fields": [
                  {
                    "name": "key",
                    "label": "Record Number",
                    "description": "...",
                    "type": "integer",
                    "queryable": true,
                    "nullable": false,
                    "deprecated": false,
                    "writeOnly": false,
                    "custom": false,
                    "maxLength": null
                  },
                  ...
                ]
              }
            }

        Raises:
            SageMetadataError: response structure is not as expected.
        """
        result = body.get("ia::result", body)  # support both wrapped and unwrapped responses

        fields_raw: list[dict[str, Any]] = result.get("fields", [])
        if not fields_raw:
            raise SageMetadataError(
                f"Models endpoint response for {self._object_path!r} "
                "contained no 'fields' section or an empty field list."
            )

        schemas: list[IntacctFieldSchema] = []
        for field_def in fields_raw:
            name = field_def.get("name", "")
            if not name:
                continue  # Skip unnamed fields — defensive; should not occur.

            # A field is non-queryable if writeOnly or deprecated.
            is_write_only = bool(field_def.get("writeOnly", False))
            is_deprecated = bool(field_def.get("deprecated", False))
            is_queryable = field_def.get("queryable", True) and not is_write_only and not is_deprecated

            schemas.append(
                IntacctFieldSchema(
                    name=name,
                    data_type=field_def.get("type", "string"),
                    is_queryable=is_queryable,
                    is_custom=bool(field_def.get("custom", False))
                    or name.startswith(_CUSTOM_FIELD_PREFIX),
                    is_nullable=bool(field_def.get("nullable", True)),
                    label=field_def.get("label", name),
                    max_length=field_def.get("maxLength"),
                )
            )

        return schemas

    @staticmethod
    def _apply_field_mode(
        fields: list[IntacctFieldSchema],
        field_mode: FieldMode,
        include_fields: list[str],
        exclude_fields: list[str],
    ) -> list[IntacctFieldSchema]:
        """
        Filter fields according to the requested field_mode and exclusion list.

        FieldMode semantics:
            ALL         — all queryable fields (standard + custom)
            STANDARD    — non-custom fields only
            CUSTOM      — custom fields only
            INCLUDE_ONLY — exactly the fields in include_fields (no discovery filter)
        """
        exclude_set = frozenset(exclude_fields)

        if field_mode == FieldMode.INCLUDE_ONLY:
            include_set = frozenset(include_fields)
            return [f for f in fields if f.name in include_set and f.name not in exclude_set]

        if field_mode == FieldMode.STANDARD:
            return [f for f in fields if not f.is_custom and f.name not in exclude_set]

        if field_mode == FieldMode.CUSTOM:
            return [f for f in fields if f.is_custom and f.name not in exclude_set]

        # FieldMode.ALL — return everything not excluded
        return [f for f in fields if f.name not in exclude_set]
