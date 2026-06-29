"""
Sync EventBridge Scheduler schedules from the DynamoDB entity config table.

Reads every active entity from {environment}-entity-extraction-config that has
schedule_cron set and schedule_enabled=True, then creates or updates the
corresponding EventBridge schedule.  Entities with schedule_cron=None or
schedule_enabled=False have their schedule deleted if one exists.

Usage:
  python scripts/seed_schedules.py [--environment dev|staging|prod] [--dry-run]

TO ADD A NEW ENTITY with a schedule:
  1. Add a record to seed_entity_config.py with schedule_cron and
     connector_params set, then run:
       python scripts/seed_entity_config.py --environment dev
  2. Re-run this script:
       python scripts/seed_schedules.py --environment dev

No code changes are needed here.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Attr

from orchestration.event_bridge.extraction_schedule_client import (
    ExtractionScheduleClient,
    ScheduleNotFoundError,
)
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# ---------------------------------------------------------------------------
# Per-environment wiring — schedule group name, state machine ARN, IAM role.
# These are infrastructure values set once at deploy time; they never change
# for a given environment.  Add staging/prod entries after those environments
# are deployed.
# ---------------------------------------------------------------------------

_ENV_CONFIG: dict[str, dict[str, str]] = {
    "dev": {
        "schedule_group_name":  "dev-extraction-schedules",
        "state_machine_arn":    "arn:aws:states:us-east-1:087972550871:stateMachine:dev-extraction-pipeline",
        "execution_role_arn":   "arn:aws:iam::087972550871:role/dev-extraction-schedule-trigger-role",
        "region":               "us-east-1",
    },
    # Populate after staging/prod are deployed:
    # "staging": {
    #     "schedule_group_name":  "staging-extraction-schedules",
    #     "state_machine_arn":    "arn:aws:states:us-east-1:087972550871:stateMachine:staging-extraction-pipeline",
    #     "execution_role_arn":   "arn:aws:iam::087972550871:role/staging-extraction-schedule-trigger-role",
    #     "region":               "us-east-1",
    # },
    # "prod": {
    #     "schedule_group_name":  "prod-extraction-schedules",
    #     "state_machine_arn":    "arn:aws:states:us-east-1:087972550871:stateMachine:prod-extraction-pipeline",
    #     "execution_role_arn":   "arn:aws:iam::087972550871:role/prod-extraction-schedule-trigger-role",
    #     "region":               "us-east-1",
    # },
}


def _load_schedulable_entities(table_name: str, region: str) -> list[dict]:
    """
    Scan DynamoDB for all active entities that have schedule_cron set.
    Returns a list of raw DynamoDB item dicts.
    """
    dynamodb = boto3.resource("dynamodb", region_name=region)
    table = dynamodb.Table(table_name)

    items: list[dict] = []
    scan_kwargs: dict = {
        "FilterExpression": Attr("active").eq(True) & Attr("schedule_cron").exists(),
    }
    while True:
        response = table.scan(**scan_kwargs)
        for item in response.get("Items", []):
            # DynamoDB returns Decimal for numbers — normalise to plain Python types.
            items.append({
                k: int(v) if isinstance(v, Decimal) else v
                for k, v in item.items()
            })
        last = response.get("LastEvaluatedKey")
        if not last:
            break
        scan_kwargs["ExclusiveStartKey"] = last

    return items


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Sync EventBridge schedules from DynamoDB entity config."
    )
    p.add_argument(
        "--environment", default="dev",
        choices=list(_ENV_CONFIG.keys()),
        help="Target environment (default: dev).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be created/updated/deleted without making AWS calls.",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    env = args.environment
    dry_run = args.dry_run

    if env not in _ENV_CONFIG:
        print(
            f"ERROR: environment '{env}' not configured in _ENV_CONFIG. "
            "Add the state machine ARN and schedule role ARN after deploying that environment.",
            file=sys.stderr,
        )
        sys.exit(1)

    cfg = _ENV_CONFIG[env]
    table_name = f"{env}-entity-extraction-config"

    entities = _load_schedulable_entities(table_name, cfg["region"])

    # Split into: should have a schedule vs. should not.
    to_upsert = [e for e in entities if e.get("schedule_enabled", True) and e.get("schedule_cron")]
    to_disable = [e for e in entities if not e.get("schedule_enabled", True) and e.get("schedule_cron")]

    print(f"Loaded {len(entities)} entity config(s) from {table_name}.")
    print(f"  {len(to_upsert)} to upsert, {len(to_disable)} to delete/disable.\n")

    if dry_run:
        print("[DRY RUN] Would upsert:")
        for e in to_upsert:
            print(f"  {e['source_id']}--{e['entity_id']}  {e['schedule_cron']}  "
                  f"connector_params={e.get('connector_params', {})}")
        if to_disable:
            print("[DRY RUN] Would delete schedule for:")
            for e in to_disable:
                print(f"  {e['source_id']}--{e['entity_id']}")
        return

    client = ExtractionScheduleClient(
        schedule_group_name=cfg["schedule_group_name"],
        target_arn=cfg["state_machine_arn"],
        execution_role_arn=cfg["execution_role_arn"],
        region_name=cfg["region"],
    )

    ok = 0
    failed: list[str] = []

    for e in to_upsert:
        try:
            name = client.create_or_update_schedule(
                source_id=e["source_id"],
                entity_id=e["entity_id"],
                cron_expression=e["schedule_cron"],
                connector_params=e.get("connector_params", {}),
                timezone=e.get("schedule_timezone", "UTC"),
            )
            print(f"  OK    {name}  →  {e['schedule_cron']}")
            ok += 1
        except Exception as exc:
            print(f"  FAIL  {e['source_id']}--{e['entity_id']}: {exc}", file=sys.stderr)
            failed.append(e["entity_id"])

    for e in to_disable:
        try:
            client.delete_schedule(
                source_id=e["source_id"],
                entity_id=e["entity_id"],
            )
            print(f"  DEL   {e['source_id']}--{e['entity_id']}")
            ok += 1
        except ScheduleNotFoundError:
            pass  # already gone
        except Exception as exc:
            print(f"  FAIL  delete {e['source_id']}--{e['entity_id']}: {exc}", file=sys.stderr)
            failed.append(e["entity_id"])

    print(f"\nDone: {ok} synced, {len(failed)} failed.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
