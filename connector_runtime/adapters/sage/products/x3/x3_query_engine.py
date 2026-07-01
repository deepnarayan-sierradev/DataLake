"""
Sage X3 query engine — builds OData v4 query parameters for the X3 REST API.

Implements SageQueryProtocol for Sage X3 Enterprise Management REST API.

X3 REST API query pattern (OData v4):
    GET {base_url}/api/{folder}/{endpoint}
        ?$select=BPCNUM_0,BPCNAM_0,...
        &$filter=MODDAT_0 ge 2026-01-01T00:00:00Z and MODDAT_0 lt 2026-07-01T00:00:00Z
        &$orderby=MODDAT_0 asc
        &$top=1000
        &$skip=0

Pagination:
    - OData $top + $skip offset pagination used by default.
    - When the response contains '@odata.nextLink', that full URL is followed
      directly for the next page (cursor-based continuation).
    - SageConnector._execute_x3_extraction() manages the pagination loop;
      the query engine produces the initial URL parameters only.

Query discriminant:
    query_text is a JSON string containing the key "_x3_odata": true.
    SageConnector.execute_extraction() inspects this key to dispatch the
    correct execution path (OData GET vs Intacct JSON-POST).

Query security (OWASP A03):
    - All field names validated against _SAFE_X3_FIELD_PATTERN before use
      in $select or $filter — no arbitrary string can become a field reference.
    - endpoint (object path) validated against _SAFE_X3_ENDPOINT_PATTERN.
    - Watermark values stored as placeholder markers in the serialised filter
      string and kept in query_parameters.  bind_parameters() validates each
      value as ISO-8601 before substitution, preventing filter injection.
    - Placeholder markers use the format "__X3_{KEY}__" — strings that cannot
      match any valid X3 field value.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Final, Literal

from connector_runtime.interfaces.connector_interface import FieldContract, QueryContract
from contracts.entity_configuration_contract import LoadType
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# Maximum records per X3 OData query page.
# Sage X3 REST API allows up to 1000 records per page; use 1000 as the safe default.
X3_PAGE_SIZE: Final[int] = 1_000

# Discriminant key stored in query_text to identify X3 OData queries.
# SageConnector checks this to select the correct execution path.
# Public (no leading underscore) — imported by sage_connector for dispatch.
X3_ODATA_DISCRIMINANT: Final[str] = "_x3_odata"

# Validates Sage X3 endpoint names: uppercase letters and digits only.
# e.g. "BPCUSTOMER", "BPSUPPLIER", "SORDER", "SINVOICE", "PITM"
_SAFE_X3_ENDPOINT_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Z][A-Z0-9]{1,63}$"
)

# Validates X3 field names (short names).
# X3 fields: uppercase, digits, underscores — e.g. BPCNUM_0, MODDAT_0, CRY_0
# Dot-notation sub-resource fields: XBPADR.BPADES_0, XBPCRIT.CRDLMT_0
_SAFE_X3_FIELD_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[A-Z][A-Z0-9_]{0,63}(\.[A-Z][A-Z0-9_]{0,63})?$"
)

# ISO-8601 UTC pattern used to validate watermark parameter values before
# substituting them into the filter string (injection prevention, OWASP A03).
_ISO8601_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)

# Placeholder markers embedded in the filter string for watermark values.
# Double-underscore delimiters ensure they cannot match any real X3 field value.
_LOWER_BOUND_PLACEHOLDER: Final[str] = "__X3_LOWER_BOUND__"
_UPPER_BOUND_PLACEHOLDER: Final[str] = "__X3_UPPER_BOUND__"


class X3QueryBuildError(Exception):
    """Raised when the X3 OData query cannot be built due to invalid inputs."""


class X3QueryEngine:
    """
    Builds OData v4 query parameters for the Sage X3 REST API.

    Implements SageQueryProtocol (structural typing via Protocol).

    The produced QueryContract.query_text is a JSON string that encodes:
      - "_x3_odata": true  — discriminant for SageConnector execution dispatch
      - "endpoint": str    — validated X3 endpoint name (e.g. "BPCUSTOMER")
      - "select": str      — comma-separated validated field names for $select
      - "filter": str | null — OData $filter expression with placeholder markers
      - "orderby": str     — OData $orderby expression for stable ordering

    Watermark values are in query_parameters and substituted at execution time
    via bind_parameters() after ISO-8601 validation.

    Usage::

        engine = X3QueryEngine(object_path="BPCUSTOMER")
        contract = engine.build(
            field_contract=field_contract,
            load_type=LoadType.INCREMENTAL,
            watermark_field="MODDAT_0",
            watermark_lower="2026-01-01T00:00:00Z",
            watermark_upper="2026-07-01T00:00:00Z",
            extraction_window_days=30,
        )
        # contract.query_text → JSON string with _x3_odata + placeholder markers
        # contract.query_parameters → {"lower_bound": "2026-01-01T...", ...}
    """

    def __init__(self, object_path: str) -> None:
        # For X3, object_path is the OData endpoint name (e.g. "BPCUSTOMER").
        if not _SAFE_X3_ENDPOINT_PATTERN.match(object_path):
            raise X3QueryBuildError(
                f"object_path {object_path!r} does not match the required X3 endpoint pattern. "
                "X3 endpoints are uppercase alphanumeric (e.g. 'BPCUSTOMER', 'SORDER')."
            )
        self._endpoint = object_path

    def build(
        self,
        field_contract: FieldContract,
        load_type: LoadType,
        watermark_field: str | None,
        watermark_lower: str | None,
        watermark_upper: str | None,
        extraction_window_days: int,
    ) -> QueryContract:
        """
        Build a parameterised OData v4 query from the discovered FieldContract.

        For INCREMENTAL loads, watermark_field is mandatory and the filter is
        "{watermark_field} ge __X3_LOWER_BOUND__ and {watermark_field} lt __X3_UPPER_BOUND__".
        For FULL loads, no filter is applied.

        Args:
            field_contract:        Discovered fields (must be non-empty).
            load_type:             FULL or INCREMENTAL.
            watermark_field:       X3 field for incremental window filtering.
            watermark_lower:       ISO-8601 UTC lower bound (inclusive).
            watermark_upper:       ISO-8601 UTC upper bound (exclusive).
            extraction_window_days: Informational; used in log events.

        Returns:
            QueryContract with JSON query_text (with _x3_odata discriminant +
            placeholder markers) and watermark values in query_parameters.

        Raises:
            X3QueryBuildError: validation failure.
        """
        if load_type == LoadType.INCREMENTAL and not watermark_field:
            raise X3QueryBuildError(
                "watermark_field is required for INCREMENTAL load type."
            )

        # Validate and collect field names from the FieldContract.
        field_names: list[str] = []
        for descriptor in field_contract.fields:
            if not _SAFE_X3_FIELD_PATTERN.match(descriptor.name):
                raise X3QueryBuildError(
                    f"Field name {descriptor.name!r} does not match the required "
                    "X3 field name pattern. Possible injection attempt."
                )
            field_names.append(descriptor.name)

        if not field_names:
            raise X3QueryBuildError(
                "FieldContract contains no queryable fields — cannot build query."
            )

        # Build OData $select — comma-separated field list.
        select_str = ",".join(field_names)

        query_parameters: dict[str, Any] = {}
        filter_str: str | None = None
        effective_watermark_field: str | None = None

        if load_type == LoadType.INCREMENTAL and watermark_field:
            if not _SAFE_X3_FIELD_PATTERN.match(watermark_field):
                raise X3QueryBuildError(
                    f"watermark_field {watermark_field!r} does not match the "
                    "required X3 field name pattern."
                )
            # Embed placeholder markers in the filter string — NOT actual values.
            # Values are in query_parameters and substituted at execution time.
            filter_str = (
                f"{watermark_field} ge {_LOWER_BOUND_PLACEHOLDER} "
                f"and {watermark_field} lt {_UPPER_BOUND_PLACEHOLDER}"
            )
            query_parameters["lower_bound"] = watermark_lower
            query_parameters["upper_bound"] = watermark_upper
            effective_watermark_field = watermark_field

        # $orderby on the watermark field if incremental, else first field for stability.
        orderby_field = watermark_field if watermark_field else field_names[0]
        orderby_str = f"{orderby_field} asc"

        query_body: dict[str, Any] = {
            X3_ODATA_DISCRIMINANT: True,   # discriminant for SageConnector dispatch
            "endpoint": self._endpoint,
            "select": select_str,
            "filter": filter_str,           # None for FULL loads
            "orderby": orderby_str,
        }

        _logger.info(
            "sage_x3_query_built",
            source_id=field_contract.source_id,
            entity_id=field_contract.entity_id,
            endpoint=self._endpoint,
            load_type=str(load_type),
            field_count=len(field_names),
            has_watermark=effective_watermark_field is not None,
        )

        return QueryContract(
            source_id=field_contract.source_id,
            entity_id=field_contract.entity_id,
            query_text=json.dumps(query_body),
            query_parameters=query_parameters,
            load_type=load_type,
            watermark_lower=watermark_lower,
            watermark_upper=watermark_upper,
            watermark_field=effective_watermark_field,
            estimated_record_count=None,
        )

    @staticmethod
    def bind_parameters(
        query_body: dict[str, Any],
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Substitute placeholder markers in the OData filter with validated values.

        Replaces __X3_LOWER_BOUND__ and __X3_UPPER_BOUND__ in query_body["filter"]
        with ISO-8601 datetime strings after strict format validation.

        The X3 REST API (OData v4) accepts ISO-8601 timestamps directly in $filter
        expressions (e.g. "MODDAT_0 ge 2026-01-01T00:00:00Z").

        Args:
            query_body:  Deserialised query dict containing placeholder markers.
            parameters:  Dict from QueryContract.query_parameters.

        Returns:
            A new query dict with placeholders replaced by validated values.

        Raises:
            X3QueryBuildError: if a parameter value fails ISO-8601 validation.
        """
        lower = parameters.get("lower_bound")
        upper = parameters.get("upper_bound")

        if lower is not None and not _ISO8601_PATTERN.match(str(lower)):
            raise X3QueryBuildError(
                f"lower_bound parameter value {lower!r} is not a valid ISO-8601 datetime. "
                "Injection-safe substitution requires ISO-8601 values."
            )
        if upper is not None and not _ISO8601_PATTERN.match(str(upper)):
            raise X3QueryBuildError(
                f"upper_bound parameter value {upper!r} is not a valid ISO-8601 datetime. "
                "Injection-safe substitution requires ISO-8601 values."
            )

        bound = copy.deepcopy(query_body)

        if bound.get("filter"):
            f = bound["filter"]
            if lower is not None:
                f = f.replace(_LOWER_BOUND_PLACEHOLDER, str(lower))
            if upper is not None:
                f = f.replace(_UPPER_BOUND_PLACEHOLDER, str(upper))
            bound["filter"] = f

        return bound
