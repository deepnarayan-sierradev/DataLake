#!/usr/bin/env python3
"""
Seed entity extraction configuration records into DynamoDB for local dev testing.

Usage:
    python scripts/seed_entity_config.py --environment dev --region us-east-1

This writes one record per source entity into the {environment}-entity-extraction-config
DynamoDB table.  All records are safe to re-run — they use put_item which is idempotent.

Prerequisite:
    AWS credentials configured (AWS_PROFILE, AWS_DEFAULT_PROFILE, or instance role).
    The DynamoDB table must already exist (provisioned by Terraform metadata_persistence module).

Records seeded:
    salesforce / salesforce-account    (full load, all fields)
    salesforce / salesforce-contact    (incremental, watermark on SystemModstamp)
    netsuite   / netsuite-customer     (incremental, watermark on lastModifiedDate)
    mysql-rds  / mysql-rds-contracts   (full load, table: Contracts)
"""

from __future__ import annotations

import argparse
import sys

import boto3


def _table_name(environment: str) -> str:
    return f"{environment}-entity-extraction-config"


def _raw_prefix(environment: str, source_id: str, entity_id: str) -> str:
    """Full s3:// URI for the raw layer partition root (no run-specific suffix)."""
    return f"s3://{environment}-edl-raw-layer/raw/{source_id}/{entity_id}/"


def _snapshot_prefix(environment: str, source_id: str, entity_id: str) -> str:
    """Full s3:// URI for schema snapshot storage."""
    return f"s3://{environment}-edl-schema-snapshots/{source_id}/{entity_id}/"


def _build_records(environment: str) -> list[dict[str, object]]:
    """Build entity extraction config records with environment-specific s3:// URIs."""
    return [
        {
            "source_id": "salesforce",
            "entity_id": "salesforce-account",
            "config_version": "1.0.0",
            "load_type": "full",
            "watermark_field": None,
            "extraction_window_days": 1,
            "watermark_overlap_hours": 0,
            "field_mode": "all",
            "include_fields": [],
            "exclude_fields": [],
            "target_raw_s3_prefix": _raw_prefix(environment, "salesforce", "salesforce-account"),
            "schema_snapshot_s3_prefix": _snapshot_prefix(environment, "salesforce", "salesforce-account"),
            "output_format": "parquet",
            "active": True,
        },
        {
            "source_id": "salesforce",
            "entity_id": "salesforce-contact",
            "config_version": "1.0.0",
            "load_type": "incremental",
            "watermark_field": "SystemModstamp",
            "extraction_window_days": 1,
            "watermark_overlap_hours": 1,
            "field_mode": "all",
            "include_fields": [],
            "exclude_fields": ["IsDeleted"],
            "target_raw_s3_prefix": _raw_prefix(environment, "salesforce", "salesforce-contact"),
            "schema_snapshot_s3_prefix": _snapshot_prefix(environment, "salesforce", "salesforce-contact"),
            "output_format": "parquet",
            "active": True,
        },
        {
            "source_id": "netsuite",
            "entity_id": "netsuite-customer",
            "config_version": "1.0.0",
            "load_type": "incremental",
            "watermark_field": "lastModifiedDate",
            "extraction_window_days": 1,
            "watermark_overlap_hours": 2,
            "field_mode": "all",
            "include_fields": [],
            "exclude_fields": [],
            "target_raw_s3_prefix": _raw_prefix(environment, "netsuite", "netsuite-customer"),
            "schema_snapshot_s3_prefix": _snapshot_prefix(environment, "netsuite", "netsuite-customer"),
            "output_format": "parquet",
            "active": True,
        },
        {
            "source_id": "mysql-rds",
            "entity_id": "mysql-rds-contracts",
            "config_version": "1.0.0",
            "load_type": "full",
            "watermark_field": None,
            "extraction_window_days": 1,
            "watermark_overlap_hours": 0,
            "field_mode": "all",
            "include_fields": [],
            "exclude_fields": [],
            "target_raw_s3_prefix": _raw_prefix(environment, "mysql-rds", "mysql-rds-contracts"),
            "schema_snapshot_s3_prefix": _snapshot_prefix(environment, "mysql-rds", "mysql-rds-contracts"),
            "output_format": "parquet",
            "active": True,
        },
    ]


def seed(environment: str, region: str, dry_run: bool = False) -> None:
    table_name = _table_name(environment)
    records = _build_records(environment)
    print(f"Target table: {table_name}  (region: {region})")

    if dry_run:
        print("\n[DRY RUN] Would write the following records:")
        for rec in records:
            print(f"  {rec['source_id']} / {rec['entity_id']}")
            print(f"    target_raw_s3_prefix       : {rec['target_raw_s3_prefix']}")
            print(f"    schema_snapshot_s3_prefix  : {rec['schema_snapshot_s3_prefix']}")
        return

    dynamodb = boto3.resource("dynamodb", region_name=region)
    table = dynamodb.Table(table_name)

    for rec in records:
        # DynamoDB does not have a native None type; omit None fields.
        item: dict[str, object] = {k: v for k, v in rec.items() if v is not None}
        table.put_item(Item=item)  # type: ignore[arg-type]
        print(f"  Written: {rec['source_id']} / {rec['entity_id']}")

    print(f"\n{len(records)} record(s) seeded successfully.")
    print("\nNext step: trigger a manual extraction run:")
    print(
        "  python scripts/trigger_extraction.py "
        "--source-id salesforce --entity-id salesforce-account "
        f"--environment {environment} --region {region}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed entity extraction config records.")
    parser.add_argument("--environment", required=True, choices=["dev", "staging", "prod"])
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be written without making API calls.",
    )
    args = parser.parse_args()

    if args.environment == "prod":
        confirm = input("You are seeding PRODUCTION. Type 'yes' to confirm: ")
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            sys.exit(0)

    seed(args.environment, args.region, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
