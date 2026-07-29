"""
Capability declarations for the four pre-DL-01 adapters (DL-CONN-17, DL-CONN-12).

Salesforce, NetSuite, MySQL RDS, and Sage predate the declaration mechanism. Declaring them
here rather than editing four adapters keeps the console's source list complete without
touching proven extraction code, and it is what lets `EP-04` stop hardcoding source names.

Sage Intacct's declaration is what `DL-CONN-12` activates against — no new code, just
credentials, entity configs, field mappings, and schedules.
"""

from __future__ import annotations

from typing import Final

from connector_runtime.source_capabilities import (
    SourceCapability,
    SourceCapabilityDeclaration,
    source_capability_registry,
)

_DECLARATIONS: Final[tuple[SourceCapabilityDeclaration, ...]] = (
    SourceCapabilityDeclaration(
        source_id="salesforce",
        display_name="Salesforce",
        capabilities=frozenset(
            {
                SourceCapability.INCREMENTAL,
                SourceCapability.SOFT_DELETE,
                SourceCapability.BULK_EXPORT,
                SourceCapability.SCHEMA_DISCOVERY,
                SourceCapability.RECORD_COUNT,
            }
        ),
        default_pagination_strategy="offset_limit",
        notes="Retained and proven; not on the customer's required source list.",
    ),
    SourceCapabilityDeclaration(
        source_id="netsuite",
        display_name="NetSuite",
        capabilities=frozenset(
            {
                SourceCapability.INCREMENTAL,
                SourceCapability.SCHEMA_DISCOVERY,
                SourceCapability.RECORD_COUNT,
            }
        ),
        # Gap 17: NetSuite's keyset paging is now an implementation of the shared interface.
        default_pagination_strategy="keyset",
        notes="Retained and proven; not on the customer's required source list.",
    ),
    SourceCapabilityDeclaration(
        source_id="mysql-rds",
        display_name="MySQL RDS",
        capabilities=frozenset(
            {
                SourceCapability.INCREMENTAL,
                SourceCapability.SOFT_DELETE,
                SourceCapability.SCHEMA_DISCOVERY,
                SourceCapability.RECORD_COUNT,
            }
        ),
        # The only source where binlog CDC via DMS is available (DL-CONN-13).
        default_sync_strategy="log_based_cdc",
        default_pagination_strategy="keyset",
        notes="Database-direct source; binlog CDC available through DMS.",
    ),
    SourceCapabilityDeclaration(
        source_id="sage",
        display_name="Sage (Intacct and X3)",
        capabilities=frozenset(
            {
                SourceCapability.INCREMENTAL,
                SourceCapability.SCHEMA_DISCOVERY,
                SourceCapability.RECORD_COUNT,
            }
        ),
        default_pagination_strategy="offset_limit",
        default_rate_limit_policy="sage-intacct-standard",
        notes=(
            "Sage Intacct is source #4 on the customer list (Evive finance, 100+ tables). "
            "Activation is configuration only — see scripts/activate_sage_intacct.py."
        ),
    ),
)


def register_legacy_source_capabilities() -> None:
    """Idempotent registration so importing twice in a warm container is safe."""
    already = set(source_capability_registry.registered_source_ids())
    for declaration in _DECLARATIONS:
        if declaration.source_id not in already:
            source_capability_registry.register(declaration)


register_legacy_source_capabilities()
