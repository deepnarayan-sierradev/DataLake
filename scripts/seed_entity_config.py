#!/usr/bin/env python3
"""
Seed entity extraction configuration records into DynamoDB for local dev testing.

Usage:
    python scripts/seed_entity_config.py --environment dev --region us-east-1

This writes one record per source entity into the EdlEntityExtractionConfig
DynamoDB table.  All records are safe to re-run — they use put_item which is idempotent.

Prerequisite:
    AWS credentials configured (AWS_PROFILE, AWS_DEFAULT_PROFILE, or instance role).
    The DynamoDB table must already exist (provisioned by Terraform metadata_persistence module).

Records seeded:
    salesforce / salesforce-account         (incremental, watermark on SystemModstamp)
    salesforce / salesforce-contact         (incremental, watermark on SystemModstamp)
    salesforce / salesforce-opportunity     (incremental, watermark on SystemModstamp)
    salesforce / salesforce-contract        (incremental, watermark on SystemModstamp)
    netsuite   / netsuite-customer          (incremental, watermark on lastModifiedDate, disabled)
    mysql-rds  / mysql-rds-contracts        (incremental, watermark on ModifiedOn, table: Contracts)
    mysql-rds  / mysql-rds-contractterms    (incremental, watermark on ModifiedOn, table:
                                             ContractTerms)
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

from contracts.identifier_policy import tenant_scoped_key

# extraction_window_days is capped at 365 (contracts/entity_configuration_contract.py)
# and only ever applies to FULL loads and to an entity's steady-state incremental
# runs where a watermark already exists. An entity's first-ever incremental run
# (no watermark yet) always backfills from epoch regardless of this value —
# see WatermarkRepository.compute_extraction_window(). Keep this small; it is
# not a backfill-window knob.
_INCREMENTAL_EXTRACTION_WINDOW_DAYS = 1


class SeedValidationError(Exception):
    """Raised when a seeded record could not be extracted as written."""


def _validate_connector_params(records: list[dict[str, object]]) -> None:
    """
    Reject a seeded config whose connector_params its source's model would refuse.

    The `netsuite-customer` record shipped with `connector_params: {}` while
    `NetSuiteConnectorParams.record_type` is required under `extra="forbid"`, so extraction for
    that entity raised ValidationError the moment it ran. Unit tests could not see it — the
    defect was in the seed *data*, not the code. Validating here turns that class of defect into
    a pre-deploy failure using the same registry the extraction handler validates against, so
    the two cannot disagree.

    Importing the extraction handler is what registers every source (the G2 property), so this
    also fails if a seeded source_id has no registered connector at all.
    """
    from pydantic import ValidationError

    import connector_runtime.extraction_pipeline_handler  # noqa: F401  (registers sources)
    from connector_runtime.registry import connector_registry

    problems: list[str] = []
    for record in records:
        source_id = str(record["source_id"])
        entity_id = str(record["entity_id"])
        params_model = connector_registry.get_params_model(source_id)
        if params_model is None:
            continue
        try:
            params_model.model_validate(record.get("connector_params") or {})
        except ValidationError as exc:
            problems.append(f"  {source_id} / {entity_id}: {exc}")

    if problems:
        raise SeedValidationError(
            "Seeded connector_params would fail at extraction time:\n"
            + "\n".join(problems)
            + "\n\nFix the seed data; do not deploy a config that cannot run."
        )


def _table_name() -> str:
    return "EdlEntityExtractionConfig"


def _account_id(region: str) -> str:
    """Resolve the AWS account ID of the caller's credentials.

    Bucket names are suffixed with the account ID rather than the
    environment name — S3 bucket names are unique across all of AWS, and
    each environment is already a separate AWS account, so the account ID
    is what actually guarantees no collision with dev/staging/prod.
    """
    return boto3.client("sts", region_name=region).get_caller_identity()["Account"]


def _raw_prefix(account_id: str, source_id: str, entity_id: str, tenant_code: str = "demo") -> str:
    """Full s3:// URI for the raw layer partition root.

    With multi-tenancy: {tenant_code}/{source_id}/{entity_id}/
    """
    return f"s3://edl-raw-{account_id}/{tenant_code}/{source_id}/{entity_id}/"


def _sage_raw_prefix(
    account_id: str, product_name: str, entity_id: str, tenant_code: str = "demo"
) -> str:
    """Full s3:// URI for Sage raw layer partition root."""
    return f"s3://edl-raw-{account_id}/{tenant_code}/sage-{product_name}/{entity_id}/"


