"""
Sage Intacct query engine — builds JSON DSL query bodies for the Intacct REST API.

Implements SageQueryProtocol for Sage Intacct REST API.

Intacct REST query service endpoint:
    POST {base_url}/services/v1/query
    Body: {
        "object":   "accounts-receivable/customer",
        "fields":   ["key", "id", "name", ...],
        "filters":  [{"$gte": {"auditInfo.modifiedAt": "2026-01-01T00:00:00Z"}}],
        "orderBy":  [{"key": "asc"}],
        "start":    1,
        "size":     4000
    }

Query security (OWASP A03):
    - All field names validated against _SAFE_FIELD_NAME_PATTERN before inclusion
      in the query body.  Arbitrary strings cannot become query field references.
    - object_path validated against _SAFE_OBJECT_PATH_PATTERN before use.
    - Watermark values stored as placeholders in query_text (serialised JSON) and
      kept separately in query_parameters.  bind_parameters() validates each value
      as ISO-8601 before substituting into the query body — preventing injection of
      arbitrary filter values.
    - Placeholder markers use the format "__SAGE_{KEY}__" — unambiguous strings
      that cannot match any legitimate Intacct field value.

Pagination:
    - SageConnector.execute_extraction() manages the start/size/next cursor loop.
    - The query engine produces the template query; start and size are injected
      by the connector at execution time, NOT stored in QueryContract.query_text.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

from connector_runtime.interfaces.connector_interface import FieldContract, QueryContract
from contracts.entity_configuration_contract import LoadType
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# Maximum records per Intacct REST query page (Sage hard limit).
PAGE_SIZE: Final[int] = 4_000

# Validated Intacct object path pattern.
_SAFE_OBJECT_PATH_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-z][a-z0-9\-]+/[a-z][a-z0-9\-]+(::[A-Za-z0-9 ]+)?$"
)

# Validates Intacct field names including:
#   - Simple names:        "key", "id", "name"
#   - Dot-notation:        "primaryContact.name", "auditInfo.modifiedAt"
#   - Custom fields:       "nsp::CUSTOM_FIELD_NAME"
_SAFE_FIELD_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-zA-Z][a-zA-Z0-9_]*(\.[a-zA-Z][a-zA-Z0-9_]*)*(::([A-Z][A-Z0-9_]*))?$"
)

# ISO-8601 UTC pattern used to validate watermark parameter values before
# substituting them into the query body (injection prevention).
_ISO8601_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)

# Placeholder markers embedded in query_text for watermark values.
# Double-underscore delimiters ensure they cannot match real field values.
_LOWER_BOUND_PLACEHOLDER: Final[str] = "__SAGE_LOWER_BOUND__"
_UPPER_BOUND_PLACEHOLDER: Final[str] = "__SAGE_UPPER_BOUND__"


# SageQueryBuildError is defined in common/sage_errors.py and re-exported here
# so existing imports from this module keep working.
from connector_runtime.adapters.sage.common.sage_errors import SageQueryBuildError  # noqa: F401


class IntacctQueryEngine:
    """
    Builds parameterised Intacct REST JSON DSL query bodies.

    Implements SageQueryProtocol (structural typing via Protocol).

    The produced QueryContract.query_text is a JSON string representing the
    query body with __SAGE_LOWER_BOUND__ and __SAGE_UPPER_BOUND__ placeholder
    markers for watermark values.  The actual values are in query_parameters
    and are substituted by bind_parameters() at execution time after ISO-8601
    validation.

    Usage::

        engine = IntacctQueryEngine(object_path="accounts-receivable/customer")
        contract = engine.build(
            field_contract=field_contract,
            load_type=LoadType.INCREMENTAL,
            watermark_field="auditInfo.modifiedAt",
            watermark_lower="2026-01-01T00:00:00Z",
            watermark_upper="2026-07-01T00:00:00Z",
            extraction_window_days=30,
        )
        # contract.query_text → JSON string with placeholder markers
        # contract.query_parameters → {"lower_bound": "2026-01-01T...", "upper_bound": "..."}
    """

    def __init__(self, object_path: str) -> None:
        if not _SAFE_OBJECT_PATH_PATTERN.match(object_path):
            raise SageQueryBuildError(
                f"object_path {object_path!r} does not match the required safe pattern. "
                "Use the Intacct module/object-name format (e.g. 'accounts-receivable/customer')."
            )
        self._object_path = object_path

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
        Build a parameterised Intacct JSON DSL query from the FieldContract.

        For INCREMENTAL loads, watermark_field is mandatory and the filter is
        applied as $gte lower bound and $lt upper bound on that field.
        For FULL loads, no filter is applied — the entire object is extracted.

        Args:
            field_contract:        Discovered fields (must be non-empty).
            load_type:             FULL or INCREMENTAL.
            watermark_field:       Intacct field name for incremental window filtering.
            watermark_lower:       ISO-8601 UTC lower bound (inclusive).
            watermark_upper:       ISO-8601 UTC upper bound (exclusive).
            extraction_window_days: Informational only; used in log events.

        Returns:
            QueryContract with JSON query_text (placeholder markers) and
            query_parameters with watermark values.

        Raises:
            SageQueryBuildError: on validation failure.
        """
        if load_type == LoadType.INCREMENTAL and not watermark_field:
            raise SageQueryBuildError(
                "watermark_field is required for INCREMENTAL load type."
            )

        # Validate and collect field names from the FieldContract.
        field_names: list[str] = []
        for descriptor in field_contract.fields:
            if not _SAFE_FIELD_NAME_PATTERN.match(descriptor.name):
                raise SageQueryBuildError(
                    f"Field name {descriptor.name!r} does not match the required "
                    "Intacct field name pattern. Possible injection attempt."
                )
            field_names.append(descriptor.name)

        if not field_names:
            raise SageQueryBuildError(
                "FieldContract contains no queryable fields — cannot build query."
            )

        # Always include "key" as the ordering anchor for stable pagination.
        # Insert only if not already present in the discovered fields.
        if "key" not in field_names:
            field_names = ["key", *field_names]

        query_body: dict[str, Any] = {
            "object": self._object_path,
            "fields": field_names,
            "orderBy": [{"key": "asc"}],
        }

        query_parameters: dict[str, Any] = {}
        effective_watermark_field: str | None = None

        if load_type == LoadType.INCREMENTAL and watermark_field:
            if not _SAFE_FIELD_NAME_PATTERN.match(watermark_field):
                raise SageQueryBuildError(
                    f"watermark_field {watermark_field!r} does not match the "
                    "required Intacct field name pattern."
                )
            # Embed placeholder markers — NOT the actual values.
            # Actual values are in query_parameters and substituted at execution time.
            query_body["filters"] = [
                {"$gte": {watermark_field: _LOWER_BOUND_PLACEHOLDER}},
                {"$lt": {watermark_field: _UPPER_BOUND_PLACEHOLDER}},
            ]
            query_parameters["lower_bound"] = watermark_lower
            query_parameters["upper_bound"] = watermark_upper
            effective_watermark_field = watermark_field

        _logger.info(
            "sage_intacct_query_built",
            source_id=field_contract.source_id,
            entity_id=field_contract.entity_id,
            object_path=self._object_path,
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
        Substitute placeholder markers in the query body with validated values.

        Walks the filters array and replaces any string value equal to a known
        placeholder marker with the corresponding validated parameter value.
        All parameter values are validated as ISO-8601 before substitution —
        arbitrary strings will raise SageQueryBuildError (injection prevention).

        Args:
            query_body:  Deserialised query dict (from JSON.loads on query_text).
            parameters:  Dict from QueryContract.query_parameters.

        Returns:
            A new query dict with placeholder markers replaced by actual values.

        Raises:
            SageQueryBuildError: if a parameter value fails ISO-8601 validation.
        """
        lower = parameters.get("lower_bound")
        upper = parameters.get("upper_bound")

        if lower is not None and not _ISO8601_PATTERN.match(str(lower)):
            raise SageQueryBuildError(
                f"lower_bound parameter value {lower!r} is not a valid ISO-8601 datetime. "
                "Injection-safe substitution requires ISO-8601 values."
            )
        if upper is not None and not _ISO8601_PATTERN.match(str(upper)):
            raise SageQueryBuildError(
                f"upper_bound parameter value {upper!r} is not a valid ISO-8601 datetime. "
                "Injection-safe substitution requires ISO-8601 values."
            )

        # Deep copy the query body and replace placeholder strings in filters.
        import copy  # local import to keep module-level imports lean
        bound = copy.deepcopy(query_body)

        filters: list[dict[str, Any]] = bound.get("filters", [])
        for filter_clause in filters:
            for operator, condition in filter_clause.items():
                for field_name, value in condition.items():
                    if value == _LOWER_BOUND_PLACEHOLDER and lower is not None:
                        condition[field_name] = lower
                    elif value == _UPPER_BOUND_PLACEHOLDER and upper is not None:
                        condition[field_name] = upper

        return bound
