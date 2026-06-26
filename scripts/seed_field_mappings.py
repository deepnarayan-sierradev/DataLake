#!/usr/bin/env python3
"""
Publish field mapping rule sets from config/field_mappings/ to S3.

Reads all versioned JSON files under config/field_mappings/{source_id}/{entity_id}/{version}.json
and uploads them to the S3 curated-layer bucket used by FieldMappingRegistryClient.
Also updates the latest.json pointer for each source/entity pair to the highest version found.

Usage:
    python scripts/seed_field_mappings.py --environment dev --region us-east-1
    python scripts/seed_field_mappings.py --environment dev --region us-east-1 --dry-run

    # Publish a single source/entity only:
    python scripts/seed_field_mappings.py --environment dev --source-id salesforce --entity-id salesforce-account

Prerequisite:
    AWS credentials configured (AWS_PROFILE, AWS_DEFAULT_REGION, or instance role).
    The S3 bucket must already exist (provisioned by the Terraform storage module).

S3 layout written by this script:
    s3://{bucket}/field-mappings/{source_id}/{entity_id}/{version}.json
    s3://{bucket}/field-mappings/{source_id}/{entity_id}/latest.json  ← pointer to highest version
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import boto3

# Root of the repo — two levels up from scripts/
_REPO_ROOT = Path(__file__).resolve().parent.parent
_MAPPINGS_DIR = _REPO_ROOT / "config" / "field_mappings"


def _bucket_name(environment: str) -> str:
    return f"{environment}-edl-curated-layer"


def _collect_mapping_files(
    source_id: str | None,
    entity_id: str | None,
) -> list[tuple[str, str, str, Path]]:
    """
    Walk config/field_mappings/ and return (source_id, entity_id, version, path) tuples.
    Filters by source_id / entity_id when provided.
    """
    results: list[tuple[str, str, str, Path]] = []

    for source_dir in sorted(_MAPPINGS_DIR.iterdir()):
        if not source_dir.is_dir():
            continue
        if source_id and source_dir.name != source_id:
            continue

        for entity_dir in sorted(source_dir.iterdir()):
            if not entity_dir.is_dir():
                continue
            if entity_id and entity_dir.name != entity_id:
                continue

            for mapping_file in sorted(entity_dir.glob("*.json")):
                version = mapping_file.stem  # e.g. "v1"
                results.append((source_dir.name, entity_dir.name, version, mapping_file))

    return results


def _resolve_latest_version(versions: list[str]) -> str:
    """
    Return the highest version string from a list like ["v1", "v2", "v10"].
    Versions must follow the pattern v{integer}.
    Falls back to lexicographic sort if parsing fails.
    """
    def _version_key(v: str) -> int:
        try:
            return int(v.lstrip("v"))
        except ValueError:
            return 0

    return max(versions, key=_version_key)


def seed(
    environment: str,
    region: str,
    source_id: str | None = None,
    entity_id: str | None = None,
    dry_run: bool = False,
) -> None:
    bucket = _bucket_name(environment)
    mapping_files = _collect_mapping_files(source_id, entity_id)

    if not mapping_files:
        print("No mapping files found matching the specified filters.")
        sys.exit(1)

    print(f"Target bucket : {bucket}  (region: {region})")
    print(f"Mappings dir  : {_MAPPINGS_DIR}")
    print(f"Files found   : {len(mapping_files)}\n")

    if dry_run:
        print("[DRY RUN] Would publish the following rule sets:")
        # Group by (source_id, entity_id) to show latest pointer
        groups: dict[tuple[str, str], list[str]] = {}
        for src, ent, ver, path in mapping_files:
            groups.setdefault((src, ent), []).append(ver)
            print(f"  s3://{bucket}/field-mappings/{src}/{ent}/{ver}.json  ← {path.relative_to(_REPO_ROOT)}")
        print()
        for (src, ent), versions in groups.items():
            latest = _resolve_latest_version(versions)
            print(f"  s3://{bucket}/field-mappings/{src}/{ent}/latest.json  → {latest}")
        return

    s3 = boto3.client("s3", region_name=region)

    # Track published versions per (source_id, entity_id) to update pointer once
    published_versions: dict[tuple[str, str], list[str]] = {}

    for src, ent, ver, path in mapping_files:
        # Validate the JSON is well-formed before uploading
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))

        # Basic contract check — ensure the file matches the directory it lives in
        if raw.get("source_id") != src:
            print(f"  ERROR: source_id in {path} is {raw.get('source_id')!r}, expected {src!r}")
            sys.exit(1)
        if raw.get("entity_id") != ent:
            print(f"  ERROR: entity_id in {path} is {raw.get('entity_id')!r}, expected {ent!r}")
            sys.exit(1)
        if raw.get("mapping_version") != ver:
            print(
                f"  ERROR: mapping_version in {path} is {raw.get('mapping_version')!r}, "
                f"expected {ver!r}"
            )
            sys.exit(1)

        key = f"field-mappings/{src}/{ent}/{ver}.json"
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(raw, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        print(f"  Published : s3://{bucket}/{key}")
        published_versions.setdefault((src, ent), []).append(ver)

    # Update latest.json pointer for each source/entity to the highest version
    for (src, ent), versions in published_versions.items():
        latest = _resolve_latest_version(versions)
        pointer_key = f"field-mappings/{src}/{ent}/latest.json"
        s3.put_object(
            Bucket=bucket,
            Key=pointer_key,
            Body=json.dumps({"mapping_version": latest}).encode("utf-8"),
            ContentType="application/json",
        )
        print(f"  Pointer   : s3://{bucket}/{pointer_key}  → {latest}")

    total = len(mapping_files)
    print(f"\n{total} rule set(s) published successfully.")
    print("\nNext step: run the transformation pipeline or test with:")
    print(
        "  python -c \""
        "from transformation.field_mapping.field_mapping_registry import FieldMappingRegistryClient; "
        f"c = FieldMappingRegistryClient('{bucket}', '{region}'); "
        "rs = c.load_rule_set('salesforce', 'salesforce-account'); "
        "print(rs)\""
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Publish field mapping rule sets from config/field_mappings/ to S3."
    )
    parser.add_argument("--environment", required=True, choices=["dev", "staging", "prod"])
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument(
        "--source-id",
        default=None,
        help="Publish only mappings for this source (e.g. salesforce). Omit to publish all.",
    )
    parser.add_argument(
        "--entity-id",
        default=None,
        help="Publish only mappings for this entity (e.g. salesforce-account). Omit to publish all.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be published without making any API calls.",
    )
    args = parser.parse_args()

    if args.environment == "prod":
        confirm = input("You are publishing to PRODUCTION. Type 'yes' to confirm: ")
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            sys.exit(0)

    seed(
        environment=args.environment,
        region=args.region,
        source_id=args.source_id,
        entity_id=args.entity_id,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
