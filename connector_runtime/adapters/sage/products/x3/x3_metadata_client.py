"""
Sage X3 metadata client — discovers queryable fields via live OData sampling.

Implements SageMetadataProtocol for Sage X3 Enterprise Management REST API.

X3 does not expose a dedicated JSON schema endpoint comparable to Intacct's
/objects/{path} Models endpoint.  This client uses a live sampling strategy:

    1. Request GET {base_url}/{endpoint}?$top=1 to retrieve one record.
    2. Infer field names and types from the response record's JSON keys.
    3. Build a FieldContract from the inferred schema.

Sampling approach trade-offs:
    - Pro: Works with any X3 endpoint without static schema maintenance.
    - Pro: Discovers all fields the X3 API actually returns, including custom
           and extension fields unique to the customer's X3 configuration.
    - Con: Optional fields absent from the first record are not discovered.
           For known X3 objects, the curated static fallback covers this gap.

Static fallback registry:
    For common X3 endpoints (BPCUSTOMER, BPSUPPLIER, SORDER, SINVOICE, PITM),
    a curated static field list is provided.  The client attempts live sampling
    first; if the endpoint returns 0 records, the static fallback is used.
    Unknown endpoints with 0 records raise SageMetadataDeterministicError.

Design:
    - supports_live_discovery = True (live sampling attempted first).
    - Metadata is cached per-instance (per extraction run).
    - Call invalidate_cache() to force re-sampling.

Field type inference mapping (JSON Python type → platform data_type):
    int      → "integer"
    float    → "decimal"
    bool     → "boolean"
    str ISO-8601 → "date"
    str other    → "string"
    None         → "string" (nullable, type unknown — default to string)

Security (OWASP A03):
    - endpoint validated against _SAFE_X3_ENDPOINT_PATTERN before interpolation.
    - The $top=1 response body is never written to durable storage.
    - Auth credentials accessed only via auth_client — never directly.
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
from connector_runtime.adapters.sage.common.sage_errors import (
    SageMetadataDeterministicError,
    SageMetadataError,
    SageMetadataTransientError,
)
from connector_runtime.adapters.sage.products.x3.x3_auth import X3AuthClient
from connector_runtime.interfaces.connector_interface import FieldContract, FieldDescriptor
from contracts.entity_configuration_contract import FieldMode
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# Validates Sage X3 endpoint names — same pattern as X3QueryEngine.
_SAFE_X3_ENDPOINT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Z][A-Z0-9]{1,63}$"
)

# ISO-8601 date/datetime pattern — used to infer "date" type from string values.
_ISO8601_VALUE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2}))?$"
)


@dataclass(frozen=True)
class X3FieldSchema:
    """Raw X3 field descriptor inferred from a sample record or static registry."""

    name: str
    data_type: str   # "string", "integer", "decimal", "boolean", "date"
    is_nullable: bool
    label: str       # Human-readable label; same as name for inferred fields


# ---------------------------------------------------------------------------
# Static fallback schemas for common X3 endpoints
# ---------------------------------------------------------------------------

_X3_STATIC_SCHEMAS: Final[dict[str, list[X3FieldSchema]]] = {
    "BPCUSTOMER": [
        X3FieldSchema("BPCNUM_0",  "string",  False, "Customer Code"),
        X3FieldSchema("BPCNAM_0",  "string",  False, "Customer Name"),
        X3FieldSchema("BCGCOD_0",  "string",  True,  "Customer Category"),
        X3FieldSchema("CRY_0",     "string",  True,  "Country"),
        X3FieldSchema("SALFCY_0",  "string",  True,  "Sales Site"),
        X3FieldSchema("CUR_0",     "string",  True,  "Currency"),
        X3FieldSchema("TEL_0",     "string",  True,  "Telephone"),
        X3FieldSchema("FAX_0",     "string",  True,  "Fax"),
        X3FieldSchema("WEB_0",     "string",  True,  "Website"),
        X3FieldSchema("EACNUM_0",  "string",  True,  "Email"),
        X3FieldSchema("REP_0",     "string",  True,  "State/Region Code"),
        X3FieldSchema("ITMREF_0",  "string",  True,  "Default Product"),
        X3FieldSchema("CREDAT_0",  "date",    True,  "Creation Date"),
        X3FieldSchema("MODDAT_0",  "date",    True,  "Last Modified Date"),
        X3FieldSchema("ENAFLG_0",  "integer", True,  "Active Flag (1=active, 2=inactive)"),
    ],
    "BPSUPPLIER": [
        X3FieldSchema("BPSNUM_0",  "string",  False, "Supplier Code"),
        X3FieldSchema("BPSNAM_0",  "string",  False, "Supplier Name"),
        X3FieldSchema("BCGCOD_0",  "string",  True,  "Supplier Category"),
        X3FieldSchema("CRY_0",     "string",  True,  "Country"),
        X3FieldSchema("CUR_0",     "string",  True,  "Currency"),
        X3FieldSchema("TEL_0",     "string",  True,  "Telephone"),
        X3FieldSchema("EACNUM_0",  "string",  True,  "Email"),
        X3FieldSchema("REP_0",     "string",  True,  "State/Region Code"),
        X3FieldSchema("CREDAT_0",  "date",    True,  "Creation Date"),
        X3FieldSchema("MODDAT_0",  "date",    True,  "Last Modified Date"),
        X3FieldSchema("ENAFLG_0",  "integer", True,  "Active Flag (1=active, 2=inactive)"),
    ],
    "SORDER": [
        X3FieldSchema("NUM_0",     "string",  False, "Sales Order Number"),
        X3FieldSchema("BPCORD_0",  "string",  True,  "Customer Code"),
        X3FieldSchema("BPCNAM_0",  "string",  True,  "Customer Name"),
        X3FieldSchema("ORDDAT_0",  "date",    True,  "Order Date"),
        X3FieldSchema("DLVDAT_0",  "date",    True,  "Delivery Date"),
        X3FieldSchema("CUR_0",     "string",  True,  "Currency"),
        X3FieldSchema("ORDATI_0",  "decimal", True,  "Total Amount Incl. Tax"),
        X3FieldSchema("ORDATEXC_0","decimal", True,  "Total Amount Excl. Tax"),
        X3FieldSchema("STAFCY_0",  "string",  True,  "Status"),
        X3FieldSchema("CREDAT_0",  "date",    True,  "Creation Date"),
        X3FieldSchema("MODDAT_0",  "date",    True,  "Last Modified Date"),
    ],
    "SINVOICE": [
        X3FieldSchema("NUM_0",     "string",  False, "Invoice Number"),
        X3FieldSchema("BPCORD_0",  "string",  True,  "Customer Code"),
        X3FieldSchema("BPCNAM_0",  "string",  True,  "Customer Name"),
        X3FieldSchema("INVDAT_0",  "date",    True,  "Invoice Date"),
        X3FieldSchema("DUDDAT_0",  "date",    True,  "Due Date"),
        X3FieldSchema("CUR_0",     "string",  True,  "Currency"),
        X3FieldSchema("AMTATI_0",  "decimal", True,  "Amount Incl. Tax"),
        X3FieldSchema("AMTNOTATI_0","decimal",True,  "Amount Excl. Tax"),
        X3FieldSchema("CREDATTIM_0","date",   True,  "Creation Datetime"),
        X3FieldSchema("UPDDATTIM_0","date",   True,  "Last Modified Datetime"),
    ],
    "PITM": [
        X3FieldSchema("ITMREF_0",  "string",  False, "Product Reference"),
        X3FieldSchema("ITMDES_0",  "string",  True,  "Product Description"),
        X3FieldSchema("TCLCOD_0",  "string",  True,  "Product Category"),
        X3FieldSchema("ITMSTA_0",  "integer", True,  "Product Status"),
        X3FieldSchema("UOM_0",     "string",  True,  "Unit of Measure"),
        X3FieldSchema("SAUPRI_0",  "decimal", True,  "Sales Price"),
        X3FieldSchema("PURPRI_0",  "decimal", True,  "Purchase Price"),
        X3FieldSchema("CREDAT_0",  "date",    True,  "Creation Date"),
        X3FieldSchema("UPDDAT_0",  "date",    True,  "Last Modified Date"),
    ],
}


class X3MetadataClient:
    """
    Discovers queryable fields for a Sage X3 endpoint via live OData sampling,
    falling back to the curated static schema registry when the endpoint is
    empty or when the live response cannot be parsed.

    Implements SageMetadataProtocol (structural typing via Protocol).

    One instance per connector instance (one endpoint per client).
    The sampled schema is cached in-memory per instance to avoid redundant
    API calls within the same extraction run.

    Usage::

        client = X3MetadataClient(
            auth_client=x3_auth,
            http_client=SageHttpClient(),
            object_path="BPCUSTOMER",
        )
        field_contract = client.discover_fields(
            source_id="sage",
            entity_id="sage-x3-customer",
            object_path="BPCUSTOMER",
            field_mode=FieldMode.ALL,
            include_fields=[],
            exclude_fields=[],
        )
    """

    # This client uses live sampling, supplemented by a static fallback.
    supports_live_discovery: bool = True

    def __init__(
        self,
        auth_client: X3AuthClient,
        http_client: SageHttpClient,
        object_path: str,
    ) -> None:
        if not _SAFE_X3_ENDPOINT_PATTERN.match(object_path):
            raise SageMetadataError(
                f"object_path {object_path!r} does not match the required X3 endpoint "
                "pattern. X3 endpoints are uppercase alphanumeric (e.g. 'BPCUSTOMER')."
            )
        self._auth = auth_client
        self._http = http_client
        self._endpoint = object_path   # For X3, object_path IS the endpoint name
        self._cached_fields: list[X3FieldSchema] | None = None

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
        Discover all queryable fields for this X3 endpoint.

        Attempts live OData sampling first; falls back to the curated static
        registry for known endpoints with no records.

        Returns:
            FieldContract with the applicable fields and a deterministic fingerprint.

        Raises:
            SageMetadataDeterministicError: endpoint not found or not in static registry.
            SageMetadataTransientError: transient HTTP error during sampling.
        """
        raw_fields = self._fetch_fields()
        filtered = self._apply_field_mode(raw_fields, field_mode, include_fields, exclude_fields)

        descriptors = tuple(
            FieldDescriptor(
                name=f.name,
                data_type=f.data_type,
                is_nullable=f.is_nullable,
                is_queryable=True,   # All fields from the sampler are queryable by definition
                length=None,
                is_custom=False,     # X3 has no standard custom-field prefix to detect
                source_label=f.label,
            )
            for f in filtered
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
            "sage_x3_fields_discovered",
            source_id=source_id,
            entity_id=entity_id,
            endpoint=self._endpoint,
            field_count=len(descriptors),
            field_mode=str(field_mode),
        )
        return contract

    def invalidate_cache(self) -> None:
        """Force the next discover_fields() call to re-sample from the X3 API."""
        self._cached_fields = None

    # ── Private ────────────────────────────────────────────────────────────────

    def _fetch_fields(self) -> list[X3FieldSchema]:
        """
        Fetch field schema via live sampling ($top=1), then fall back to the
        curated static registry if the endpoint has no records.

        Raises:
            SageMetadataDeterministicError: endpoint not found; not in static registry.
            SageMetadataTransientError: transient HTTP error.
        """
        if self._cached_fields is not None:
            return self._cached_fields

        # Build sampling URL: {base_url}/{endpoint}?$top=1
        sample_url = f"{self._auth.base_url}/{self._endpoint}"
        headers = self._auth.build_auth_headers()

        try:
            response_body = self._http.get(
                url=sample_url,
                headers=headers,
                params={"$top": "1"},
            )
        except SageObjectNotFoundError:
            # Endpoint may not exist — check static registry before failing.
            static = _X3_STATIC_SCHEMAS.get(self._endpoint)
            if static:
                _logger.info(
                    "sage_x3_metadata_fallback_static",
                    endpoint=self._endpoint,
                    reason="endpoint_not_found_in_api",
                )
                self._cached_fields = static
                return static
            raise SageMetadataDeterministicError(
                f"X3 endpoint {self._endpoint!r} was not found and has no static "
                "schema fallback. Verify the endpoint in connector_params.object_path."
            ) from None
        except SageHttpError as exc:
            raise SageMetadataTransientError(
                f"X3 sampling request failed for endpoint {self._endpoint!r}: "
                f"{type(exc).__name__}"
            ) from None

        value_list: list[dict[str, Any]] = response_body.get("value", [])

        if value_list:
            # Infer schema from the first record's keys.
            inferred = _infer_schema_from_record(value_list[0])
            self._cached_fields = inferred
            _logger.info(
                "sage_x3_metadata_live_sampled",
                endpoint=self._endpoint,
                inferred_field_count=len(inferred),
            )
            return inferred

        # No records — try the static fallback.
        static = _X3_STATIC_SCHEMAS.get(self._endpoint)
        if static:
            _logger.info(
                "sage_x3_metadata_fallback_static",
                endpoint=self._endpoint,
                reason="empty_endpoint",
            )
            self._cached_fields = static
            return static

        raise SageMetadataDeterministicError(
            f"X3 endpoint {self._endpoint!r} returned no records and has no static "
            "schema fallback.  Add a static schema to _X3_STATIC_SCHEMAS or populate "
            "the endpoint with at least one record to enable live schema inference."
        )

    def _apply_field_mode(
        self,
        raw_fields: list[X3FieldSchema],
        field_mode: FieldMode,
        include_fields: list[str],
        exclude_fields: list[str],
    ) -> list[X3FieldSchema]:
        """
        Apply field_mode, include_fields, and exclude_fields filters.

        FieldMode.ALL and FieldMode.STANDARD include everything (X3 has no
        custom-field concept in the same sense as Intacct/Salesforce).
        FieldMode.CUSTOM returns an empty list (X3 extension fields are not
        differentiated in OData responses).
        FieldMode.INCLUDE_ONLY returns only the fields listed in include_fields.
        """
        if field_mode == FieldMode.CUSTOM:
            # X3 OData responses don't segregate standard vs custom.
            return []

        if field_mode == FieldMode.INCLUDE_ONLY:
            include_set = frozenset(include_fields)
            filtered = [f for f in raw_fields if f.name in include_set]
        else:
            # ALL or STANDARD — include everything.
            filtered = list(raw_fields)

        exclude_set = frozenset(exclude_fields)
        return [f for f in filtered if f.name not in exclude_set]


# ---------------------------------------------------------------------------
# Schema inference helpers
# ---------------------------------------------------------------------------

def _infer_type(value: Any) -> str:
    """Infer the platform data_type string from a JSON Python value."""
    if value is None:
        return "string"  # nullable; default to string
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "decimal"
    if isinstance(value, str):
        if _ISO8601_VALUE_PATTERN.match(value):
            return "date"
        return "string"
    return "string"  # dict/list sub-objects: treated as string


def _infer_schema_from_record(record: dict[str, Any]) -> list[X3FieldSchema]:
    """
    Build a list of X3FieldSchema from the keys and values of a sample record.

    Skips OData metadata keys (prefixed with "@odata." or "@").
    """
    fields: list[X3FieldSchema] = []
    for key, value in record.items():
        if key.startswith("@"):
            continue  # Skip OData metadata annotations
        data_type = _infer_type(value)
        fields.append(
            X3FieldSchema(
                name=key,
                data_type=data_type,
                is_nullable=(value is None),
                label=key,
            )
        )
    return fields
