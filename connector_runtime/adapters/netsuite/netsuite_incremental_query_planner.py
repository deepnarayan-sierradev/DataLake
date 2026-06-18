"""
NetSuite incremental query planner.

Builds parameterized SuiteQL queries from a FieldContract, applying
watermark window filtering for incremental loads.

SuiteQL is NetSuite's SQL-like query language executed via:
  POST https://{account_id}.suitetalk.api.netsuite.com
       /services/rest/query/v1/suiteql

Security (OWASP A03):
  - Record type and field names are validated against a strict regex pattern
    before insertion into the query text.
  - Watermark values are stored as query_parameters (dict) and bound via
    named placeholders (:lower_bound / :upper_bound) — they are NEVER
    interpolated by string formatting.
  - The NetSuiteIncrementalQueryPlanner.bind_parameters() method substitutes
    only ISO-8601 date-time values — validated by regex before substitution.

Naming per spec: netsuite_incremental_query_planner → NetSuiteIncrementalQueryPlanner
"""

from __future__ import annotations

import re
from typing import Any, Final

from connector_runtime.interfaces.connector_interface import FieldContract, QueryContract
from contracts.entity_configuration_contract import LoadType
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# SuiteQL record type and field name pattern.
# NetSuite record types follow standard SQL identifier conventions.
_IDENTIFIER_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,254}$")

# ISO-8601 UTC date-time pattern used for watermark parameter validation.
# Only these values are accepted as watermark bounds to prevent injection.
_ISO8601_UTC_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z?([+-]\d{2}:\d{2})?$"
)

# SuiteQL endpoint path (relative; host is account-specific).
_SUITEQL_PATH: Final[str] = "/services/rest/query/v1/suiteql"

# Maximum rows per SuiteQL page (NetSuite limit).
_PAGE_SIZE: Final[int] = 1_000


class NetSuiteIncrementalQueryPlannerError(Exception):
    """Raised when query construction fails due to invalid inputs."""


