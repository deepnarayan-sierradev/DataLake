#!/usr/bin/env python3
"""
Seed serving store load configuration records into DynamoDB.

Onboards one or more (tenant_code, entity_type) pairs to the serving store, so
the LoadServingStore pipeline stage actually loads them into the relational
serving database instead of skipping them. Without a config record, Stage 16
silently skips the entity — which is why a freshly deployed serving store RDS
instance stays empty until this runs.

Usage:
    # Seed the default entity types for the demo tenant into the dev MySQL RDS
    python scripts/seed_serving_store_config.py --environment dev --region us-east-1

    # Seed a single entity type
    python scripts/seed_serving_store_config.py --environment dev --entity-type company

    # Preview without writing
    python scripts/seed_serving_store_config.py --environment dev --dry-run

The writer credential ARN defaults to the AWS-managed master secret of the
deployed serving store RDS instance (edl-serving-store-{engine}-{environment});
pass --writer-secret-arn to override (e.g. for a BYO database).

Records are written via ServingStoreConfigRepositoryClient.save_config, so every
record is Pydantic-validated against ServingStoreLoadConfig before it lands. Safe
to re-run — overwrite is on by default.
"""

from __future__ import annotations

import argparse
import sys

import boto3

from contracts.serving_store_config_contract import ServingStoreEngine, ServingStoreLoadConfig
from serving_store.serving_store_config_repository import ServingStoreConfigRepositoryClient

# Entity types with analytics-layer output that are live in dev today (see
# docs/PLATFORM_STATUS.md). Others (person's supplier/ar_invoice/… variants) are
# config-complete but not yet producing analytics data, so they are not seeded by
# default — pass --entity-type to onboard one explicitly once it is producing data.
_DEFAULT_ENTITY_TYPES: tuple[str, ...] = ("company", "person", "contract")

# golden_id is kept in the analytics layer as the stable cross-entity join key
# (analytics_publisher_handler.py) — the natural upsert/primary key here.
_PRIMARY_KEYS: tuple[str, ...] = ("golden_id",)

# Registry engine_id → RDS instance engine segment in the Terraform identifier.
_ENGINE_TO_RDS_SEGMENT: dict[ServingStoreEngine, str] = {
    ServingStoreEngine.MYSQL_RDS: "mysql",
    ServingStoreEngine.POSTGRESQL: "postgres",
    ServingStoreEngine.SQLSERVER: "sqlserver-se",
}


def _resolve_instance(
    engine: ServingStoreEngine, environment: str, region: str
) -> dict[str, object]:
    """Read the master secret ARN and endpoint host/port off the deployed RDS instance.

    The AWS-managed master secret carries only username/password (no host/port), so the
    endpoint must be threaded through the config record — the loader injects it at connect
    time. BYO engines (azure_sql, redshift) have no such instance; pass values explicitly.
    """
    segment = _ENGINE_TO_RDS_SEGMENT.get(engine)
    if segment is None:
        raise ValueError(
            f"Cannot auto-resolve an instance for engine {engine.value!r}; "
            "pass --writer-secret-arn and --db-host explicitly."
        )
    identifier = f"edl-serving-store-{segment}-{environment}"
    rds = boto3.client("rds", region_name=region)
    instance = rds.describe_db_instances(DBInstanceIdentifier=identifier)["DBInstances"][0]
    secret = instance.get("MasterUserSecret") or {}
    if not secret.get("SecretArn"):
        raise ValueError(
            f"RDS instance {identifier!r} has no managed master secret; "
            "pass --writer-secret-arn explicitly."
        )
    endpoint = instance.get("Endpoint") or {}
    return {
        "secret_arn": str(secret["SecretArn"]),
        "db_host": endpoint.get("Address"),
        "db_port": endpoint.get("Port"),
    }


def _table_name_for(entity_type: str) -> str:
    """Analytics entity type → SQL table name (hyphens are not valid SQL identifiers)."""
    return entity_type.replace("-", "_")


