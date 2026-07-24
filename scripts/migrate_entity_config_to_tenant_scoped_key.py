"""
One-time migration: re-key EdlEntityExtractionConfig onto the tenant-scoped PK (FR-F0.8b).

The entity-extraction-config PK changed from a plain source_id to
tenant_scoped_key(tenant_code, source_id) = "{tenant_code}#{source_id}". Items
written before that change use the plain key and are invisible to the new read
path. This rewrites each such item under the scoped key and deletes the old one.

Idempotent and dry-run by default. Run once per environment (with --apply) BEFORE
deploying the new configuration_repository code, or existing configs go dark.
"""

from __future__ import annotations

import argparse
from typing import Any

import boto3

from contracts.identifier_policy import tenant_scoped_key


def _migrate(table: Any, *, dry_run: bool) -> tuple[int, int]:
    migrated = 0
    already_scoped = 0
    scan_kwargs: dict[str, Any] = {}
    while True:
        page = table.scan(**scan_kwargs)
        for item in page.get("Items", []):
            source_id = str(item.get("source_id", ""))
            entity_id = str(item.get("entity_id", ""))
            tenant_code = str(item.get("tenant_code", "demo"))
            if "#" in source_id:
                already_scoped += 1
                continue
            scoped = tenant_scoped_key(tenant_code, source_id)
            print(f"  {source_id} -> {scoped}  (entity_id={entity_id})")
            if not dry_run:
                table.put_item(Item={**item, "source_id": scoped})
                table.delete_item(Key={"source_id": source_id, "entity_id": entity_id})
            migrated += 1
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    return migrated, already_scoped


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-key entity extraction config onto the tenant-scoped PK (FR-F0.8b)."
    )
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--table", default="EdlEntityExtractionConfig")
    parser.add_argument("--apply", action="store_true", help="Perform writes (default: dry-run).")
    args = parser.parse_args()

    table = boto3.resource("dynamodb", region_name=args.region).Table(args.table)
    dry_run = not args.apply
    print(f"{'DRY-RUN' if dry_run else 'APPLYING'} migration on {args.table} ({args.region})")
    migrated, already_scoped = _migrate(table, dry_run=dry_run)
    print(
        f"\n{migrated} item(s) {'would be ' if dry_run else ''}migrated; "
        f"{already_scoped} already tenant-scoped."
    )
    if dry_run:
        print("Re-run with --apply to perform the migration.")


if __name__ == "__main__":
    main()