class NetSuiteIncrementalQueryPlanner:
    """
    Builds parameterized SuiteQL queries for NetSuite record extraction.

    Supports both FULL and INCREMENTAL load types.  For INCREMENTAL loads,
    watermark bounds are added as named parameters (:lower_bound / :upper_bound)
    and stored in query_parameters — never interpolated into the query text.

    The planner validates all field names and the record type name before
    constructing the query to prevent SQL injection.

    Usage::

        planner = NetSuiteIncrementalQueryPlanner(record_type="customer")
        query = planner.build(
            field_contract=field_contract,
            load_type=LoadType.INCREMENTAL,
            watermark_field="lastmodifieddate",
            watermark_lower="2026-01-01T00:00:00Z",
            watermark_upper="2026-06-12T00:00:00Z",
            extraction_window_days=7,
        )
        # query.query_text → "SELECT id, companyname, lastmodifieddate FROM customer
        #                      WHERE lastmodifieddate >= :lower_bound
        #                        AND lastmodifieddate < :upper_bound"
        # query.query_parameters → {"lower_bound": "2026-01-01T00:00:00Z", ...}
    """

    def __init__(self, record_type: str) -> None:
        if not _IDENTIFIER_PATTERN.match(record_type):
            raise NetSuiteIncrementalQueryPlannerError(
                f"record_type {record_type!r} does not match the required pattern "
                f"{_IDENTIFIER_PATTERN.pattern!r}."
            )
        self._record_type = record_type

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
        Build a parameterized SuiteQL query from the FieldContract.

        For INCREMENTAL loads, the watermark_field must be provided.
        For FULL loads, the entire table is scanned with no time filter.

        Args:
            field_contract: Discovered fields — used to build the SELECT clause.
            load_type: FULL or INCREMENTAL.
            watermark_field: Source column name for the watermark (e.g. "lastmodifieddate").
            watermark_lower: ISO-8601 lower bound (inclusive) for incremental window.
            watermark_upper: ISO-8601 upper bound (exclusive) for incremental window.
            extraction_window_days: Informational; not used in SuiteQL query itself.

        Returns:
            QueryContract with parameterized query_text and query_parameters dict.

        Raises:
            NetSuiteIncrementalQueryPlannerError: on validation failure.
        """
        if load_type == LoadType.INCREMENTAL and not watermark_field:
            raise NetSuiteIncrementalQueryPlannerError(
                "watermark_field is required for INCREMENTAL load type."
            )

        # Validate and extract field names from the contract.
        field_names: list[str] = []
        for descriptor in field_contract.fields:
            if not _IDENTIFIER_PATTERN.match(descriptor.name):
                raise NetSuiteIncrementalQueryPlannerError(
                    f"Field name {descriptor.name!r} does not match the required "
                    f"identifier pattern {_IDENTIFIER_PATTERN.pattern!r}."
                )
            field_names.append(descriptor.name)

        if not field_names:
            raise NetSuiteIncrementalQueryPlannerError(
                "FieldContract contains no queryable fields — cannot build SuiteQL query."
            )

        select_clause = ", ".join(field_names)
        # field names and record_type are both validated against _IDENTIFIER_PATTERN
        # before this point — no user-controlled input can reach this f-string.
        query_text = f"SELECT {select_clause} FROM {self._record_type}"  # noqa: S608
        query_parameters: dict[str, Any] = {}
        effective_watermark_field: str | None = None

        if load_type == LoadType.INCREMENTAL and watermark_field:
            if not _IDENTIFIER_PATTERN.match(watermark_field):
                raise NetSuiteIncrementalQueryPlannerError(
                    f"watermark_field {watermark_field!r} does not match identifier pattern."
                )
            query_text = (
                f"{query_text}"
                f" WHERE {watermark_field} >= :lower_bound"
                f" AND {watermark_field} < :upper_bound"
            )
            query_parameters["lower_bound"] = watermark_lower
            query_parameters["upper_bound"] = watermark_upper
            effective_watermark_field = watermark_field

        _logger.info(
            "netsuite_query_built",
            source_id=field_contract.source_id,
            entity_id=field_contract.entity_id,
            load_type=str(load_type),
            field_count=len(field_names),
            record_type=self._record_type,
        )

        return QueryContract(
            source_id=field_contract.source_id,
            entity_id=field_contract.entity_id,
            query_text=query_text,
            query_parameters=query_parameters,
            load_type=load_type,
            watermark_lower=watermark_lower,
            watermark_upper=watermark_upper,
            watermark_field=effective_watermark_field,
            estimated_record_count=None,
        )

    @staticmethod
    def bind_parameters(query_text: str, parameters: dict[str, Any]) -> str:
        """
        Substitute :param_name placeholders with validated ISO-8601 values.

        This method is called by the NetSuiteConnector just before submitting
        the SuiteQL request — the SuiteQL REST API does not support server-side
        parameter binding, so client-side substitution is required.

        Only ISO-8601 date-time values are accepted as parameter values.
        Any value that fails the ISO-8601 regex causes an immediate error,
        preventing injection of arbitrary SQL fragments.

        Args:
            query_text: Parameterized SuiteQL string with :param_name placeholders.
            parameters: Dict of parameter name → ISO-8601 string values.

        Returns:
            Query string with all placeholders substituted.

        Raises:
            NetSuiteIncrementalQueryPlannerError: if a value fails ISO-8601 validation.
        """
        bound = query_text
        for param_name, param_value in parameters.items():
            value_str = str(param_value)
            if not _ISO8601_UTC_PATTERN.match(value_str):
                raise NetSuiteIncrementalQueryPlannerError(
                    f"Parameter {param_name!r} value {value_str!r} is not a valid "
                    "ISO-8601 datetime — will not substitute to prevent SQL injection."
                )
            # Use a regex word-boundary lookahead so that `:lower_bound` does not
            # accidentally match inside `:lower_bound_extended` (prefix collision safety).
            bound = re.sub(
                rf":{re.escape(param_name)}(?!\w)",
                f"'{value_str}'",
                bound,
            )
        return bound
