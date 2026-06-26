#!/usr/bin/env python3
"""
Local runner for the MySQL RDS connector.

Reads credentials from AWS Secrets Manager, entity config from DynamoDB,
executes the full extraction pipeline locally, and writes raw Parquet
data to the dev S3 raw-layer bucket — exactly as the Lambda would do.

Usage:
    # Full load of contracts table
    AWS_PROFILE=dev python scripts/run_mysql_connector_local.py \
        --entity-id mysql-rds-contracts

    # Incremental load of orders table (last N days)
    AWS_PROFILE=dev python scripts/run_mysql_connector_local.py \
        --entity-id mysql-rds-orders

    # Override extraction window
    AWS_PROFILE=dev python scripts/run_mysql_connector_local.py \
        --entity-id mysql-rds-orders --window-days 7

    # Dry-run: connect + discover schema only, no S3 write
    AWS_PROFILE=dev python scripts/run_mysql_connector_local.py \
        --entity-id mysql-rds-orders --dry-run
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
import connector_runtime.adapters.mysql_rds.mysql_rds_connector  # noqa: E402, F401

from connector_runtime.adapters.mysql_rds.mysql_rds_connector import MySqlRdsConnector  # noqa: E402
from connector_runtime.adapters.mysql_rds.mysql_rds_credentials_client import (  # noqa: E402
    MySqlRdsCredentialsClient,
)
from connector_runtime.adapters.mysql_rds.mysql_rds_raw_layer_writer import (  # noqa: E402
    MySqlRdsRawLayerWriter,
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

# Map entity_id → MySQL table name (matches DynamoDB entity configs)
_ENTITY_TABLE_MAP: dict[str, str] = {
    "mysql-rds-contracts": "Contracts",
}


# ---------------------------------------------------------------------------
# Step 1 + 2: Connection test
# ---------------------------------------------------------------------------

def test_connection(region: str, environment: str) -> bool:
    """Fetch credentials from Secrets Manager and open a test connection."""
    import pymysql
    import pymysql.cursors

    print("\n[1/4] Fetching credentials from Secrets Manager ...")
    creds_client = MySqlRdsCredentialsClient(environment=environment, region_name=region)
    params = creds_client.get_connection_parameters()
    print(f"      host={params.host}  port={params.port}  db={params.database}  user={params.username}")

    print("[2/4] Opening MySQL connection (SSL enforced) ...")
    try:
        conn = pymysql.connect(
            host=params.host,
            port=params.port,
            user=params.username,
            password=params.password,
            database=params.database,
            ssl_disabled=False,
            connect_timeout=10,
            cursorclass=pymysql.cursors.DictCursor,
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT VERSION() AS version, NOW() AS server_time, DATABASE() AS current_db"
            )
            row = cur.fetchone()
            print(f"      MySQL version : {row['version']}")
            print(f"      Server time   : {row['server_time']}")
            print(f"      Database      : {row['current_db']}")
        conn.close()
        print("      Connection OK\n")
        return True
    except Exception as exc:
        print(f"      Connection FAILED: {exc}\n", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Step 3: Schema discovery
# ---------------------------------------------------------------------------

_EXCLUDE_FIELDS: list[str] = ["ErrorMessage", "DSRequest"]


def discover_schema(connector: MySqlRdsConnector, entity_id: str) -> FieldContract:
    """Discover columns via information_schema and print them."""
    print("[3/4] Discovering schema via information_schema ...")
    field_contract = connector.discover_queryable_fields(
        source_id="mysql-rds",
        entity_id=entity_id,
        field_mode=FieldMode.ALL,
        include_fields=[],
        exclude_fields=_EXCLUDE_FIELDS,
    )
    print(f"      Discovered {len(field_contract.fields)} columns:")
    for fd in field_contract.fields:
        nullable = "NULL" if fd.is_nullable else "NOT NULL"
        print(f"        {fd.name:<30} {fd.data_type:<20} {nullable}")
    print(f"      Fingerprint: {field_contract.schema_fingerprint}\n")
    return field_contract


# ---------------------------------------------------------------------------
# Step 4: Extraction + S3 write
# ---------------------------------------------------------------------------

def run_extraction(
    connector: MySqlRdsConnector,
    entity_id: str,
    field_contract: FieldContract,
    config: EntityExtractionConfig,
    watermark_lower: str | None,
    watermark_upper: str,
    dry_run: bool,
    raw_s3_bucket: str,
    raw_s3_prefix: str,
    region: str,
    limit: int | None = None,
) -> None:
    """Build query, execute extraction, and stream Parquet to S3 (or peek 5 rows if dry-run)."""
    print("[4/4] Running extraction ...")

    query_contract = connector.build_extraction_query(
        field_contract=field_contract,
        load_type=config.load_type,
        watermark_field=config.watermark_field,
        watermark_lower=watermark_lower,
        watermark_upper=watermark_upper,
        extraction_window_days=config.extraction_window_days,
    )

    if limit is not None:
        query_contract = dataclasses.replace(
            query_contract,
            query_text=f"{query_contract.query_text} LIMIT {limit}",
        )
        print(f"      [TEST] Row limit applied: LIMIT {limit}")

    print(f"      Load type  : {config.load_type.value}")
    print(f"      Query      : {query_contract.query_text}")
    print(f"      Params     : {query_contract.query_parameters}")
    print()

    run_id = f"local-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    extraction_date = datetime.now(UTC).strftime("%Y-%m-%d")

    if dry_run:
        # Use a direct LIMIT 5 query — avoids triggering the 1000-row fetchmany batch
        # that would transfer MBs of mediumtext data before yielding anything.
        import pymysql
        import pymysql.cursors
        from connector_runtime.adapters.mysql_rds.mysql_rds_credentials_client import (
            MySqlRdsCredentialsClient,
        )

        print("      [DRY RUN] Fetching 5 sample rows (LIMIT 5, DSRequest excluded) ...")
        creds = MySqlRdsCredentialsClient(
            environment=config.source_id.split("-")[0] if False else "dev",
            region_name=region,
        ).get_connection_parameters()
        # Build column list excluding the heavy mediumtext column for display
        display_cols = [
            f"`{fd.name}`"
            for fd in field_contract.fields
            if fd.name != "DSRequest"
        ]
        sample_sql = f"SELECT {', '.join(display_cols)} FROM `{connector._table_name}` LIMIT 5"
        conn = pymysql.connect(
            host=creds.host, port=creds.port, user=creds.username,
            password=creds.password, database=creds.database,
            ssl_disabled=False, connect_timeout=10,
            cursorclass=pymysql.cursors.DictCursor,
        )
        with conn.cursor() as cur:
            cur.execute(sample_sql)
            rows = cur.fetchall()
        conn.close()
        print(f"      Got {len(rows)} sample row(s) (DSRequest omitted from display):")
        for row in rows:
            print(f"        {json.dumps(dict(row), default=str)}")
        print("\n      [DRY RUN] Connection + schema + data fetch verified. No S3 write.")
        return

    # Full run — stream directly to S3 in 50k-row chunks (O(chunk) memory, not O(table))
    print("      Streaming records to S3 in batches ...")
    writer = MySqlRdsRawLayerWriter(
        s3_bucket=raw_s3_bucket,
        s3_prefix=raw_s3_prefix,
        region_name=region,
    )
    record_iter = connector.execute_extraction(query_contract=query_contract, run_id=run_id)
    partition_prefix, total_count = writer.write_partition_streaming(
        record_iter=record_iter,
        source_id="mysql-rds",
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
        description="Run the MySQL RDS connector locally against dev AWS resources."
    )
    parser.add_argument(
        "--entity-id",
        required=True,
        choices=list(_ENTITY_TABLE_MAP.keys()),
        help="Entity to extract (maps to MySQL table name).",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=None,
        help="Override extraction_window_days from entity config.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Connect, discover schema, fetch rows — but skip the S3 write.",
    )
    parser.add_argument("--region", default=_REGION)
    parser.add_argument("--environment", default=_ENVIRONMENT)
    parser.add_argument("--raw-s3-bucket", default=_RAW_S3_BUCKET)
    parser.add_argument("--raw-s3-prefix", default=_RAW_S3_PREFIX)
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Append LIMIT N to the query (testing only). Omit for full extraction.",
    )

    args = parser.parse_args()

    entity_id: str = args.entity_id
    table_name: str = _ENTITY_TABLE_MAP[entity_id]
    region: str = args.region
    environment: str = args.environment

    print("=" * 60)
    print("  MySQL RDS Connector — Local Test Runner")
    print("=" * 60)
    print(f"  Entity    : {entity_id}")
    print(f"  Table     : {table_name}")
    print(f"  Env       : {environment}")
    print(f"  Region    : {region}")
    print(f"  S3 bucket : {args.raw_s3_bucket}/{args.raw_s3_prefix}")
    print(f"  Dry run   : {args.dry_run}")
    row_limit_label = str(args.limit) if args.limit else "none (full table)"
    print(f"  Row limit : {row_limit_label}")
    print("=" * 60)

    # ── Steps 1 + 2: Connection test ─────────────────────────────────────────
    if not test_connection(region=region, environment=environment):
        sys.exit(1)

    # ── Load entity config from DynamoDB ─────────────────────────────────────
    config_client = ConfigurationRepositoryClient(environment=environment, region_name=region)
    config = config_client.load_config(source_id="mysql-rds", entity_id=entity_id)

    if args.window_days is not None:
        config = config.model_copy(update={"extraction_window_days": args.window_days})

    print(
        f"  Config loaded : load_type={config.load_type.value}, "
        f"window={config.extraction_window_days}d\n"
    )

    # ── Build connector ───────────────────────────────────────────────────────
    connector = MySqlRdsConnector(
        environment=environment,
        region_name=region,
        table_name=table_name,
    )

    # ── Step 3: Schema discovery ──────────────────────────────────────────────
    field_contract = discover_schema(connector=connector, entity_id=entity_id)

    # ── Resolve watermark bounds ──────────────────────────────────────────────
    watermark_lower: str | None = None
    watermark_upper: str = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    if config.load_type == LoadType.INCREMENTAL:
        watermark_repo = WatermarkRepository(environment=environment, region_name=region)
        record = watermark_repo.get_watermark(source_id="mysql-rds", entity_id=entity_id)
        if record is not None:
            watermark_lower = record.last_successful_watermark.strftime("%Y-%m-%dT%H:%M:%SZ")
            print(f"  Watermark lower : {watermark_lower}")
        else:
            print("  No prior watermark — running as first-time incremental (full range).")

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
        limit=args.limit,
    )

    print("\n  Done.\n")


if __name__ == "__main__":
    main()
