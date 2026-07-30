"""
Migration: move shared per-source credentials to per-connection paths (DL-SEC-05, DL-SCOPE-06).

Before: one secret per connector type at `datalake/<env>/sources/{source_id}/credentials`,
shared by every
tenant using that connector. After: one secret per connection at
`datalake/<env>/tenants/{tenant_code}/connections/{connection_id}/credentials`.

The copy is additive — the legacy secret is left in place — because
`ConnectionCredentialPathResolver` falls back to it with a warning while the migration is in
flight. Deleting it is a **separate, explicit** step (`--delete-legacy`) so a partially-migrated
environment cannot lose its only credential copy.

New secrets are tagged `tenant_code`, which is what the DL-SEC-01 IAM boundary's Secrets Manager
condition matches on — an untagged secret is denied, so the tag is not cosmetic.

Dry-run by default. Values are never printed.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

from contracts.resource_naming import secret_path
from tenancy.source_connection import connection_credential_path


def _list_connections(table: Any) -> list[dict[str, Any]]:
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


def _secret_exists(secrets: Any, secret_id: str) -> bool:
    try:
        secrets.describe_secret(SecretId=secret_id)
    except ClientError:
        return False
    return True


def _copy_secret(
    secrets: Any,
    *,
    legacy_id: str,
    target_id: str,
    tenant_code: str,
    connection_id: str,
    kms_key_id: str | None,
    dry_run: bool,
) -> str:
    if _secret_exists(secrets, target_id):
        return "already-present"
    if not _secret_exists(secrets, legacy_id):
        return "no-legacy-source"
    if dry_run:
        return "would-copy"
    value = secrets.get_secret_value(SecretId=legacy_id)["SecretString"]
    create_kwargs: dict[str, Any] = {
        "Name": target_id,
        "SecretString": value,
        "Description": (
            f"Per-connection credentials for {connection_id} (migrated from {legacy_id})."
        ),
        "Tags": [
            {"Key": "tenant_code", "Value": tenant_code},
            {"Key": "connection_id", "Value": connection_id},
            {"Key": "ManagedBy", "Value": "migrate_credentials_to_connection_paths"},
        ],
    }
    if kms_key_id:
        create_kwargs["KmsKeyId"] = kms_key_id
    secrets.create_secret(**create_kwargs)
    return "copied"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Copy shared per-source credentials to per-connection paths (DL-SEC-05)."
    )
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--connection-table", default=os.environ.get("SOURCE_CONNECTION_TABLE"))
    parser.add_argument(
        "--kms-key-id",
        default=None,
        help="Secrets CMK for the new secrets; omit to use the account default key.",
    )
    parser.add_argument("--apply", action="store_true", help="Perform writes (default: dry-run).")
    parser.add_argument(
        "--delete-legacy",
        action="store_true",
        help=(
            "Schedule deletion of the shared per-source secrets. Run only after every "
            "environment has migrated and no legacy-path warning has been logged."
        ),
    )
    args = parser.parse_args()

    dry_run = not args.apply
    secrets = boto3.client("secretsmanager", region_name=args.region)
    table = boto3.resource("dynamodb", region_name=args.region).Table(args.connection_table)

    connections = _list_connections(table)
    if not connections:
        print(
            f"No connections found in {args.connection_table}. Run "
            "scripts/migrate_to_connection_identity.py --apply first — credential paths are "
            "derived from the connection model."
        )
        return

    mode = "DRY-RUN" if dry_run else "APPLYING"
    print(f"{mode} credential migration for {len(connections)} connection(s) ({args.region})")

    outcomes: dict[str, int] = {}
    legacy_sources: set[str] = set()
    for connection in sorted(connections, key=lambda c: (c["tenant_code"], c["connection_id"])):
        tenant_code = str(connection["tenant_code"])
        connection_id = str(connection["connection_id"])
        source_id = str(connection.get("source_id", connection_id))
        legacy_id = secret_path("sources", source_id, "credentials")
        target_id = connection_credential_path(tenant_code, connection_id)
        outcome = _copy_secret(
            secrets,
            legacy_id=legacy_id,
            target_id=target_id,
            tenant_code=tenant_code,
            connection_id=connection_id,
            kms_key_id=args.kms_key_id,
            dry_run=dry_run,
        )
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        print(f"  {outcome:>18}  {legacy_id} -> {target_id}")
        if outcome in ("copied", "would-copy", "already-present"):
            legacy_sources.add(legacy_id)

    print("\nSummary:")
    for outcome, count in sorted(outcomes.items()):
        print(f"  {outcome}: {count}")

    if args.delete_legacy:
        print("\nScheduling deletion of shared per-source secrets:")
        for legacy_id in sorted(legacy_sources):
            print(f"  - {legacy_id}")
            if not dry_run:
                secrets.delete_secret(SecretId=legacy_id, RecoveryWindowInDays=30)
    elif not dry_run:
        print(
            "\nLegacy shared secrets left in place. The resolver falls back to them with a "
            "warning; re-run with --delete-legacy once no warning has been logged."
        )

    if dry_run:
        print("\nNo writes performed. Re-run with --apply to execute.")


if __name__ == "__main__":
    main()
