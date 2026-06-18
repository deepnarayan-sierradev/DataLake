#!/usr/bin/env python3
"""
Manually trigger a single extraction run via Step Functions.

Usage:
    python scripts/trigger_extraction.py \\
        --source-id salesforce \\
        --entity-id salesforce-account \\
        --environment dev \\
        --region us-east-1 \\
        --state-machine-arn \
            arn:aws:states:us-east-1:123456789012:stateMachine:dev-extraction-pipeline

If --state-machine-arn is omitted the script reads it from Terraform output:
    cd infrastructure/environments/dev && terraform output -raw state_machine_arn

Supported connector_params per source:
    salesforce:  --param object_name=Account
    netsuite:    --param record_type=customer
    mysql-rds:   --param table_name=orders
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime


def _get_state_machine_arn(environment: str, region: str) -> str:
    """Read state machine ARN from Terraform output."""
    try:
        result = subprocess.run(
            ["terraform", "output", "-raw", "state_machine_arn"],
            capture_output=True,
            text=True,
            check=True,
            cwd=f"infrastructure/environments/{environment}",
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as exc:
        print(f"Error reading Terraform output: {exc.stderr}")
        print("Pass --state-machine-arn explicitly instead.")
        sys.exit(1)


def trigger(
    source_id: str,
    entity_id: str,
    environment: str,
    region: str,
    state_machine_arn: str,
    connector_params: dict[str, str],
    is_replay: bool = False,
    replay_of_run_id: str | None = None,
) -> None:
    import boto3

    sfn = boto3.client("stepfunctions", region_name=region)

    execution_input: dict[str, object] = {
        "source_id": source_id,
        "entity_id": entity_id,
        "environment": environment,
        "connector_params": connector_params,
        "is_replay": is_replay,
    }
    if is_replay and replay_of_run_id:
        execution_input["replay_of_run_id"] = replay_of_run_id

    # Generate a deterministic execution name to prevent accidental duplicates.
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    prefix = "replay" if is_replay else "manual"
    execution_name = f"{prefix}-{source_id}-{entity_id}-{ts}"[:80]

    print(f"Starting execution: {execution_name}")
    print(f"State machine:      {state_machine_arn}")
    print(f"Input:              {json.dumps(execution_input, indent=2)}")
    print()

    response = sfn.start_execution(
        stateMachineArn=state_machine_arn,
        name=execution_name,
        input=json.dumps(execution_input, separators=(",", ":")),
    )

    execution_arn = response["executionArn"]
    print(f"Execution started: {execution_arn}")
    print()
    print("Monitor in AWS Console:")
    console_url = (
        f"https://{region}.console.aws.amazon.com/states/home"
        f"?region={region}#/executions/details/{execution_arn}"
    )
    print(f"  {console_url}")
    print()
    print("Or poll with:")
    cmd = f"aws stepfunctions describe-execution --execution-arn {execution_arn} --region {region}"
    print(f"  {cmd}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Trigger an extraction pipeline run.")
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--entity-id", required=True)
    parser.add_argument("--environment", required=True, choices=["dev", "staging", "prod"])
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--state-machine-arn", default=None)
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Connector parameter (e.g. --param object_name=Account). Repeat for multiple.",
    )
    parser.add_argument("--is-replay", action="store_true")
    parser.add_argument("--replay-of-run-id", default=None)

    args = parser.parse_args()

    if args.is_replay and not args.replay_of_run_id:
        print("Error: --replay-of-run-id is required when --is-replay is set.")
        sys.exit(1)

    state_machine_arn = args.state_machine_arn or _get_state_machine_arn(
        args.environment, args.region
    )

    connector_params: dict[str, str] = {}
    for kv in args.param:
        if "=" not in kv:
            print(f"Error: --param must be KEY=VALUE format, got: {kv!r}")
            sys.exit(1)
        k, v = kv.split("=", 1)
        connector_params[k] = v

    trigger(
        source_id=args.source_id,
        entity_id=args.entity_id,
        environment=args.environment,
        region=args.region,
        state_machine_arn=state_machine_arn,
        connector_params=connector_params,
        is_replay=args.is_replay,
        replay_of_run_id=args.replay_of_run_id,
    )


if __name__ == "__main__":
    main()