def _build_records(
    entity_types: tuple[str, ...],
    tenant_code: str,
    engine: ServingStoreEngine,
    writer_secret_arn: str,
    region: str,
    db_host: str | None,
    db_port: int | None,
) -> list[ServingStoreLoadConfig]:
    return [
        ServingStoreLoadConfig(
            tenant_code=tenant_code,
            entity_type=entity_type,
            target_engine=engine,
            table_name=_table_name_for(entity_type),
            primary_keys=_PRIMARY_KEYS,
            secret_arn=writer_secret_arn,
            region_name=region,
            connection_database=None,
            db_host=db_host,
            db_port=db_port,
            enabled=True,
        )
        for entity_type in entity_types
    ]


def seed(
    environment: str,
    region: str,
    tenant_code: str,
    engine: ServingStoreEngine,
    entity_types: tuple[str, ...],
    writer_secret_arn: str | None,
    db_host: str | None,
    db_port: int | None,
    dry_run: bool = False,
) -> None:
    if writer_secret_arn is None or db_host is None:
        resolved = _resolve_instance(engine, environment, region)
        writer_secret_arn = writer_secret_arn or str(resolved["secret_arn"])
        db_host = db_host or (str(resolved["db_host"]) if resolved["db_host"] else None)
        if db_port is None and resolved["db_port"] is not None:
            db_port = int(str(resolved["db_port"]))

    records = _build_records(
        entity_types, tenant_code, engine, writer_secret_arn, region, db_host, db_port
    )
    print(
        f"Target table: EdlServingStoreConfig  (region: {region}, tenant_code: {tenant_code}, "
        f"engine: {engine.value})\nWriter secret: {writer_secret_arn}\n"
        f"DB endpoint: {db_host}:{db_port}"
    )

    if dry_run:
        print("\n[DRY RUN] Would write the following records:")
        for rec in records:
            print(
                f"  {rec.tenant_code} / {rec.entity_type} → table {rec.table_name} "
                f"(pk={list(rec.primary_keys)}, host={rec.db_host}, enabled={rec.enabled})"
            )
        return

    repo = ServingStoreConfigRepositoryClient(environment=environment, region_name=region)
    for rec in records:
        repo.save_config(rec, overwrite=True)
        print(f"  Written: {rec.tenant_code} / {rec.entity_type} → {rec.table_name}")

    print(f"\n{len(records)} record(s) seeded successfully.")
    print("\nNext: the next pipeline run for these entity types will create the tenant")
    print("database/schema, tables, and the per-tenant read-only reader credential")
    print(f"(edl/serving-store/{tenant_code}/{engine.value}/reader-credentials).")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed serving store load config records.")
    parser.add_argument("--environment", required=True, choices=["dev", "staging", "prod"])
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--tenant-code", default="demo", help="Tenant code slug (default: demo).")
    parser.add_argument(
        "--engine",
        default=ServingStoreEngine.MYSQL_RDS.value,
        choices=[e.value for e in ServingStoreEngine],
        help="Target serving store engine (default: mysql_rds — the deployed dev engine).",
    )
    parser.add_argument(
        "--entity-type",
        action="append",
        dest="entity_types",
        help="Entity type to seed; repeatable. Defaults to the live-in-dev set.",
    )
    parser.add_argument(
        "--writer-secret-arn",
        default=None,
        help="Writer credential secret ARN. Defaults to the deployed instance's master secret.",
    )
    parser.add_argument(
        "--db-host",
        default=None,
        help="Serving DB endpoint host. Defaults to the deployed instance's endpoint address.",
    )
    parser.add_argument(
        "--db-port",
        type=int,
        default=None,
        help="Serving DB endpoint port. Defaults to the deployed instance's endpoint port.",
    )
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

    entity_types = tuple(args.entity_types) if args.entity_types else _DEFAULT_ENTITY_TYPES
    seed(
        environment=args.environment,
        region=args.region,
        tenant_code=args.tenant_code,
        engine=ServingStoreEngine(args.engine),
        entity_types=entity_types,
        writer_secret_arn=args.writer_secret_arn,
        db_host=args.db_host,
        db_port=args.db_port,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
