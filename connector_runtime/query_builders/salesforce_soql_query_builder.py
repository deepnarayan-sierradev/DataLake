"""
Salesforce SOQL query builder.

Builds fully parameterized SOQL queries from a FieldContract and entity
configuration.  No field lists are hardcoded — all fields come from
metadata discovery at runtime.

Security (OWASP A05 — Injection):
  - Watermark bounds are passed as QueryContract.query_parameters, NEVER
    interpolated into query_text strings.
  - Object name is validated against a strict allowlist pattern before
    inclusion in the query string.
  - Field names are validated against the discovered FieldContract — no
    caller-controlled strings reach the query text directly.

Naming per spec: salesforce_soql_query_builder
"""

from __future__ import annotations

import re
from typing import Final

from connector_runtime.interfaces.connector_interface import FieldContract, QueryContract
from contracts.entity_configuration_contract import LoadType
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# Salesforce object names: letters, digits, underscores, may end with __c/__e etc.
# Validated before inclusion in query_text to prevent injection via config.
_OBJECT_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,254}$")

# Salesforce field names follow the same pattern.
_FIELD_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,254}$")


class SalesforceSoqlQueryBuilderError(Exception):
    """Raised when a safe SOQL query cannot be constructed from the given inputs."""


class SalesforceSoqlQueryBuilder:
    """
    Constructs parameterized SOQL queries for a specific Salesforce object.

    One instance per entity.  The object_name is validated at construction
    time.  Fields are validated against the FieldContract at build time to
    ensure no caller-injected strings reach the query text.

    Usage::

        builder = SalesforceSoqlQueryBuilder(object_name="Account")
        query = builder.build(
            field_contract=contract,
            load_type=LoadType.INCREMENTAL,
            watermark_field="SystemModstamp",
            watermark_lower="2026-06-01T00:00:00+00:00",
            watermark_upper="2026-06-02T00:00:00+00:00",
            extraction_window_days=1,
        )
        # query.query_text  => "SELECT ... FROM Account WHERE ..."
        # query.query_parameters  => {"lower_bound": "...", "upper_bound": "..."}
    """

    def __init__(self, object_name: str) -> None:
        if not _OBJECT_NAME_PATTERN.match(object_name):
            raise SalesforceSoqlQueryBuilderError(
                f"object_name {object_name!r} contains characters not permitted "
                "in a Salesforce API name.  Use only letters, digits, and underscores."
            )
        self._object_name = object_name

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
        Build a parameterized SOQL QueryContract.

        For INCREMENTAL loads, the watermark filter is:
            WHERE {watermark_field} >= :lower_bound AND {watermark_field} < :upper_bound

        Watermark values are placed in query_parameters — NOT interpolated into
        query_text.  The Bulk API job controller binds them at query submission.

        For FULL loads, no WHERE clause is added (full table scan).

        Raises:
            SalesforceSoqlQueryBuilderError: if watermark inputs are invalid for
                incremental load, or if field names fail validation.
        """
        field_list = self._build_field_list(field_contract)
        params: dict[str, str] = {}

        if load_type == LoadType.INCREMENTAL:
            if not watermark_field:
                raise SalesforceSoqlQueryBuilderError(
                    "watermark_field is required for INCREMENTAL load type."
                )
            if not watermark_lower or not watermark_upper:
                raise SalesforceSoqlQueryBuilderError(
                    "watermark_lower and watermark_upper are required for INCREMENTAL load."
                )
            if not _FIELD_NAME_PATTERN.match(watermark_field):
                raise SalesforceSoqlQueryBuilderError(
                    f"watermark_field {watermark_field!r} contains characters not permitted "
                    "in a Salesforce field name."
                )
            where_clause = (
                f" WHERE {watermark_field} >= :lower_bound AND {watermark_field} < :upper_bound"
            )
            params = {
                "lower_bound": watermark_lower,
                "upper_bound": watermark_upper,
            }
        else:
            where_clause = ""

        query_text = f"SELECT {field_list} FROM {self._object_name}{where_clause}"  # noqa: S608

        _logger.info(
            "salesforce_soql_query_built",
            object_name=self._object_name,
            load_type=str(load_type),
            field_count=len(field_contract.fields),
            has_watermark_filter=load_type == LoadType.INCREMENTAL,
        )

        return QueryContract(
            source_id=field_contract.source_id,
            entity_id=field_contract.entity_id,
            query_text=query_text,
            query_parameters=params,
            load_type=load_type,
            watermark_lower=watermark_lower,
            watermark_upper=watermark_upper,
            watermark_field=watermark_field if load_type == LoadType.INCREMENTAL else None,
        )

    def _build_field_list(self, field_contract: FieldContract) -> str:
        """
        Build the comma-separated SOQL field list from FieldContract.

        Every field name is validated against _FIELD_NAME_PATTERN before
        inclusion in the query string, preventing injection through
        malformed Describe API responses.

        Raises:
            SalesforceSoqlQueryBuilderError: if any field name is invalid.
        """
        validated: list[str] = []
        for descriptor in field_contract.fields:
            if not _FIELD_NAME_PATTERN.match(descriptor.name):
                raise SalesforceSoqlQueryBuilderError(
                    f"Field name {descriptor.name!r} from Describe API contains "
                    "characters not permitted in a SOQL field reference.  "
                    "This field will be excluded to prevent query injection."
                )
            validated.append(descriptor.name)

        if not validated:
            raise SalesforceSoqlQueryBuilderError(
                f"No valid fields available for object {self._object_name!r}. "
                "Check field_mode configuration and Describe API response."
            )
        return ", ".join(validated)
