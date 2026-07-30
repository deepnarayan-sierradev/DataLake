"""
Migration: register a default source connection per existing source (DL-SCOPE-05).

`connection_id` becomes the identity component of every composite key. An existing
single-connection source migrates to `connection_id == source_id`, so every DynamoDB key and
schedule name is byte-identical to the pre-DL-12 form and nothing goes dark. What this script
does is create the missing `datalake-source-connections-dev` rows and stamp the `connection_id`
attribute
onto existing config and watermark items, so the connection model is populated before any
franchisee-specific connection is added.

Dry-run by default; `--apply` performs writes. Reversible: `--rollback` deletes the synthesised
connection rows and clears the stamped attribute, leaving the original items untouched.

Run **before** deploying the connection-aware code to each environment.
"""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime
from typing import Any

import boto3

from tenancy.source_connection import ConnectionState, SourceConnection


def _scan(table: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    scan_kwargs: dict[str, Any] = {}
    while True:
        page = table.scan(**scan_kwargs)
        items.extend(dict(item) for item in page.get("Items", []))
        last_key = page.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    return items


def _discover_sources(config_items: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
    """
    Map (tenant_code, source_id) -> source_id from the existing config table.

    The PK already holds `{tenant}#{source_id}`, so the plain source_id comes from splitting it
    rather than from a separate attribute, which is why this works on pre-migration data.
    """
    discovered: dict[tuple[str, str], str] = {}
    for item in config_items:
        tenant_code = str(item.get("tenant_code", "demo"))
        raw = str(item.get("source_id", ""))
        source_id = raw.split("#", 1)[1] if "#" in raw else raw
        if source_id:
            discovered[(tenant_code, source_id)] = source_id
    return discovered


def _register_defaults(
    connection_table: Any,
    sources: dict[tuple[str, str], str],
    *,
    dry_run: bool,
) -> tuple[int, int]:
    created = 0
    existing = 0
    for (tenant_code, source_id), connection_id in sorted(sources.items()):
        response = connection_table.get_item(
            Key={"tenant_code": tenant_code, "connection_id": connection_id}
        )
        if response.get("Item"):
            existing += 1
            continue
        connection = SourceConnection(
            tenant_code=tenant_code,
            connection_id=connection_id,
            source_id=source_id,
            display_name=source_id,
            state=ConnectionState.ACTIVE,
        )
        print(f"  + connection {tenant_code}/{connection_id} (source={source_id})")
        if not dry_run:
            connection_table.put_item(
                Item={
                    **connection.model_dump(mode="json"),
                    "migrated_at": datetime.now(UTC).isoformat(),
                    "migrated_by": "migrate_to_connection_identity",
                }
            )
        created += 1
    return created, existing


def _stamp_connection_id(table: Any, *, key_names: tuple[str, str], dry_run: bool) -> int:
    """
    Write the `connection_id` attribute onto existing items.

    The key itself is unchanged — for a default connection the scoped key already equals
    `{tenant}#{source_id}` — so this is an attribute stamp, not a re-key. That is what makes the
    migration non-destructive and the rollback trivial.
    """
    stamped = 0
    for item in _scan(table):
        if item.get("connection_id"):
            continue
        raw = str(item.get(key_names[0], ""))
        source_id = raw.split("#", 1)[1] if "#" in raw else raw
        if not source_id:
            continue
        print(f"  ~ stamp connection_id={source_id} on {raw}/{item.get(key_names[1])}")
        if not dry_run:
            table.update_item(
                Key={key_names[0]: raw, key_names[1]: item[key_names[1]]},
                UpdateExpression="SET connection_id = :cid",
                ExpressionAttributeValues={":cid": source_id},
            )
        stamped += 1
    return stamped


def _rollback(
    connection_table: Any, config_table: Any, watermark_table: Any, *, dry_run: bool
) -> tuple[int, int]:
    """Remove synthesised rows and stamps; original items are left exactly as they were."""
    removed = 0
    for item in _scan(connection_table):
        if item.get("migrated_by") != "migrate_to_connection_identity":
            continue
        print(f"  - connection {item['tenant_code']}/{item['connection_id']}")
        if not dry_run:
            connection_table.delete_item(
                Key={
                    "tenant_code": item["tenant_code"],
                    "connection_id": item["connection_id"],
                }
            )
        removed += 1

    cleared = 0
    for table, key_names in (
        (config_table, ("source_id", "entity_id")),
        (watermark_table, ("source_id", "entity_id")),
    ):
        for item in _scan(table):
            if not item.get("connection_id"):
                continue
            print(f"  ~ clear connection_id on {item[key_names[0]]}/{item[key_names[1]]}")
            if not dry_run:
                table.update_item(
                    Key={
                        key_names[0]: item[key_names[0]],
                        key_names[1]: item[key_names[1]],
                    },
                    UpdateExpression="REMOVE connection_id",
                )
            cleared += 1
    return removed, cleared


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Register a default source connection per existing source (DL-SCOPE-05)."
    )
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--connection-table", default=os.environ.get("SOURCE_CONNECTION_TABLE"))
    parser.add_argument("--config-table", default=os.environ.get("ENTITY_CONFIG_TABLE"))
    parser.add_argument("--watermark-table", default=os.environ.get("WATERMARK_TABLE"))
    parser.add_argument("--apply", action="store_true", help="Perform writes (default: dry-run).")
    parser.add_argument(
        "--rollback",
        action="store_true",
        help="Undo the migration: delete synthesised connections and clear stamped attributes.",
    )
    args = parser.parse_args()

    resource = boto3.resource("dynamodb", region_name=args.region)
    connection_table = resource.Table(args.connection_table)
    config_table = resource.Table(args.config_table)
    watermark_table = resource.Table(args.watermark_table)
    dry_run = not args.apply

    mode = "DRY-RUN" if dry_run else "APPLYING"
    print(f"{mode} connection-identity migration ({args.region})")

    if args.rollback:
        removed, cleared = _rollback(
            connection_table, config_table, watermark_table, dry_run=dry_run
        )
        print(f"\n{mode}: removed {removed} connection(s), cleared {cleared} stamp(s).")
        return

    sources = _discover_sources(_scan(config_table))
    print(f"\nDiscovered {len(sources)} (tenant, source) pair(s) in {args.config_table}.")

    print("\nDefault connections:")
    created, existing = _register_defaults(connection_table, sources, dry_run=dry_run)

    print("\nStamping connection_id on entity config:")
    config_stamped = _stamp_connection_id(
        config_table, key_names=("source_id", "entity_id"), dry_run=dry_run
    )

    print("\nStamping connection_id on watermarks:")
    watermark_stamped = _stamp_connection_id(
        watermark_table, key_names=("source_id", "entity_id"), dry_run=dry_run
    )

    print(
        f"\n{mode}: {created} connection(s) created, {existing} already present, "
        f"{config_stamped} config item(s) and {watermark_stamped} watermark item(s) stamped."
    )
    if dry_run:
        print("\nNo writes performed. Re-run with --apply to execute.")
    else:
        print("\nDone. Deploy the connection-aware code after this completes.")


if __name__ == "__main__":
    main()
