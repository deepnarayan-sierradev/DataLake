"""
SageQueryProtocol — structural type for any Sage product query engine.

Implemented by product-specific query builders (e.g. IntacctQueryEngine).
Consumed by SageConnector to build a QueryContract without knowing which
Sage product's query language is in use.

Security guarantee (OWASP A03):
  - Implementations MUST validate all field names against a safe pattern
    before including them in a query body.
  - Watermark values MUST be stored as query_parameters, not interpolated
    into query_text.  bind_parameters() validates before substitution.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from connector_runtime.interfaces.connector_interface import FieldContract, QueryContract
from contracts.entity_configuration_contract import LoadType


@runtime_checkable
class SageQueryProtocol(Protocol):
    """
    Structural type for a product-specific Sage query engine.

    The engine translates a platform FieldContract into a QueryContract
    whose query_text is a product-appropriate serialised query and whose
    query_parameters holds watermark values ready for safe substitution.
    """

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
        Build a parameterised extraction query from the discovered FieldContract.

        Args:
            field_contract:        Discovered fields from the metadata client.
            load_type:             FULL or INCREMENTAL.
            watermark_field:       Source field used for incremental filtering.
            watermark_lower:       ISO-8601 UTC lower bound (inclusive).
            watermark_upper:       ISO-8601 UTC upper bound (exclusive).
            extraction_window_days: Informational; used for logging only.

        Returns:
            QueryContract with parameterised query_text and bound parameters.

        Raises:
            SageQueryBuildError: on validation failure (bad field names, missing
                                  watermark_field for INCREMENTAL loads, etc.).
        """
        ...

    @staticmethod
    def bind_parameters(
        query_body: dict[str, Any],
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Substitute safe placeholder markers in the query body with validated values.

        Called by SageConnector.execute_extraction() immediately before sending
        the request.  Validates each substitution value before use.

        Args:
            query_body:  Deserialised query dict containing placeholder markers.
            parameters:  Dict of {placeholder_key: validated_value}.

        Returns:
            A new query dict with all placeholders replaced by validated values.

        Raises:
            SageQueryBuildError: if a parameter value fails validation.
        """
        ...
