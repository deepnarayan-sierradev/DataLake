#!/usr/bin/env python3
"""
Local runner for the Salesforce connector.

Reads credentials from AWS Secrets Manager (OAuth 2.0 client_credentials flow),
entity config from DynamoDB, discovers Salesforce object schema via Describe API,
executes the full extraction pipeline locally, and writes raw Parquet data to the
dev S3 raw-layer bucket — exactly as the Lambda would do.

Usage:
    # Full load of Account object
    AWS_PROFILE=dev python scripts/run_salesforce_connector_local.py \
        --entity-id salesforce-account

    # Incremental load of Contact object (uses watermark from DynamoDB)
    AWS_PROFILE=dev python scripts/run_salesforce_connector_local.py \
        --entity-id salesforce-contact

    # Dry-run: connect + discover schema only, no S3 write
    AWS_PROFILE=dev python scripts/run_salesforce_connector_local.py \
        --entity-id salesforce-account --dry-run

    # Override extraction window days (incremental only)
    AWS_PROFILE=dev python scripts/run_salesforce_connector_local.py \
        --entity-id salesforce-contact --window-days 7
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

# ── Make project root importable when run directly ──────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ── Force boto3 to use the dev profile when AWS_PROFILE is not already set ──
if "AWS_PROFILE" not in os.environ:
    os.environ["AWS_PROFILE"] = "dev"
if "AWS_DEFAULT_REGION" not in os.environ:
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"

# Register connector by importing its module (triggers @register decorator)
import connector_runtime.adapters.salesforce.salesforce_connector  # noqa: E402, F401

from connector_runtime.adapters.salesforce.salesforce_connector import SalesforceConnector  # noqa: E402
from connector_runtime.adapters.salesforce.salesforce_auth_client import SalesforceAuthClient  # noqa: E402
from connector_runtime.adapters.salesforce.salesforce_raw_layer_writer import (  # noqa: E402
    SalesforceRawLayerWriter,
)
from connector_runtime.configuration_repository.configuration_repository import (  # noqa: E402
    ConfigurationRepositoryClient,
)
from connector_runtime.interfaces.connector_interface import FieldContract  # noqa: E402
from contracts.entity_configuration_contract import (  # noqa: E402
    EntityExtractionConfig,
    FieldMode,
    LoadType,
)
from observability.structured_logger import get_platform_logger  # noqa: E402
from watermark_management.watermark_repository.watermark_repository import (  # noqa: E402
    WatermarkRepository,
)

_logger = get_platform_logger(__name__)

_ENVIRONMENT = "dev"
_REGION = "us-east-1"
_RAW_S3_BUCKET = "dev-edl-raw-layer"
_RAW_S3_PREFIX = "raw"
_SOURCE_ID = "salesforce"

# Map entity_id → Salesforce object API name
_ENTITY_OBJECT_MAP: dict[str, str] = {
    "salesforce-account": "Account",
    "salesforce-contact": "Contact",
}


# ---------------------------------------------------------------------------
# Step 1 + 2: Connection test (OAuth token fetch)
# ---------------------------------------------------------------------------

def test_connection(region: str, environment: str) -> tuple[bool, SalesforceAuthClient]:
    """Fetch credentials from Secrets Manager, obtain an OAuth token, confirm connectivity."""
    print("\n[1/4] Fetching credentials from Secrets Manager ...")
    auth_client = SalesforceAuthClient(environment=environment, region_name=region)

    print("[2/4] Obtaining Salesforce OAuth 2.0 access token ...")
    try:
        token = auth_client.get_access_token()
        instance_url = auth_client.instance_url
        # Log only first/last 4 chars of token as a liveness indicator — never the full value.
        token_hint = f"{token[:4]}...{token[-4:]}" if len(token) >= 8 else "****"
        print(f"      instance_url  : {instance_url}")
        print(f"      token (hint)  : {token_hint}")
        print("      OAuth OK\n")
        return True, auth_client
    except Exception as exc:
        print(f"      OAuth FAILED  : {exc}\n", file=sys.stderr)
        return False, auth_client


# ---------------------------------------------------------------------------
# Step 3: Schema discovery
# ---------------------------------------------------------------------------

def discover_schema(
    connector: SalesforceConnector,
    entity_id: str,
    config: EntityExtractionConfig,
) -> FieldContract:
    """Discover fields via Salesforce Describe API and print a summary."""
    print("[3/4] Discovering schema via Salesforce Describe API ...")
    field_contract = connector.discover_queryable_fields(
        source_id=_SOURCE_ID,
        entity_id=entity_id,
        field_mode=config.field_mode,
        include_fields=config.include_fields,
        exclude_fields=config.exclude_fields,
    )
    print(f"      Discovered {len(field_contract.fields)} queryable fields:")
    for fd in field_contract.fields[:20]:  # Print first 20 to avoid wall of text
        nullable = "NULL" if fd.is_nullable else "NOT NULL"
        print(f"        {fd.name:<40} {fd.data_type:<20} {nullable}")
    if len(field_contract.fields) > 20:
        print(f"        ... and {len(field_contract.fields) - 20} more fields (omitted for brevity)")
    print(f"      Fingerprint: {field_contract.schema_fingerprint}\n")
    return field_contract


# ---------------------------------------------------------------------------
# Step 4: Extraction + S3 write
# ---------------------------------------------------------------------------

def run_extraction(
    connector: SalesforceConnector,
    entity_id: str,
    field_contract: FieldContract,
    config: EntityExtractionConfig,
    watermark_lower: str | None,
    watermark_upper: str,
    dry_run: bool,
    raw_s3_bucket: str,
    raw_s3_prefix: str,
    region: str,
) -> None:
    """Build SOQL query, execute via Bulk API 2.0, and stream Parquet to S3 (or peek rows if dry-run)."""
    print("[4/4] Running extraction ...")

    query_contract = connector.build_extraction_query(
        field_contract=field_contract,
        load_type=config.load_type,
        watermark_field=config.watermark_field,
        watermark_lower=watermark_lower,
        watermark_upper=watermark_upper,
        extraction_window_days=config.extraction_window_days,
    )

    print(f"      Load type  : {config.load_type.value}")
    print(f"      SOQL       : {query_contract.query_text}")
    if query_contract.query_parameters:
        print(f"      Params     : {query_contract.query_parameters}")
    print()

    run_id = f"local-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    extraction_date = datetime.now(UTC).strftime("%Y-%m-%d")

    if dry_run:
        # For dry-run: execute via Bulk API but collect only first 5 records.
        # This validates end-to-end connectivity without writing to S3.
        print("      [DRY RUN] Submitting Bulk API job and collecting first 5 records ...")
        sample_rows: list[dict] = []
        try:
            for record in connector.execute_extraction(
                query_contract=query_contract,
                run_id=run_id,
            ):
                sample_rows.append(record.payload)
                if len(sample_rows) >= 5:
                    break
        except Exception as exc:
            print(f"      [DRY RUN] Extraction error: {exc}", file=sys.stderr)
            return

        print(f"      Got {len(sample_rows)} sample row(s):")
        for row in sample_rows:
            print(f"        {json.dumps(row, default=str)}")
        print("\n      [DRY RUN] OAuth + schema + Bulk API query verified. No S3 write.")
        return

    # Full run — stream directly to S3 in 50k-row chunks (O(chunk) memory, not O(table))
    print("      Streaming records to S3 in batches ...")
    writer = SalesforceRawLayerWriter(
        s3_bucket=raw_s3_bucket,
        s3_prefix=raw_s3_prefix,
        region_name=region,
    )
    record_iter = connector.execute_extraction(
        query_contract=query_contract,
        run_id=run_id,
    )
    partition_prefix, total_count = writer.write_partition_streaming(
        record_iter=record_iter,
        source_id=_SOURCE_ID,
        entity_id=entity_id,
        run_id=run_id,
        schema_fingerprint=field_contract.schema_fingerprint,
        extraction_date=extraction_date,
    )

    print(f"\n      Records written : {total_count}")
    print(f"      S3 prefix       : s3://{raw_s3_bucket}/{partition_prefix}/")
    print(
        f"      Browse          : https://s3.console.aws.amazon.com/s3/buckets/"
        f"{raw_s3_bucket}?prefix={partition_prefix}/"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Salesforce connector locally against dev AWS resources."
    )
    parser.add_argument(
        "--entity-id",
        required=True,
        choices=list(_ENTITY_OBJECT_MAP.keys()),
        help="Entity to extract (maps to Salesforce object API name).",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=None,
        help="Override extraction_window_days from entity config (incremental only).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Connect + discover schema + fetch sample rows, but skip the S3 write.",
    )
    parser.add_argument("--region", default=_REGION)
    parser.add_argument("--environment", default=_ENVIRONMENT)
    parser.add_argument("--raw-s3-bucket", default=_RAW_S3_BUCKET)
    parser.add_argument("--raw-s3-prefix", default=_RAW_S3_PREFIX)

    args = parser.parse_args()

    entity_id: str = args.entity_id
    object_name: str = _ENTITY_OBJECT_MAP[entity_id]
    region: str = args.region
    environment: str = args.environment

    print("=" * 60)
    print("  Salesforce Connector — Local Test Runner")
    print("=" * 60)
    print(f"  Entity      : {entity_id}")
    print(f"  SF Object   : {object_name}")
    print(f"  Env         : {environment}")
    print(f"  Region      : {region}")
    print(f"  S3 bucket   : {args.raw_s3_bucket}/{args.raw_s3_prefix}")
    print(f"  Dry run     : {args.dry_run}")
    print("=" * 60)

    # ── Steps 1 + 2: OAuth connection test ───────────────────────────────────
    ok, _auth = test_connection(region=region, environment=environment)
    if not ok:
        sys.exit(1)

    # ── Load entity config from DynamoDB ─────────────────────────────────────
    config_client = ConfigurationRepositoryClient(environment=environment, region_name=region)
    config = config_client.load_config(source_id=_SOURCE_ID, entity_id=entity_id)

    if args.window_days is not None:
        config = config.model_copy(update={"extraction_window_days": args.window_days})

    print(
        f"  Config loaded : load_type={config.load_type.value}, "
        f"window={config.extraction_window_days}d, "
        f"watermark_field={config.watermark_field}\n"
    )

    # ── Build connector ───────────────────────────────────────────────────────
    connector = SalesforceConnector(
        environment=environment,
        region_name=region,
        object_name=object_name,
    )

    # ── Step 3: Schema discovery ──────────────────────────────────────────────
    field_contract = discover_schema(connector=connector, entity_id=entity_id, config=config)

    # ── Resolve watermark bounds ──────────────────────────────────────────────
    watermark_lower: str | None = None
    watermark_upper: str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    if config.load_type == LoadType.INCREMENTAL:
        watermark_repo = WatermarkRepository(environment=environment, region_name=region)
        record = watermark_repo.get_watermark(source_id=_SOURCE_ID, entity_id=entity_id)
        if record is not None:
            watermark_lower = record.last_successful_watermark.strftime("%Y-%m-%dT%H:%M:%SZ")
            print(f"  Watermark lower : {watermark_lower}")
        else:
            # First-time incremental run — use an epoch lower bound to capture all records.
            # The upper bound limits how much data we pull on this first pass.
            watermark_lower = "2000-01-01T00:00:00Z"
            print(f"  No prior watermark — first-time incremental, lower bound set to {watermark_lower}")

    print(f"  Watermark upper : {watermark_upper}\n")

    # ── Step 4: Extraction + optional S3 write ────────────────────────────────
    run_extraction(
        connector=connector,
        entity_id=entity_id,
        field_contract=field_contract,
        config=config,
        watermark_lower=watermark_lower,
        watermark_upper=watermark_upper,
        dry_run=args.dry_run,
        raw_s3_bucket=args.raw_s3_bucket,
        raw_s3_prefix=args.raw_s3_prefix,
        region=region,
    )

    print("\n  Done.\n")


if __name__ == "__main__":
    main()
