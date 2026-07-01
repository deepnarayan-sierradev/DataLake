"""
SageMetadataProtocol — structural type for any Sage product metadata client.

Implemented by product-specific metadata clients (e.g. IntacctMetadataClient).
Consumed by SageConnector to discover queryable fields without knowing which
Sage product's metadata API is in use.

Products that do not expose a live metadata endpoint implement this protocol
by returning a FieldContract derived from a static schema definition with
supports_live_discovery = False.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from connector_runtime.interfaces.connector_interface import FieldContract
from contracts.entity_configuration_contract import FieldMode


@runtime_checkable
class SageMetadataProtocol(Protocol):
    """
    Structural type for a product-specific Sage metadata discovery client.

    Discovers the queryable fields for a given Sage object path and returns
    a platform FieldContract with a schema fingerprint for drift detection.
    """

    @property
    def supports_live_discovery(self) -> bool:
        """
        True when this client queries a live Sage metadata API.
        False when field discovery falls back to a static schema definition.

        Used by SageConnector to populate ConnectorCapabilities.
        supports_metadata_discovery accordingly.
        """
        ...

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
        Discover all queryable fields for the given Sage object path.

        Args:
            source_id:      Platform stable source identifier (e.g. "sage").
            entity_id:      Platform stable entity identifier.
            object_path:    Sage product object path (e.g. "accounts-receivable/customer").
            field_mode:     ALL, STANDARD, CUSTOM, or INCLUDE_ONLY.
            include_fields: When field_mode=INCLUDE_ONLY, exactly these fields.
            exclude_fields: Fields to unconditionally exclude regardless of mode.

        Returns:
            FieldContract with all applicable fields and a deterministic fingerprint.

        Raises:
            SageMetadataError: on API failure or unrecognised object path.
        """
        ...

    def invalidate_cache(self) -> None:
        """
        Force the next discover_fields() call to re-fetch from the metadata API.

        No-op for static (non-live) metadata clients.
        """
        ...
