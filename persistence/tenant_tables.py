"""
The one declaration of which DynamoDB tables hold tenant data (DL-PORT-04, DL-SEC-02).

Two things needed this list and neither could derive it: the deletion saga, which must sweep every
tenant-keyed table or refuse to certify, and the IAM tenant boundary, whose `dynamodb:LeadingKeys`
condition applies to exactly these tables. Before this existed each repository named its own table
in its own module constant and nothing enumerated them, so "did we cover every table" was answered
by grep.

**The names are supplied by Terraform, not written here.** Each list arrives as a comma-separated
environment variable built from the tables Terraform actually created, so the sweep cannot address a
table that does not exist and cannot miss one that does — the failure mode a hand-maintained list
has. dev and uat share an AWS account, which makes a hardcoded list actively dangerous: it would
have a uat deletion sweep dev's tables.

Read lazily through functions rather than at import, because a Lambda that imports this module for
one list must not fail cold-start over a variable it never uses.

The deletion certificate table is deliberately **excluded from the sweep**: it is the evidence the
deletion happened, so deleting it as part of the deletion would destroy the record that proves
compliance (SOW §24.7 requires written confirmation to survive).
"""

from __future__ import annotations

from typing import Final

from contracts.resource_naming import name_list_from_env

TENANT_KEYED_TABLES_VAR: Final[str] = "TENANT_KEYED_TABLES"

TENANT_SCOPED_KEY_TABLES_VAR: Final[str] = "TENANT_SCOPED_KEY_TABLES"

TENANT_ATTRIBUTED_TABLES_VAR: Final[str] = "TENANT_ATTRIBUTED_TABLES"

DELETION_EVIDENCE_TABLES_VAR: Final[str] = "DELETION_EVIDENCE_TABLES"

ENTITY_TYPE_REGISTRY_TABLE_VAR: Final[str] = "ENTITY_TYPE_REGISTRY_TABLE"

TENANT_ATTRIBUTED_INDEX: Final[str] = "tenant-started-index"


def tenant_keyed_tables() -> tuple[str, ...]:
    """Tables whose partition key is `tenant_code` itself."""
    return name_list_from_env(TENANT_KEYED_TABLES_VAR)


def tenant_scoped_key_tables() -> tuple[str, ...]:
    """Tables whose partition key is `tenant#...`, matched by prefix."""
    return name_list_from_env(TENANT_SCOPED_KEY_TABLES_VAR)


def tenant_attributed_tables() -> tuple[str, ...]:
    """Tables holding tenant data under a non-tenant partition key, reached via a GSI."""
    return name_list_from_env(TENANT_ATTRIBUTED_TABLES_VAR)


def deletion_evidence_tables() -> tuple[str, ...]:
    """Tables that survive a deletion because they are its proof."""
    return name_list_from_env(DELETION_EVIDENCE_TABLES_VAR)


def tenant_scoped_tables() -> tuple[str, ...]:
    """Every table a tenant deletion or export must cover."""
    return tenant_keyed_tables() + tenant_scoped_key_tables() + tenant_attributed_tables()


def all_platform_tables() -> frozenset[str]:
    """Every table this platform owns, swept or not."""
    from observability.lambda_runtime import require_env

    return frozenset(
        tenant_scoped_tables()
        + deletion_evidence_tables()
        + (require_env(ENTITY_TYPE_REGISTRY_TABLE_VAR),)
    )
