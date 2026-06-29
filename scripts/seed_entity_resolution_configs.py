#!/usr/bin/env python3
"""
Publish entity resolution configs from config/entity_resolution/ to S3.

Reads all match_rules_{version}.json and survivorship_{version}.json files
for each entity type and uploads them to the S3 curated-layer bucket used by
ResolutionConfigRegistry.  Also updates the latest.json pointer per entity type.

Usage:
    python scripts/seed_entity_resolution_configs.py --environment dev --region us-east-1
    python scripts/seed_entity_resolution_configs.py --environment dev --region us-east-1 --dry-run

    # Seed a single entity type only:
    python scripts/seed_entity_resolution_configs.py --environment dev --entity-type contract

Prerequisite:
    AWS credentials configured (AWS_PROFILE, AWS_DEFAULT_REGION, or instance role).
    The S3 bucket must already exist (provisioned by the Terraform storage module).

S3 layout written by this script:
    s3://{bucket}/entity-resolution/{entity_type}/match_rules_{version}.json
    s3://{bucket}/entity-resolution/{entity_type}/survivorship_{version}.json
    s3://{bucket}/entity-resolution/{entity_type}/latest.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import boto3
import botocore.exceptions

# Root of the repo — two levels up from scripts/
_REPO_ROOT = Path(__file__).resolve().parent.parent
_CONFIGS_DIR = _REPO_ROOT / "config" / "entity_resolution"


def _bucket_name(environment: str) -> str:
    return f"{environment}-edl-curated-layer"


def _collect_config_files(
    entity_type_filter: str | None,
) -> dict[str, dict[str, list[tuple[str, str, Path]]]]:
    """
    Walk config/entity_resolution/ and return a nested dict:
      { entity_type: { "match_rules": [(version, kind, path), ...],
                       "survivorship": [(version, kind, path), ...] } }
    """
    result: dict[str, dict[str, list[tuple[str, str, Path]]]] = {}

    for entity_dir in sorted(_CONFIGS_DIR.iterdir()):
        if not entity_dir.is_dir():
            continue
        entity_type = entity_dir.name
        if entity_type_filter and entity_type != entity_type_filter:
            continue

        match_rules: list[tuple[str, str, Path]] = []
        survivorship: list[tuple[str, str, Path]] = []

        for f in sorted(entity_dir.glob("*.json")):
            stem = f.stem
            if stem == "latest":
                continue  # we regenerate latest.json from discovered versions
            if stem.startswith("match_rules_"):
                version = stem[len("match_rules_"):]
                match_rules.append((version, "match_rules", f))
            elif stem.startswith("survivorship_"):
                version = stem[len("survivorship_"):]
                survivorship.append((version, "survivorship", f))

        if match_rules or survivorship:
            result[entity_type] = {
                "match_rules": match_rules,
                "survivorship": survivorship,
            }

    return result


def _resolve_latest_version(versions: list[str]) -> str:
    """Return the highest version from a list like ['v1', 'v2', 'v10']."""
    def _key(v: str) -> int:
        try:
            return int(v.lstrip("v"))
        except ValueError:
            return 0

    return max(versions, key=_key, default="v1")


def _upload(
    s3: Any,
    bucket: str,
    key: str,
    body: bytes,
    content_type: str,
    dry_run: bool,
) -> None:
    if dry_run:
        print(f"  [DRY-RUN] would upload s3://{bucket}/{key} ({len(body):,} bytes)")
        return
    s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type)
    print(f"  uploaded  s3://{bucket}/{key} ({len(body):,} bytes)")


def seed(
    environment: str,
    region: str,
    entity_type_filter: str | None = None,
    dry_run: bool = False,
) -> None:
    bucket = _bucket_name(environment)
    s3 = boto3.client("s3", region_name=region)

    # Verify bucket is reachable before uploading (fail-fast).
    if not dry_run:
        try:
            s3.head_bucket(Bucket=bucket)
        except botocore.exceptions.ClientError as exc:
            code = exc.response["Error"]["Code"]
            print(
                f"ERROR: Cannot access bucket s3://{bucket} (code={code}). "
                "Ensure the bucket exists and your credentials have s3:HeadBucket permission.",
                file=sys.stderr,
            )
            sys.exit(1)

    configs = _collect_config_files(entity_type_filter)
    if not configs:
        print(
            f"No entity resolution config files found under {_CONFIGS_DIR}"
            + (f" for entity_type={entity_type_filter!r}" if entity_type_filter else ""),
            file=sys.stderr,
        )
        sys.exit(1)

    total_uploads = 0

    for entity_type, groups in sorted(configs.items()):
        print(f"\nEntity type: {entity_type}")

        mr_versions: list[str] = []
        sv_versions: list[str] = []

        # Upload match_rules files
        for version, kind, path in groups["match_rules"]:
            body = path.read_bytes()
            # Validate JSON before uploading
            try:
                json.loads(body)
            except json.JSONDecodeError as exc:
                print(f"  ERROR: {path} is not valid JSON: {exc}", file=sys.stderr)
                sys.exit(1)
            key = f"entity-resolution/{entity_type}/match_rules_{version}.json"
            _upload(s3, bucket, key, body, "application/json", dry_run)
            mr_versions.append(version)
            total_uploads += 1

        # Upload survivorship files
        for version, kind, path in groups["survivorship"]:
            body = path.read_bytes()
            try:
                json.loads(body)
            except json.JSONDecodeError as exc:
                print(f"  ERROR: {path} is not valid JSON: {exc}", file=sys.stderr)
                sys.exit(1)
            key = f"entity-resolution/{entity_type}/survivorship_{version}.json"
            _upload(s3, bucket, key, body, "application/json", dry_run)
            sv_versions.append(version)
            total_uploads += 1

        # Write (or update) the latest.json pointer
        latest_mr = _resolve_latest_version(mr_versions) if mr_versions else "v1"
        latest_sv = _resolve_latest_version(sv_versions) if sv_versions else "v1"
        latest_doc = {
            "match_rules_version": latest_mr,
            "survivorship_version": latest_sv,
        }
        latest_key = f"entity-resolution/{entity_type}/latest.json"
        _upload(
            s3,
            bucket,
            latest_key,
            json.dumps(latest_doc, indent=2).encode(),
            "application/json",
            dry_run,
        )
        total_uploads += 1
        print(
            f"  latest pointer → match_rules={latest_mr}, survivorship={latest_sv}"
        )

    print(f"\nDone. {total_uploads} object(s) {'would be ' if dry_run else ''}uploaded to s3://{bucket}/entity-resolution/")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed entity resolution configs from config/entity_resolution/ to S3."
    )
    parser.add_argument("--environment", required=True, choices=["dev", "staging", "prod"])
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument(
        "--entity-type",
        default=None,
        help="Seed only this entity type (e.g. 'contract'). Omit to seed all.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be uploaded without writing to S3.",
    )
    args = parser.parse_args()

    seed(
        environment=args.environment,
        region=args.region,
        entity_type_filter=args.entity_type,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
