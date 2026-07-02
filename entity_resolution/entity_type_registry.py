"""
Entity type registry for the Enterprise Data Lake platform.

Single source of truth for entity_id → entity_type mappings and related
lookup tables used by the entity resolution and analytics publisher pipelines.

Adding a new entity:
  1. Add entry to ENTITY_ID_TO_TYPE.
  2. Add primary-key field to ENTITY_TYPE_PK_FIELD (if it is a new entity_type).
  3. Add the (source_id, entity_id) pair to the correct ENTITY_TYPE_SOURCES list.
  4. Rebuild and redeploy the Lambda zip.

Both entity_resolution_pipeline_handler and analytics_publisher_handler import
from this module — only one edit required when onboarding a new entity.
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# entity_id → canonical entity type
# ---------------------------------------------------------------------------

ENTITY_ID_TO_TYPE: Final[dict[str, str]] = {
    "salesforce-account":     "company",
    "netsuite-customer":      "company",       # ready for NetSuite onboarding
    "sage-intacct-customer":  "company",       # Sage Intacct AR customer
    "sage-x3-customer":       "company",       # Sage X3 business partner (customer)
    "salesforce-contact":     "person",
    "mysql-rds-contracts":    "contract",
    "sage-intacct-vendor":    "supplier",      # Sage Intacct AP vendor
    "sage-x3-supplier":       "supplier",      # Sage X3 business partner (supplier)
    "sage-intacct-arinvoice": "ar_invoice",    # Sage Intacct AR invoice
    "sage-intacct-apbill":    "ap_bill",       # Sage Intacct AP bill
}

# ---------------------------------------------------------------------------
# entity_type → canonical primary-key field name in curated records
# ---------------------------------------------------------------------------

ENTITY_TYPE_PK_FIELD: Final[dict[str, str]] = {
    "company":    "account_id",   # Salesforce Account, NetSuite Customer,
                                  #   Sage Intacct Customer, Sage X3 Customer —
                                  #   each maps its native ID to account_id.
    "person":     "contact_id",
    "contract":   "contract_id",
    "supplier":   "vendor_id",    # Sage Intacct Vendor, Sage X3 Supplier
    "ar_invoice": "invoice_id",   # Sage Intacct AR Invoice
    "ap_bill":    "bill_id",      # Sage Intacct AP Bill
}

# ---------------------------------------------------------------------------
# entity_type → ordered (source_id, entity_id) pairs that contribute records
# ---------------------------------------------------------------------------
# Order determines S3 scanning preference for "other sources"; it does NOT
# affect survivorship (that is controlled by SurvivorshipPolicy).

ENTITY_TYPE_SOURCES: Final[dict[str, list[tuple[str, str]]]] = {
    "company": [
        ("salesforce", "salesforce-account"),
        ("netsuite",   "netsuite-customer"),      # skipped gracefully when absent
        ("sage",       "sage-intacct-customer"),  # skipped gracefully when absent
        ("sage",       "sage-x3-customer"),       # skipped gracefully when absent
    ],
    "person": [
        ("salesforce", "salesforce-contact"),
    ],
    "contract": [
        ("mysql-rds", "mysql-rds-contracts"),
    ],
    "supplier": [
        ("sage", "sage-intacct-vendor"),    # Intacct preferred for contact richness
        ("sage", "sage-x3-supplier"),
    ],
    "ar_invoice": [
        ("sage", "sage-intacct-arinvoice"),
    ],
    "ap_bill": [
        ("sage", "sage-intacct-apbill"),
    ],
}