def _snapshot_prefix(
    account_id: str, source_id: str, entity_id: str, tenant_code: str = "demo"
) -> str:
    """Full s3:// URI for schema snapshot storage."""
    return f"s3://edl-schema-snapshots-{account_id}/{tenant_code}/{source_id}/{entity_id}/"


def _build_records(account_id: str, tenant_code: str = "demo") -> list[dict[str, object]]:
    """Build entity extraction config records with account-specific s3:// URIs."""
    return [
        {
            "source_id": "salesforce",
            "entity_id": "salesforce-account",
            "config_version": "1.1.0",
            "tenant_code": tenant_code,
            "load_type": "incremental",
            "watermark_field": "SystemModstamp",
            "extraction_window_days": _INCREMENTAL_EXTRACTION_WINDOW_DAYS,
            "watermark_overlap_hours": 1,
            "field_mode": "all",
            "include_fields": [],
            "exclude_fields": [],
            "target_raw_s3_prefix": _raw_prefix(
                account_id, "salesforce", "salesforce-account", tenant_code
            ),
            "schema_snapshot_s3_prefix": _snapshot_prefix(
                account_id, "salesforce", "salesforce-account", tenant_code
            ),
            "output_format": "parquet",
            "connector_params": {"object_name": "Account"},
            "schedule_cron": "cron(0 2 * * ? *)",
            "schedule_enabled": True,
            "schedule_timezone": "UTC",
            # SCD Type 1 merge with tombstone soft-delete.
            # Salesforce hard-deletes: deleted accounts disappear from the API
            # and are not captured in the incremental delta.  They persist in
            # the curated layer as tombstones (last-known state, is_active=False
            # if deactivated before deletion).  soft_delete_field is None because
            # there is no deletion flag in the extracted data — a future enhancement
            # will add IsDeleted=true ALL ROWS SOQL support to track hard-deletes.
            "primary_key_field": "account_id",
            "soft_delete_field": None,
            "active": True,
        },
        {
            "source_id": "salesforce",
            "entity_id": "salesforce-contact",
            "config_version": "1.0.0",
            "load_type": "incremental",
            "watermark_field": "SystemModstamp",
            "extraction_window_days": _INCREMENTAL_EXTRACTION_WINDOW_DAYS,
            "watermark_overlap_hours": 1,
            "field_mode": "all",
            "include_fields": [],
            "exclude_fields": ["IsDeleted"],
            "target_raw_s3_prefix": _raw_prefix(
                account_id, "salesforce", "salesforce-contact", tenant_code
            ),
            "schema_snapshot_s3_prefix": _snapshot_prefix(
                account_id, "salesforce", "salesforce-contact", tenant_code
            ),
            "output_format": "parquet",
            "connector_params": {"object_name": "Contact"},
            "schedule_cron": "cron(15 2 * * ? *)",
            "schedule_enabled": True,
            "schedule_timezone": "UTC",
            # SCD Type 1 merge: curated layer always holds full current state.
            # Salesforce Contact uses hard-delete (records disappear from API),
            # so soft_delete_field is None — deletions tracked via full-load
            # when the entity is eventually switched to full load for that need.
            "primary_key_field": "contact_id",
            "soft_delete_field": None,
            "active": True,
            "tenant_code": tenant_code,
        },
        {
            "source_id": "salesforce",
            "entity_id": "salesforce-opportunity",
            "config_version": "1.0.0",
            "tenant_code": tenant_code,
            "load_type": "incremental",
            "watermark_field": "SystemModstamp",
            "extraction_window_days": _INCREMENTAL_EXTRACTION_WINDOW_DAYS,
            "watermark_overlap_hours": 1,
            "field_mode": "all",
            "include_fields": [],
            "exclude_fields": ["IsDeleted"],
            "target_raw_s3_prefix": _raw_prefix(
                account_id, "salesforce", "salesforce-opportunity", tenant_code
            ),
            "schema_snapshot_s3_prefix": _snapshot_prefix(
                account_id, "salesforce", "salesforce-opportunity", tenant_code
            ),
            "output_format": "parquet",
            "connector_params": {"object_name": "Opportunity"},
            "schedule_cron": "cron(20 2 * * ? *)",
            "schedule_enabled": True,
            "schedule_timezone": "UTC",
            # SCD Type 1 merge: curated layer always holds full current state.
            # Salesforce Opportunity uses hard-delete (records disappear from
            # the API), so soft_delete_field is None — same rationale as Contact.
            "primary_key_field": "opportunity_id",
            "soft_delete_field": None,
            "active": True,
        },
        {
            "source_id": "salesforce",
            "entity_id": "salesforce-contract",
            "config_version": "1.0.0",
            "tenant_code": tenant_code,
            "load_type": "incremental",
            "watermark_field": "SystemModstamp",
            "extraction_window_days": _INCREMENTAL_EXTRACTION_WINDOW_DAYS,
            "watermark_overlap_hours": 1,
            "field_mode": "all",
            "include_fields": [],
            "exclude_fields": ["IsDeleted"],
            "target_raw_s3_prefix": _raw_prefix(
                account_id, "salesforce", "salesforce-contract", tenant_code
            ),
            "schema_snapshot_s3_prefix": _snapshot_prefix(
                account_id, "salesforce", "salesforce-contract", tenant_code
            ),
            "output_format": "parquet",
            "connector_params": {"object_name": "Contract"},
            "schedule_cron": "cron(25 2 * * ? *)",
            "schedule_enabled": True,
            "schedule_timezone": "UTC",
            # SCD Type 1 merge: curated layer always holds full current state.
            # Salesforce Contract uses hard-delete (records disappear from
            # the API), so soft_delete_field is None — same rationale as Contact.
            "primary_key_field": "sales_contract_id",
            "soft_delete_field": None,
            "active": True,
        },
        {
            "source_id": "netsuite",
            "entity_id": "netsuite-customer",
            "config_version": "1.0.0",
            "load_type": "incremental",
            "watermark_field": "lastModifiedDate",
            "extraction_window_days": _INCREMENTAL_EXTRACTION_WINDOW_DAYS,
            "watermark_overlap_hours": 2,
            "field_mode": "all",
            "include_fields": [],
            "exclude_fields": [],
            "target_raw_s3_prefix": _raw_prefix(
                account_id, "netsuite", "netsuite-customer", tenant_code
            ),
            "schema_snapshot_s3_prefix": _snapshot_prefix(
                account_id, "netsuite", "netsuite-customer", tenant_code
            ),
            "output_format": "parquet",
            # `record_type` is required by NetSuiteConnectorParams (extra="forbid"), so the
            # previous `{}` made this entity raise ValidationError the moment extraction ran.
            # Recorded as KNOWN_GAPS item 9; `_validate_connector_params` below now makes a
            # config that cannot construct its params model a seed-time failure.
            "connector_params": {"record_type": "customer"},
            "schedule_cron": None,
            "schedule_enabled": False,
            "schedule_timezone": "UTC",
            "active": False,
            "tenant_code": tenant_code,
        },
        # ── Add new MySQL tables here — copy this block and adjust entity_id,
        # ── connector_params["table_name"], schedule_cron, and load_type.
        # ── After adding: make seed-entity-config && make seed-schedules
        {
            "source_id": "mysql-rds",
            "entity_id": "mysql-rds-contracts",
            "config_version": "1.1.0",
            "load_type": "incremental",
            "watermark_field": "ModifiedOn",
            "extraction_window_days": _INCREMENTAL_EXTRACTION_WINDOW_DAYS,
            "watermark_overlap_hours": 1,
            "field_mode": "all",
            "include_fields": [],
            "exclude_fields": [],
            "target_raw_s3_prefix": _raw_prefix(
                account_id, "mysql-rds", "mysql-rds-contracts", tenant_code
            ),
            "schema_snapshot_s3_prefix": _snapshot_prefix(
                account_id, "mysql-rds", "mysql-rds-contracts", tenant_code
            ),
            "output_format": "parquet",
            "connector_params": {"table_name": "Contracts"},
            "schedule_cron": "cron(30 2 * * ? *)",
            "schedule_enabled": True,
            "schedule_timezone": "UTC",
            # SCD Type 1 merge with tombstone soft-delete.
            # MySQL Contracts uses a soft-delete flag (IsDelete → canonical is_deleted).
            # soft_delete_field is intentionally None: deleted contracts are KEPT in
            # the curated layer and analytics with is_deleted=True as a tombstone,
            # never physically removed.  BI queries filter WHERE is_deleted = false
            # to see only active contracts.  This preserves the full audit trail.
            "primary_key_field": "contract_id",
            "soft_delete_field": None,
            "active": True,
            "tenant_code": tenant_code,
        },
        {
            "source_id": "mysql-rds",
            "entity_id": "mysql-rds-contractterms",
            "config_version": "1.0.0",
            "load_type": "incremental",
            "watermark_field": "ModifiedOn",
            "extraction_window_days": _INCREMENTAL_EXTRACTION_WINDOW_DAYS,
            "watermark_overlap_hours": 1,
            "field_mode": "all",
            "include_fields": [],
            "exclude_fields": [],
            "target_raw_s3_prefix": _raw_prefix(
                account_id, "mysql-rds", "mysql-rds-contractterms", tenant_code
            ),
            "schema_snapshot_s3_prefix": _snapshot_prefix(
                account_id, "mysql-rds", "mysql-rds-contractterms", tenant_code
            ),
            "output_format": "parquet",
            "connector_params": {"table_name": "ContractTerms"},
            "schedule_cron": "cron(35 2 * * ? *)",
            "schedule_enabled": True,
            "schedule_timezone": "UTC",
            # Same SCD Type 1 / tombstone convention as mysql-rds-contracts —
            # assumed ModifiedOn/Id column names; adjust if ContractTerms uses
            # different watermark/primary-key columns than Contracts.
            "primary_key_field": "contract_term_id",
            "soft_delete_field": None,
            "active": True,
            "tenant_code": tenant_code,
        },
        # ── Sage Intacct ─────────────────────────────────────────────────────
        {
            "source_id": "sage",
            "entity_id": "sage-intacct-customer",
            "config_version": "1.0.0",
            "load_type": "incremental",
            "watermark_field": "auditInfo.modifiedAt",
            "extraction_window_days": _INCREMENTAL_EXTRACTION_WINDOW_DAYS,
            "watermark_overlap_hours": 1,
            "field_mode": "all",
            "include_fields": [],
            "exclude_fields": [],
            "target_raw_s3_prefix": _sage_raw_prefix(
                account_id, "intacct", "sage-intacct-customer", tenant_code
            ),
            "schema_snapshot_s3_prefix": _snapshot_prefix(
                account_id, "sage", "sage-intacct-customer", tenant_code
            ),
            "output_format": "parquet",
            "connector_params": {
                "sage_product": "intacct",
                "object_path": "accounts-receivable/customer",
            },
            "schedule_cron": "cron(45 2 * * ? *)",
            "schedule_enabled": True,
            "schedule_timezone": "UTC",
            "active": True,
            "tenant_code": tenant_code,
        },
        {
            "source_id": "sage",
            "entity_id": "sage-intacct-vendor",
            "config_version": "1.0.0",
            "load_type": "incremental",
            "watermark_field": "auditInfo.modifiedAt",
            "extraction_window_days": _INCREMENTAL_EXTRACTION_WINDOW_DAYS,
            "watermark_overlap_hours": 1,
            "field_mode": "all",
            "include_fields": [],
            "exclude_fields": [],
            "target_raw_s3_prefix": _sage_raw_prefix(
                account_id, "intacct", "sage-intacct-vendor", tenant_code
            ),
            "schema_snapshot_s3_prefix": _snapshot_prefix(
                account_id, "sage", "sage-intacct-vendor", tenant_code
            ),
            "output_format": "parquet",
            "connector_params": {
                "sage_product": "intacct",
                "object_path": "accounts-payable/vendor",
            },
            "schedule_cron": "cron(50 2 * * ? *)",
            "schedule_enabled": True,
            "schedule_timezone": "UTC",
            "active": True,
            "tenant_code": tenant_code,
        },
        {
            "source_id": "sage",
            "entity_id": "sage-intacct-arinvoice",
            "config_version": "1.0.0",
            "load_type": "incremental",
            "watermark_field": "auditInfo.modifiedAt",
            "extraction_window_days": _INCREMENTAL_EXTRACTION_WINDOW_DAYS,
            "watermark_overlap_hours": 1,
            "field_mode": "all",
            "include_fields": [],
            "exclude_fields": [],
            "target_raw_s3_prefix": _sage_raw_prefix(
                account_id, "intacct", "sage-intacct-arinvoice", tenant_code
            ),
            "schema_snapshot_s3_prefix": _snapshot_prefix(
                account_id, "sage", "sage-intacct-arinvoice", tenant_code
            ),
            "output_format": "parquet",
            "connector_params": {
                "sage_product": "intacct",
                "object_path": "accounts-receivable/invoice",
            },
            "schedule_cron": "cron(55 2 * * ? *)",
            "schedule_enabled": True,
            "schedule_timezone": "UTC",
            "active": True,
            "tenant_code": tenant_code,
        },
        {
            "source_id": "sage",
            "entity_id": "sage-intacct-apbill",
            "config_version": "1.0.0",
            "load_type": "incremental",
            "watermark_field": "auditInfo.modifiedAt",
            "extraction_window_days": _INCREMENTAL_EXTRACTION_WINDOW_DAYS,
            "watermark_overlap_hours": 1,
            "field_mode": "all",
            "include_fields": [],
            "exclude_fields": [],
            "target_raw_s3_prefix": _sage_raw_prefix(
                account_id, "intacct", "sage-intacct-apbill", tenant_code
            ),
            "schema_snapshot_s3_prefix": _snapshot_prefix(
                account_id, "sage", "sage-intacct-apbill", tenant_code
            ),
            "output_format": "parquet",
            "connector_params": {"sage_product": "intacct", "object_path": "accounts-payable/bill"},
            "schedule_cron": "cron(5 3 * * ? *)",
            "schedule_enabled": True,
            "schedule_timezone": "UTC",
            "active": True,
            "tenant_code": tenant_code,
        },
        # ── Sage X3 ────────────────────────────────────────────────────────────
        {
            "source_id": "sage",
            "entity_id": "sage-x3-customer",
            "config_version": "1.0.0",
            "load_type": "incremental",
            "watermark_field": "MODDAT_0",
            "extraction_window_days": _INCREMENTAL_EXTRACTION_WINDOW_DAYS,
            "watermark_overlap_hours": 1,
            "field_mode": "all",
            "include_fields": [],
            "exclude_fields": [],
            "target_raw_s3_prefix": _sage_raw_prefix(
                account_id, "x3", "sage-x3-customer", tenant_code
            ),
            "schema_snapshot_s3_prefix": _snapshot_prefix(
                account_id, "sage", "sage-x3-customer", tenant_code
            ),
            "output_format": "parquet",
            "connector_params": {"sage_product": "x3", "object_path": "BPCUSTOMER"},
            "schedule_cron": "cron(55 2 * * ? *)",
            "schedule_enabled": True,
            "schedule_timezone": "UTC",
            "active": True,
            "tenant_code": tenant_code,
        },
        {
            "source_id": "sage",
            "entity_id": "sage-x3-supplier",
            "config_version": "1.0.0",
            "load_type": "incremental",
            "watermark_field": "MODDAT_0",
            "extraction_window_days": _INCREMENTAL_EXTRACTION_WINDOW_DAYS,
            "watermark_overlap_hours": 1,
            "field_mode": "all",
            "include_fields": [],
            "exclude_fields": [],
            "target_raw_s3_prefix": _sage_raw_prefix(
                account_id, "x3", "sage-x3-supplier", tenant_code
            ),
            "schema_snapshot_s3_prefix": _snapshot_prefix(
                account_id, "sage", "sage-x3-supplier", tenant_code
            ),
            "output_format": "parquet",
            "connector_params": {"sage_product": "x3", "object_path": "BPSUPPLIER"},
            "schedule_cron": "cron(0 3 * * ? *)",
            "schedule_enabled": False,
            "schedule_timezone": "UTC",
            "active": True,
            "tenant_code": tenant_code,
        },
    ]


def seed(environment: str, region: str, dry_run: bool = False, tenant_code: str = "demo") -> None:
    table_name = _table_name()
    account_id = _account_id(region)
    records = _build_records(account_id, tenant_code=tenant_code)
    # Before the dry-run branch on purpose: a dry run should surface an unusable config too.
    _validate_connector_params(records)
    print(
        f"Target table: {table_name}  "
        f"(account: {account_id}, region: {region}, tenant_code: {tenant_code})"
    )

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
        # PK is the tenant-scoped composite (ARCH-1/ARCH-03), matching
        # ConfigurationRepositoryClient so the extraction pipeline reads it back.
        item["source_id"] = tenant_scoped_key(str(rec["tenant_code"]), str(rec["source_id"]))
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
    parser.add_argument("--tenant-code", default="demo", help="Tenant code slug (default: demo).")
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

    seed(args.environment, args.region, dry_run=args.dry_run, tenant_code=args.tenant_code)


if __name__ == "__main__":
    main()
