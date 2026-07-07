#!/usr/bin/env python3
"""
Seed the entity type registry DynamoDB table for a tenant (ARCH-2).

The DEFAULT_TENANT_CODE ("demo") tenant does NOT need seeding — the platform
falls back to the hardcoded ENTITY_ID_TO_TYPE / ENTITY_TYPE_PK_FIELD /
ENTITY_TYPE_SOURCES constants in entity_resolution/entity_type_registry.py
when no DynamoDB record exists, so existing single-tenant behaviour is
unaffected by this table being empty.

This script is for onboarding a NEW tenant's entity types — either mirroring
the default set, or registering tenant-specific custom entities.

Usage:
    # Mirror the default entity set for a new tenant (common case):
    python scripts/seed_entity_type_registry.py \\
        --environment dev --region us-east-1 --tenant-code acme-corp --mirror-default

    # Register one custom entity type for a tenant:
    python scripts/seed_entity_type_registry.py \\
        --environment dev --region us-east-1 --tenant-code acme-corp \\
        --entity-id acme-widget --entity-type widget --pk-field widget_id \\
        --contributing-source acme-erp:acme-widget

Prerequisite:
    AWS credentials configured. The DynamoDB table must already exist
    (provisioned by Terraform metadata_persistence module).
"""

from __future__ import annotations

import argparse
import sys

from entity_resolution.entity_type_registry import (
    ENTITY_ID_TO_TYPE,
    ENTITY_TYPE_PK_FIELD,
    ENTITY_TYPE_SOURCES,
    EntityTypeRecord,
    EntityTypeRegistryClient,
)


def _mirror_default(client: EntityTypeRegistryClient, tenant_code: str, dry_run: bool) -> None:
    for entity_id, entity_type in ENTITY_ID_TO_TYPE.items():
        pk_field = ENTITY_TYPE_PK_FIELD[entity_type]
        contributing_sources = tuple(ENTITY_TYPE_SOURCES.get(entity_type, []))
        if dry_run:
            print(f"  [DRY RUN] {entity_id} -> {entity_type} (pk_field={pk_field})")
            continue
        client.register_entity_type(
            EntityTypeRecord(
                entity_id=entity_id,
                entity_type=entity_type,
                pk_field=pk_field,
                contributing_sources=contributing_sources,
            ),
            tenant_code=tenant_code,
        )
        print(f"  Registered: {entity_id} -> {entity_type}")


def _register_one(
    client: EntityTypeRegistryClient,
    tenant_code: str,
    entity_id: str,
    entity_type: str,
    pk_field: str,
    contributing_sources: list[str],
    dry_run: bool,
) -> None:
    parsed_sources = tuple(
        (pair.split(":", 1)[0], pair.split(":", 1)[1]) for pair in contributing_sources
    )
    if dry_run:
        print(f"  [DRY RUN] {entity_id} -> {entity_type} (pk_field={pk_field})")
        print(f"    contributing_sources: {parsed_sources}")
        return
    client.register_entity_type(
        EntityTypeRecord(
            entity_id=entity_id,
            entity_type=entity_type,
            pk_field=pk_field,
            contributing_sources=parsed_sources,
        ),
        tenant_code=tenant_code,
    )
    print(f"  Registered: {entity_id} -> {entity_type}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed the entity type registry for a tenant.")
    parser.add_argument("--environment", required=True, choices=["dev", "staging", "prod"])
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--tenant-code", required=True, help="Tenant code slug (e.g. acme-corp).")
    parser.add_argument(
        "--mirror-default",
        action="store_true",
        help="Register the same entity set the default tenant uses (common for new tenants).",
    )
    parser.add_argument(
        "--entity-id", help="Single entity_id to register (with --entity-type/--pk-field)."
    )
    parser.add_argument("--entity-type", help="Entity type for --entity-id.")
    parser.add_argument("--pk-field", help="Primary-key field name for --entity-type.")
    parser.add_argument(
        "--contributing-source",
        action="append",
        default=[],
        help="source_id:entity_id pair contributing to --entity-type. Repeatable.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without making API calls.",
    )
    args = parser.parse_args()

    if not args.mirror_default and not (args.entity_id and args.entity_type and args.pk_field):
        parser.error("Either --mirror-default or --entity-id/--entity-type/--pk-field is required.")

    if args.environment == "prod":
        confirm = input("You are seeding PRODUCTION. Type 'yes' to confirm: ")
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            sys.exit(0)

    client = EntityTypeRegistryClient(environment=args.environment, region_name=args.region)
    print(f"Target tenant: {args.tenant_code}  (environment: {args.environment})")

    if args.mirror_default:
        _mirror_default(client, args.tenant_code, args.dry_run)
    else:
        # parser.error() above already exits if any of these three are missing.
        _register_one(
            client,
            args.tenant_code,
            str(args.entity_id),
            str(args.entity_type),
            str(args.pk_field),
            args.contributing_source,
            args.dry_run,
        )

    print("\nDone.")


if __name__ == "__main__":
    main()
