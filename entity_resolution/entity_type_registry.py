"""
Entity type registry for the Enterprise Data Lake platform.

Single source of truth for entity_id → entity_type mappings and related
lookup tables used by the entity resolution and analytics publisher pipelines.

ARCH-2: `EntityTypeRegistryClient` below is the DynamoDB-backed, tenant-scoped
registry. The module-level constants (ENTITY_ID_TO_TYPE, ENTITY_TYPE_PK_FIELD,
ENTITY_TYPE_SOURCES) are the seed data for the DEFAULT_TENANT_CODE tenant and
the fallback used when a DynamoDB record is absent — so a tenant with no
custom entity types registered gets identical behaviour to today, and
existing single-tenant deployments need no data migration.

Onboarding a new entity for the default tenant still means editing the
constants below and redeploying (unchanged from before). Onboarding a
tenant-specific entity type — the actual goal of this registry — means
calling `EntityTypeRegistryClient.register_entity_type()` with no code
change or redeploy required.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Final

import boto3
from botocore.exceptions import ClientError

from contracts.identifier_policy import DEFAULT_TENANT_CODE, validate_tenant_code
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# ---------------------------------------------------------------------------
# entity_id → canonical entity type
# ---------------------------------------------------------------------------

ENTITY_ID_TO_TYPE: Final[dict[str, str]] = {
    "salesforce-account": "company",
    "netsuite-customer": "company",  # ready for NetSuite onboarding
    "sage-intacct-customer": "company",  # Sage Intacct AR customer
    "sage-x3-customer": "company",  # Sage X3 business partner (customer)
    "salesforce-contact": "person",
    "mysql-rds-contracts": "contract",
    "sage-intacct-vendor": "supplier",  # Sage Intacct AP vendor
    "sage-x3-supplier": "supplier",  # Sage X3 business partner (supplier)
    "sage-intacct-arinvoice": "ar_invoice",  # Sage Intacct AR invoice
    "sage-intacct-apbill": "ap_bill",  # Sage Intacct AP bill
}

# ---------------------------------------------------------------------------
# entity_type → canonical primary-key field name in curated records
# ---------------------------------------------------------------------------

ENTITY_TYPE_PK_FIELD: Final[dict[str, str]] = {
    "company": "account_id",  # Salesforce Account, NetSuite Customer,
    #   Sage Intacct Customer, Sage X3 Customer —
    #   each maps its native ID to account_id.
    "person": "contact_id",
    "contract": "contract_id",
    "supplier": "vendor_id",  # Sage Intacct Vendor, Sage X3 Supplier
    "ar_invoice": "invoice_id",  # Sage Intacct AR Invoice
    "ap_bill": "bill_id",  # Sage Intacct AP Bill
}

# ---------------------------------------------------------------------------
# entity_type → ordered (source_id, entity_id) pairs that contribute records
# ---------------------------------------------------------------------------
# Order determines S3 scanning preference for "other sources"; it does NOT
# affect survivorship (that is controlled by SurvivorshipPolicy).

ENTITY_TYPE_SOURCES: Final[dict[str, list[tuple[str, str]]]] = {
    "company": [
        ("salesforce", "salesforce-account"),
        ("netsuite", "netsuite-customer"),  # skipped gracefully when absent
        ("sage", "sage-intacct-customer"),  # skipped gracefully when absent
        ("sage", "sage-x3-customer"),  # skipped gracefully when absent
    ],
    "person": [
        ("salesforce", "salesforce-contact"),
    ],
    "contract": [
        ("mysql-rds", "mysql-rds-contracts"),
    ],
    "supplier": [
        ("sage", "sage-intacct-vendor"),  # Intacct preferred for contact richness
        ("sage", "sage-x3-supplier"),
    ],
    "ar_invoice": [
        ("sage", "sage-intacct-arinvoice"),
    ],
    "ap_bill": [
        ("sage", "sage-intacct-apbill"),
    ],
}


# ---------------------------------------------------------------------------
# EntityTypeRegistryClient — DynamoDB-backed, tenant-scoped (ARCH-2)
# ---------------------------------------------------------------------------

_TABLE_NAME: Final[str] = "EdlEntityTypeRegistry"


@dataclass(frozen=True)
class EntityTypeRecord:
    """A tenant's registration for one entity_id."""

    entity_id: str
    entity_type: str
    pk_field: str
    contributing_sources: tuple[tuple[str, str], ...]


