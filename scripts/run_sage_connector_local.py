#!/usr/bin/env python3
"""
Local runner for the Sage ERP connector (Intacct and X3).

Reads credentials from AWS Secrets Manager (OAuth 2.0 client_credentials),
entity config from DynamoDB, discovers object schema via the product-specific
metadata client, and executes paginated extraction — exactly as the Lambda does.

Usage:
    # Intacct dry-run: OAuth + schema discovery + first 5 records, no S3 write
    AWS_PROFILE=dev python scripts/run_sage_connector_local.py \\
        --entity-id sage-intacct-customer --dry-run

    # Intacct full run: extract all records and write Parquet to S3 raw layer
    AWS_PROFILE=dev python scripts/run_sage_connector_local.py \\
        --entity-id sage-intacct-customer

    # X3 dry-run
    AWS_PROFILE=dev python scripts/run_sage_connector_local.py \\
        --entity-id sage-x3-customer --dry-run

    # X3 supplier dry-run
    AWS_PROFILE=dev python scripts/run_sage_connector_local.py \\
        --entity-id sage-x3-supplier --dry-run

    # Override extraction window for incremental runs (days)
    AWS_PROFILE=dev python scripts/run_sage_connector_local.py \\
        --entity-id sage-intacct-customer --window-days 7

Note:
    Local scripts cannot write to dev-edl-raw-layer (bucket policy restricts
    writes to the dev-extraction-runtime-role Lambda IAM role only).
    Use --dry-run for local connectivity and schema validation.
    Full extraction must be triggered via Step Functions.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

# ── Make project root importable when run directly ──────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import os

# ── Default to dev profile/region if not already configured ─────────────────
if "AWS_PROFILE" not in os.environ:
    os.environ["AWS_PROFILE"] = "dev"
if "AWS_DEFAULT_REGION" not in os.environ:
    os.environ["AWS_DEFAULT_REGION"] = "us-east-1"

# Register the Sage connector at import time (triggers @register decorator)
import connector_runtime.adapters.sage.sage_connector  # noqa: E402, F401

from connector_runtime.adapters.sage.common.sage_credential_manager import SageCredentialManager  # noqa: E402
from connector_runtime.adapters.sage.common.sage_http_client import SageHttpClient  # noqa: E402
from connector_runtime.adapters.sage.common.sage_product_registry import resolve_product_strategies  # noqa: E402
from connector_runtime.adapters.sage.sage_connector import SageConnector  # noqa: E402
from connector_runtime.configuration_repository.configuration_repository import (  # noqa: E402
    ConfigurationRepositoryClient,
)
from connector_runtime.interfaces.connector_interface import FieldContract  # noqa: E402
from contracts.entity_configuration_contract import EntityExtractionConfig, LoadType  # noqa: E402
from observability.structured_logger import get_platform_logger  # noqa: E402
from watermark_management.watermark_repository.watermark_repository import WatermarkRepository  # noqa: E402

_logger = get_platform_logger(__name__)

_ENVIRONMENT = "dev"
_REGION = "us-east-1"
_RAW_S3_BUCKET = "dev-edl-raw-layer"
_SOURCE_ID = "sage"

# Known entity IDs and their Sage object paths
_ENTITY_CONFIG: dict[str, dict[str, str]] = {
    "sage-intacct-customer": {
        "sage_product": "intacct",
        "object_path": "accounts-receivable/customer",
    },
    "sage-intacct-vendor": {
        "sage_product": "intacct",
        "object_path": "accounts-payable/vendor",
    },
    "sage-x3-customer": {
        "sage_product": "x3",
        "object_path": "BPCUSTOMER",
    },
    "sage-x3-supplier": {
        "sage_product": "x3",
        "object_path": "BPSUPPLIER",
    },
}

_DRY_RUN_RECORD_LIMIT = 5


# ---------------------------------------------------------------------------
# Step 1 + 2: Credential fetch + OAuth token validation
# ---------------------------------------------------------------------------

def test_connection(entity_id: str) -> tuple[bool, object]:
    """
    Fetch credentials from Secrets Manager and obtain an Intacct OAuth token.

    Returns (success: bool, auth_client).
    """
    cfg = _ENTITY_CONFIG[entity_id]
    sage_product = cfg["sage_product"]

    print(f"\n[1/4] Fetching credentials from Secrets Manager ...")
    print(f"      Secret path: {_ENVIRONMENT}/sources/sage/{sage_product}/credentials")

    from connector_runtime.adapters.sage.products.intacct.intacct_auth import IntacctAuthClient
    from connector_runtime.adapters.sage.common.sage_credential_manager import SageCredentialManager

    required_keys = frozenset({"base_url", "token_url", "client_id", "client_secret", "company_id"})
    credential_manager = SageCredentialManager(
        environment=_ENVIRONMENT,
        region_name=_REGION,
        product_name=sage_product,
        required_keys=required_keys,
    )
    http_client = SageHttpClient()
    auth_client = IntacctAuthClient(
        credential_manager=credential_manager,
        http_client=http_client,
    )

    print("[2/4] Obtaining Sage Intacct OAuth 2.0 access token ...")
    try:
        token = auth_client.get_access_token()
        base_url = auth_client.base_url
        # Show only a hint — never the full token value (OWASP A09)
        token_hint = f"{token[:4]}...{token[-4:]}" if len(token) >= 8 else "****"
        print(f"      base_url     : {base_url}")
        print(f"      token (hint) : {token_hint}")
        print("      OAuth OK\n")
        return True, auth_client
    except Exception as exc:
        print(f"      OAuth FAILED : {type(exc).__name__}: {exc}\n", file=sys.stderr)
        return False, None


# ---------------------------------------------------------------------------
# Step 3: Schema discovery via Intacct Models endpoint
# ---------------------------------------------------------------------------

def discover_schema(
    connector: SageConnector,
    entity_id: str,
    config: EntityExtractionConfig,
) -> FieldContract:
    """Discover fields from the Intacct Models endpoint and print a summary."""
    print("[3/4] Discovering schema via Intacct Models endpoint ...")
    field_contract = connector.discover_queryable_fields(
        source_id=_SOURCE_ID,
        entity_id=entity_id,
        field_mode=config.field_mode,
        include_fields=config.include_fields,
        exclude_fields=config.exclude_fields,
    )
    print(f"      Discovered {len(field_contract.fields)} queryable fields:")
    for fd in field_contract.fields[:20]:
        nullable = "NULL    " if fd.is_nullable else "NOT NULL"
        custom = " [custom]" if fd.is_custom else ""
        print(f"        {fd.name:<50} {fd.data_type:<20} {nullable}{custom}")
    if len(field_contract.fields) > 20:
        print(f"        ... and {len(field_contract.fields) - 20} more (omitted for brevity)")
    print(f"      Fingerprint: {field_contract.schema_fingerprint}\n")
    return field_contract


# ---------------------------------------------------------------------------
# Step 4: Extraction (dry-run peeks first N records, full run writes Parquet)
# ---------------------------------------------------------------------------

def run_extraction(
    connector: SageConnector,
    entity_id: str,
    field_contract: FieldContract,
    config: EntityExtractionConfig,
    watermark_lower: str | None,
    watermark_upper: str,
    dry_run: bool,
) -> None:
    """Build Intacct JSON DSL query and execute paginated extraction."""
    print("[4/4] Running extraction ...")

    query_contract = connector.build_extraction_query(
        field_contract=field_contract,
        load_type=config.load_type,
        watermark_field=config.watermark_field,
        watermark_lower=watermark_lower,
        watermark_upper=watermark_upper,
        extraction_window_days=config.extraction_window_days,
    )
    print(f"      load_type      : {config.load_type}")
    print(f"      watermark_field: {config.watermark_field or '(none)'}")
    print(f"      lower_bound    : {watermark_lower or '(epoch)'}")
    print(f"      upper_bound    : {watermark_upper}")

    if dry_run:
        print(f"\n      [DRY RUN] Fetching first {_DRY_RUN_RECORD_LIMIT} records (no S3 write)...")
        records_seen = 0
        for rec in connector.execute_extraction(query_contract, run_id="dry-run-local"):
            records_seen += 1
            if records_seen <= _DRY_RUN_RECORD_LIMIT:
                print(f"\n      --- Record {records_seen} ---")
                for key, val in list(rec.payload.items())[:10]:
                    print(f"        {key}: {val!r}")
                if len(rec.payload) > 10:
                    print(f"        ... ({len(rec.payload) - 10} more fields)")
            elif records_seen == _DRY_RUN_RECORD_LIMIT + 1:
                print("\n      (additional records exist — stopping dry-run preview)")
                break
        print(f"\n      Dry-run complete. Records seen: {records_seen}")
    else:
        print(
            "\n      NOTE: Local scripts cannot write to dev-edl-raw-layer.\n"
            "      The bucket policy restricts writes to dev-extraction-runtime-role only.\n"
            "      To run a full extraction, trigger via Step Functions:\n\n"
            f"        AWS_PROFILE=dev python scripts/trigger_extraction.py \\\n"
            f"          --source-id {_SOURCE_ID} --entity-id {entity_id} \\\n"
            f"          --environment {_ENVIRONMENT} --region {_REGION} \\\n"
            f"          --state-machine-arn arn:aws:states:{_REGION}:087972550871:stateMachine:{_ENVIRONMENT}-extraction-pipeline \\\n"
            f"          --param sage_product={_ENTITY_CONFIG[entity_id]['sage_product']} \\\n"
            f"          --param object_path={_ENTITY_CONFIG[entity_id]['object_path']}"
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Local Sage Intacct connector runner (connectivity + schema validation)."
    )
    parser.add_argument(
        "--entity-id",
        required=True,
        choices=list(_ENTITY_CONFIG.keys()),
        help="Entity ID to test (e.g. sage-intacct-customer).",
    )
    parser.add_argument(
        "--environment",
        default=_ENVIRONMENT,
        choices=["dev", "staging", "prod"],
        help="Deployment environment (default: dev).",
    )
    parser.add_argument(
        "--region",
        default=_REGION,
        help="AWS region (default: us-east-1).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="OAuth + schema discovery + peek first records only. No S3 write.",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=None,
        help="Override extraction_window_days from entity config (incremental only).",
    )
    args = parser.parse_args()

    entity_id: str = args.entity_id
    environment: str = args.environment
    region: str = args.region

    # ── Step 1 + 2: Connection test ──────────────────────────────────────────
    ok, _auth = test_connection(entity_id)
    if not ok:
        print("Aborting: cannot obtain access token.", file=sys.stderr)
        sys.exit(1)

    # ── Load entity config from DynamoDB ─────────────────────────────────────
    print("Loading entity config from DynamoDB ...")
    config_client = ConfigurationRepositoryClient(environment=environment, region_name=region)
    config: EntityExtractionConfig = config_client.load_entity_config(
        source_id=_SOURCE_ID,
        entity_id=entity_id,
    )
    if args.window_days is not None:
        # Patch the config to use the overridden window (useful for testing)
        import dataclasses
        config = dataclasses.replace(config, extraction_window_days=args.window_days)
    print(f"  load_type      : {config.load_type}")
    print(f"  watermark_field: {config.watermark_field or '(none)'}")
    print(f"  field_mode     : {config.field_mode}\n")

    # ── Load watermark (incremental lower bound) ─────────────────────────────
    watermark_lower: str | None = None
    if config.load_type == LoadType.INCREMENTAL:
        watermark_repo = WatermarkRepository(environment=environment, region_name=region)
        wm = watermark_repo.get_watermark(source_id=_SOURCE_ID, entity_id=entity_id)
        if wm and wm.last_successful_watermark:
            watermark_lower = wm.last_successful_watermark.isoformat()
            print(f"  watermark_lower (from DynamoDB): {watermark_lower}")
        else:
            print("  watermark_lower: (no prior watermark — first run will use epoch)")
    watermark_upper = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ── Build connector ───────────────────────────────────────────────────────
    cfg = _ENTITY_CONFIG[entity_id]
    connector = SageConnector(
        environment=environment,
        region_name=region,
        sage_product=cfg["sage_product"],
        object_path=cfg["object_path"],
    )

    # ── Step 3: Schema discovery ──────────────────────────────────────────────
    field_contract = discover_schema(connector, entity_id, config)

    # ── Step 4: Extraction ────────────────────────────────────────────────────
    run_extraction(
        connector=connector,
        entity_id=entity_id,
        field_contract=field_contract,
        config=config,
        watermark_lower=watermark_lower,
        watermark_upper=watermark_upper,
        dry_run=args.dry_run,
    )

    print("\nDone.")


if __name__ == "__main__":
    main()
