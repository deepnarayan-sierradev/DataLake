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
    salesforce / salesforce-account         (full load, all fields)
    salesforce / salesforce-contact         (incremental, watermark on SystemModstamp)
    netsuite   / netsuite-customer          (incremental, watermark on lastModifiedDate)
    mysql-rds  / mysql-rds-contracts        (full load, table: Contracts)
    sage       / sage-intacct-customer      (incremental, watermark on auditInfo.modifiedAt)
    sage       / sage-intacct-vendor        (incremental, watermark on auditInfo.modifiedAt)
    sage       / sage-intacct-arinvoice     (incremental, watermark on auditInfo.modifiedAt)
    sage       / sage-intacct-apbill        (incremental, watermark on auditInfo.modifiedAt)
    sage       / sage-x3-customer           (incremental, watermark on MODDAT_0)
    sage       / sage-x3-supplier           (incremental, watermark on MODDAT_0, disabled)
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


def _sage_raw_prefix(environment: str, product_name: str, entity_id: str) -> str:
    """
    Full s3:// URI for Sage raw layer partition root.

    SageRawLayerWriter writes to sage/{product_name}/{entity_id}/ — the
    product_name segment is inserted by the writer so the path must match here.
    """
    return f"s3://{environment}-edl-raw-layer/sage/{product_name}/{entity_id}/"


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
            "connector_params": {"object_name": "Account"},
            "schedule_cron": "cron(0 2 * * ? *)",
            "schedule_enabled": True,
            "schedule_timezone": "UTC",
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
            "connector_params": {"object_name": "Contact"},
            "schedule_cron": "cron(15 2 * * ? *)",
            "schedule_enabled": True,
            "schedule_timezone": "UTC",
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
            "connector_params": {},
            "schedule_cron": None,
            "schedule_enabled": False,
            "schedule_timezone": "UTC",
            "active": False,
        },
        # ── Add new MySQL tables here — copy this block and adjust entity_id,
        # ── connector_params["table_name"], schedule_cron, and load_type.
        # ── After adding: make seed-entity-config && make seed-schedules
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
            "connector_params": {"table_name": "Contracts"},
            "schedule_cron": "cron(30 2 * * ? *)",
            "schedule_enabled": True,
            "schedule_timezone": "UTC",
            "active": True,
        },
        # ── Sage Intacct ─────────────────────────────────────────────────────
        {
            "source_id": "sage",
            "entity_id": "sage-intacct-customer",
            "config_version": "1.0.0",
            "load_type": "incremental",
            "watermark_field": "auditInfo.modifiedAt",
            "extraction_window_days": 1,
            "watermark_overlap_hours": 1,
            "field_mode": "all",
            "include_fields": [],
            "exclude_fields": [],
            "target_raw_s3_prefix": _sage_raw_prefix(environment, "intacct", "sage-intacct-customer"),
            "schema_snapshot_s3_prefix": _snapshot_prefix(environment, "sage", "sage-intacct-customer"),
            "output_format": "parquet",
            "connector_params": {"sage_product": "intacct", "object_path": "accounts-receivable/customer"},
            "schedule_cron": "cron(45 2 * * ? *)",
            "schedule_enabled": True,
            "schedule_timezone": "UTC",
            "active": True,
        },
        {
            "source_id": "sage",
            "entity_id": "sage-intacct-vendor",
            "config_version": "1.0.0",
            "load_type": "incremental",
            "watermark_field": "auditInfo.modifiedAt",
            "extraction_window_days": 1,
            "watermark_overlap_hours": 1,
            "field_mode": "all",
            "include_fields": [],
            "exclude_fields": [],
            "target_raw_s3_prefix": _sage_raw_prefix(environment, "intacct", "sage-intacct-vendor"),
            "schema_snapshot_s3_prefix": _snapshot_prefix(environment, "sage", "sage-intacct-vendor"),
            "output_format": "parquet",
            "connector_params": {"sage_product": "intacct", "object_path": "accounts-payable/vendor"},
            "schedule_cron": "cron(50 2 * * ? *)",
            "schedule_enabled": True,
            "schedule_timezone": "UTC",
            "active": True,
        },
        {
            "source_id": "sage",
            "entity_id": "sage-intacct-arinvoice",
            "config_version": "1.0.0",
            "load_type": "incremental",
            "watermark_field": "auditInfo.modifiedAt",
            "extraction_window_days": 1,
            "watermark_overlap_hours": 1,
            "field_mode": "all",
            "include_fields": [],
            "exclude_fields": [],
            "target_raw_s3_prefix": _sage_raw_prefix(environment, "intacct", "sage-intacct-arinvoice"),
            "schema_snapshot_s3_prefix": _snapshot_prefix(environment, "sage", "sage-intacct-arinvoice"),
            "output_format": "parquet",
            "connector_params": {"sage_product": "intacct", "object_path": "accounts-receivable/invoice"},
            "schedule_cron": "cron(55 2 * * ? *)",
            "schedule_enabled": True,
            "schedule_timezone": "UTC",
            "active": True,
        },
        {
            "source_id": "sage",
            "entity_id": "sage-intacct-apbill",
            "config_version": "1.0.0",
            "load_type": "incremental",
            "watermark_field": "auditInfo.modifiedAt",
            "extraction_window_days": 1,
            "watermark_overlap_hours": 1,
            "field_mode": "all",
            "include_fields": [],
            "exclude_fields": [],
            "target_raw_s3_prefix": _sage_raw_prefix(environment, "intacct", "sage-intacct-apbill"),
            "schema_snapshot_s3_prefix": _snapshot_prefix(environment, "sage", "sage-intacct-apbill"),
            "output_format": "parquet",
            "connector_params": {"sage_product": "intacct", "object_path": "accounts-payable/bill"},
            "schedule_cron": "cron(5 3 * * ? *)",
            "schedule_enabled": True,
            "schedule_timezone": "UTC",
            "active": True,
        },
        # ── Sage X3 ────────────────────────────────────────────────────────────
        {
            "source_id": "sage",
            "entity_id": "sage-x3-customer",
            "config_version": "1.0.0",
            "load_type": "incremental",
            "watermark_field": "MODDAT_0",
            "extraction_window_days": 1,
            "watermark_overlap_hours": 1,
            "field_mode": "all",
            "include_fields": [],
            "exclude_fields": [],
            "target_raw_s3_prefix": _sage_raw_prefix(environment, "x3", "sage-x3-customer"),
            "schema_snapshot_s3_prefix": _snapshot_prefix(environment, "sage", "sage-x3-customer"),
            "output_format": "parquet",
            "connector_params": {"sage_product": "x3", "object_path": "BPCUSTOMER"},
            "schedule_cron": "cron(55 2 * * ? *)",
            "schedule_enabled": True,
            "schedule_timezone": "UTC",
            "active": True,
        },
        {
            "source_id": "sage",
            "entity_id": "sage-x3-supplier",
            "config_version": "1.0.0",
            "load_type": "incremental",
            "watermark_field": "MODDAT_0",
            "extraction_window_days": 1,
            "watermark_overlap_hours": 1,
            "field_mode": "all",
            "include_fields": [],
            "exclude_fields": [],
            "target_raw_s3_prefix": _sage_raw_prefix(environment, "x3", "sage-x3-supplier"),
            "schema_snapshot_s3_prefix": _snapshot_prefix(environment, "sage", "sage-x3-supplier"),
            "output_format": "parquet",
            "connector_params": {"sage_product": "x3", "object_path": "BPSUPPLIER"},
            "schedule_cron": "cron(0 3 * * ? *)",
            "schedule_enabled": False,
            "schedule_timezone": "UTC",
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