class EntityTypeRegistryClient:
    """
    DynamoDB-backed entity type registry (ARCH-2).

    Single-table design, PK=tenant_code:
      - Per-entity_id item:   SK = "entity_id#{entity_id}"   -> {entity_type}
      - Per-entity_type item: SK = "entity_type#{entity_type}" -> {pk_field, contributing_sources}

    Every read method falls back to the module-level constants
    (ENTITY_ID_TO_TYPE / ENTITY_TYPE_PK_FIELD / ENTITY_TYPE_SOURCES) when no
    DynamoDB record exists — this is what makes onboarding tenant-specific
    entity types additive: the default tenant's behaviour is unchanged, and
    a lookup failure never blocks the pipeline.
    """

    def __init__(self, environment: str, region_name: str) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        self._environment = environment
        table_name = os.environ.get("ENTITY_TYPE_REGISTRY_TABLE") or _TABLE_NAME
        self._table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)

    def get_entity_type(self, entity_id: str, tenant_code: str = DEFAULT_TENANT_CODE) -> str | None:
        """Return the entity_type for entity_id, or None if unknown to this tenant."""
        tenant_code = validate_tenant_code(tenant_code)
        item = self._get_item(tenant_code, f"entity_id#{entity_id}")
        if item is not None:
            return str(item["entity_type"])
        return ENTITY_ID_TO_TYPE.get(entity_id)

    def get_pk_field(self, entity_type: str, tenant_code: str = DEFAULT_TENANT_CODE) -> str | None:
        """Return the canonical primary-key field name for entity_type."""
        tenant_code = validate_tenant_code(tenant_code)
        item = self._get_item(tenant_code, f"entity_type#{entity_type}")
        if item is not None:
            return str(item["pk_field"])
        return ENTITY_TYPE_PK_FIELD.get(entity_type)

    def get_contributing_sources(
        self, entity_type: str, tenant_code: str = DEFAULT_TENANT_CODE
    ) -> list[tuple[str, str]]:
        """Return the ordered (source_id, entity_id) pairs contributing to entity_type."""
        tenant_code = validate_tenant_code(tenant_code)
        item = self._get_item(tenant_code, f"entity_type#{entity_type}")
        if item is not None:
            return [(pair[0], pair[1]) for pair in item["contributing_sources"]]
        return ENTITY_TYPE_SOURCES.get(entity_type, [])

    def register_entity_type(self, record: EntityTypeRecord, tenant_code: str) -> None:
        """
        Register (or update) an entity_id's type mapping for a tenant.

        Writes both the per-entity_id item and the per-entity_type item —
        the latter is only overwritten if this call includes a
        pk_field/contributing_sources, so multiple entity_ids of the same
        entity_type can be registered independently without clobbering each
        other's contribution to the shared entity_type descriptor as long as
        callers pass the full, current contributing_sources list each time.
        """
        tenant_code = validate_tenant_code(tenant_code)
        try:
            self._table.put_item(
                Item={
                    "tenant_code": tenant_code,
                    "sk": f"entity_id#{record.entity_id}",
                    "entity_type": record.entity_type,
                }
            )
            self._table.put_item(
                Item={
                    "tenant_code": tenant_code,
                    "sk": f"entity_type#{record.entity_type}",
                    "pk_field": record.pk_field,
                    "contributing_sources": [list(pair) for pair in record.contributing_sources],
                }
            )
        except ClientError as exc:
            _logger.error(
                "entity_type_registration_failed",
                tenant_code=tenant_code,
                entity_id=record.entity_id,
                entity_type=record.entity_type,
                error=str(exc),
            )
            raise

    def _get_item(self, tenant_code: str, sk: str) -> dict[str, Any] | None:
        try:
            response = self._table.get_item(Key={"tenant_code": tenant_code, "sk": sk})
        except ClientError as exc:
            _logger.warning(
                "entity_type_registry_lookup_failed",
                tenant_code=tenant_code,
                sk=sk,
                error=str(exc),
            )
            return None
        return response.get("Item")
