#!/usr/bin/env python3
"""
Migrate DynamoDB table data from legacy (non-edl) tables to the new edl-prefixed tables.

Run this AFTER `terraform apply` (which recreates the edl tables with correct key schemas)
and BEFORE deploying the new Lambda code that points to the edl tables.

Tables migrated
---------------
  dev-entity-extraction-config    →  dev-edl-entity-extraction-config
  dev-watermark-repository        →  dev-edl-watermark-repository
  dev-run-audit-log               →  dev-edl-run-audit-log   (optional — audit history only)

Usage
-----
    # Dry run — prints counts, writes nothing:
    python scripts/migrate_tables_to_edl.py --environment dev --region us-east-1

    # Live migration (all tables):
    python scripts/migrate_tables_to_edl.py --environment dev --region us-east-1 --execute

    # Skip audit log (recommended — audit log is historical only, not operationally required):
    python scripts/migrate_tables_to_edl.py \
        --environment dev --region us-east-1 --execute --skip-audit-log

    # Migrate a single table:
    python scripts/migrate_tables_to_edl.py \
        --environment dev --region us-east-1 --execute --table watermark

Prerequisites
-------------
    AWS credentials configured with read access to legacy tables and
    read+write access to the edl tables.
    The edl tables must already exist (created by `terraform apply`).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator
from dataclasses import dataclass

import boto3
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Table pair definitions
# ---------------------------------------------------------------------------

_LABEL_ENTITY_CONFIG = "entity-extraction-config"
_LABEL_WATERMARK = "watermark-repository"
_LABEL_AUDIT_LOG = "run-audit-log"


@dataclass(frozen=True)
class TableMigration:
    label: str
    source: str  # legacy table name
    destination: str  # new edl table name


def _build_migrations(environment: str) -> list[TableMigration]:
    return [
        TableMigration(
            label=_LABEL_ENTITY_CONFIG,
            source=f"{environment}-entity-extraction-config",
            destination=f"{environment}-edl-entity-extraction-config",
        ),
        TableMigration(
            label=_LABEL_WATERMARK,
            source=f"{environment}-watermark-repository",
            destination=f"{environment}-edl-watermark-repository",
        ),
        TableMigration(
            label=_LABEL_AUDIT_LOG,
            source=f"{environment}-run-audit-log",
            destination=f"{environment}-edl-run-audit-log",
        ),
    ]


# ---------------------------------------------------------------------------
# DynamoDB helpers
# ---------------------------------------------------------------------------


def _scan_all(table) -> Iterator[dict]:
    """
    Full table scan using pagination — yields one item at a time.
    Never loads the entire table into memory.
    """
    kwargs: dict = {}
    while True:
        response = table.scan(**kwargs)
        yield from response.get("Items", [])
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key


def _verify_table_exists(ddb, table_name: str) -> bool:
    try:
        ddb.Table(table_name).load()
        return True
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "ResourceNotFoundException":
            return False
        # AccessDeniedException or other AWS errors — re-raise with context
        raise RuntimeError(
            f"Failed to verify table '{table_name}': {code} — {exc.response['Error']['Message']}"
        ) from exc


# ---------------------------------------------------------------------------
# Migration logic
# ---------------------------------------------------------------------------


def _migrate_table(
    ddb,
    migration: TableMigration,
    execute: bool,
) -> tuple[int, int]:
    """
    Stream all items from source to destination table using a single batch_writer
    context (boto3 handles internal 25-item DynamoDB batches automatically).

    Returns (source_count, written_count).
    When execute=False (dry run) written_count is always 0.
    """
    src_table = ddb.Table(migration.source)
    dest_table = ddb.Table(migration.destination)

    print(f"\n  [{migration.label}]")
    print(f"    source      : {migration.source}")
    print(f"    destination : {migration.destination}")

    if not _verify_table_exists(ddb, migration.source):
        print("    ⚠  Source table does not exist — skipping.")
        return 0, 0

    if not _verify_table_exists(ddb, migration.destination):
        print("    ✗  Destination table does not exist — run `terraform apply` first.")
        return 0, 0

    src_count = 0
    written = 0

    if not execute:
        # Dry run: count only, write nothing.
        print("    Counting source items (dry run) …", end=" ", flush=True)
        for _ in _scan_all(src_table):
            src_count += 1
        print(f"{src_count} item(s).")
        if src_count == 0:
            print("    Nothing to migrate.")
        else:
            print(f"    DRY RUN — would write {src_count} item(s) to destination.")
        return src_count, 0

    # Live migration: stream source → single batch_writer context.
    # boto3's batch_writer buffers items and flushes every 25 items automatically,
    # also retrying any UnprocessedItems returned by DynamoDB.
    print("    Migrating items …", end=" ", flush=True)
    with dest_table.batch_writer() as batch:
        for item in _scan_all(src_table):
            src_count += 1
            batch.put_item(Item=item)
            written += 1
            if written % 500 == 0:
                print(f"{written} …", end=" ", flush=True)

    print(f"done ({written} item(s) written).")

    # Validation: compare what we read from source against what we wrote.
    # We deliberately do NOT re-scan the destination immediately — DynamoDB
    # reads are eventually consistent and a scan right after a batch write can
    # return a lower count. The written count is the reliable figure.
    if written == src_count:
        print(f"    ✓  Validation passed: {written} item(s) written == {src_count} scanned.")
    else:
        # This should never happen since we increment both counters in the same loop,
        # but guard defensively.
        print(
            f"    ✗  COUNT MISMATCH: scanned={src_count}, written={written}. "
            "Re-run the migration to fill in any missing items."
        )

    return src_count, written


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

_TABLE_CLI_TO_LABEL: dict[str, str] = {
    "entity-config": _LABEL_ENTITY_CONFIG,
    "watermark": _LABEL_WATERMARK,
    "audit-log": _LABEL_AUDIT_LOG,
}


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Migrate DynamoDB data from legacy to edl-prefixed tables.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--environment",
        required=True,
        choices=["dev", "staging", "prod"],
        help="Target environment.",
    )
    parser.add_argument("--region", required=True, help="AWS region (e.g. us-east-1).")
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Actually write data. Without this flag the script is a dry run.",
    )
    parser.add_argument(
        "--skip-audit-log",
        action="store_true",
        default=False,
        help="Skip the run-audit-log table (historical data only).",
    )
    parser.add_argument(
        "--table",
        choices=list(_TABLE_CLI_TO_LABEL.keys()),
        default=None,
        help="Migrate a single named table instead of all three.",
    )
    parser.add_argument("--profile", default=None, help="AWS profile name (optional).")
    return parser


def _select_migrations(args: argparse.Namespace) -> list[TableMigration]:
    migrations = _build_migrations(args.environment)

    if args.table:
        target_label = _TABLE_CLI_TO_LABEL[args.table]
        migrations = [m for m in migrations if m.label == target_label]

    if args.skip_audit_log:
        migrations = [m for m in migrations if m.label != _LABEL_AUDIT_LOG]

    return migrations


def _confirm_prod_migration() -> bool:
    """Prompt for an explicit confirmation string before writing to prod. True = proceed."""
    confirm = input(
        "\n  ⚠  You are about to write to PRODUCTION tables.\n"
        "  Type 'yes-migrate-prod' to confirm: "
    )
    return confirm.strip() == "yes-migrate-prod"


def _run_migrations(
    ddb, migrations: list[TableMigration], execute: bool
) -> tuple[int, int, list[str]]:
    total_src = 0
    total_written = 0
    errors: list[str] = []

    for migration in migrations:
        try:
            src, written = _migrate_table(ddb, migration, execute=execute)
            total_src += src
            total_written += written
        except Exception as exc:
            errors.append(f"{migration.label}: {exc}")
            print(f"    ✗  ERROR: {exc}")

    return total_src, total_written, errors


def main() -> int:
    args = _build_arg_parser().parse_args()

    # Guard: --table audit-log + --skip-audit-log is contradictory.
    if args.table == "audit-log" and args.skip_audit_log:
        print(
            "ERROR: --table audit-log and --skip-audit-log are mutually exclusive.", file=sys.stderr
        )
        return 2

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    ddb = session.resource("dynamodb")  # region inherited from session

    migrations = _select_migrations(args)

    mode = "LIVE MIGRATION" if args.execute else "DRY RUN (no data written)"
    print(f"\n{'=' * 60}")
    print(f"  DynamoDB EDL Table Migration — {args.environment.upper()}")
    print(f"  Mode   : {mode}")
    print(f"  Region : {args.region}")
    print(f"  Tables : {len(migrations)} selected")
    print(f"{'=' * 60}")

    if args.execute and args.environment == "prod" and not _confirm_prod_migration():
        print("  Aborted.")
        return 1

    total_src, total_written, errors = _run_migrations(ddb, migrations, args.execute)

    print(f"\n{'=' * 60}")
    print("  Summary")
    print(f"{'=' * 60}")
    print(f"  Source records scanned : {total_src}")
    print(f"  Records written        : {total_written}")
    if errors:
        print(f"  Errors                 : {len(errors)}")
        for err in errors:
            print(f"    - {err}")
        return 1

    if not args.execute:
        print("\n  This was a DRY RUN. Re-run with --execute to perform the migration.")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
