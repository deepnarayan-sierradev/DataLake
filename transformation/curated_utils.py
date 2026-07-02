"""
Shared curated-layer utilities for the Enterprise Data Lake platform.

Provides S3 prefix discovery and Parquet record loading for the curated layer.
Used by:
  - transformation.curated_accumulator  — loads previous state for SCD Type 1 merge
  - entity_resolution pipeline handler  — loads latest curated partitions for
                                          multi-source entity resolution
  - transformation.transformation_pipeline — S3 prefix validation

Extracting these helpers into a shared module eliminates duplication and ensures
that curated layer access semantics are consistent across all stages.

Curated path structure (spec §14):
  curated/{domain}/{entity_id}/curated_date={YYYY-MM-DD}/run_id={run_id}/data.parquet

Security (OWASP A03 / CWE-22):
  - All S3 prefix inputs are validated against a safe pattern before use to
    prevent path traversal.
  - Functions accept only server-side-derived inputs (domain, entity_id) —
    never raw user or event input.
"""

from __future__ import annotations

import io
import re
from typing import Any, Final

import pyarrow.parquet as pq

from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

# S3 prefix safety: no path traversal sequences, no leading slash (OWASP A03).
# Hive-style partition paths (curated_date=2026-07-02, run_id=...) require '=' and '-'.
# Single definition — imported wherever prefix validation is needed (no duplication).
SAFE_S3_PREFIX_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9\-_/=]{0,511}$"
)


def source_id_to_domain(source_id: str) -> str:
    """
    Convert a stable source_id to an S3/Glue-safe domain string.

    Converts hyphens to underscores so the result satisfies Glue table naming
    constraints (^[a-z][a-z0-9_]{0,63}$).

    Single definition — imported by both the transformation handler and the
    entity resolution handler.

    Examples:
        "mysql-rds"   → "mysql_rds"
        "salesforce"  → "salesforce"
        "netsuite"    → "netsuite"
    """
    return source_id.replace("-", "_")


def find_latest_curated_prefix(
    s3: Any,
    bucket: str,
    domain: str,
    entity_id: str,
) -> str | None:
    """
    Scan the curated bucket for the most recent run prefix for (domain, entity_id).

    Curated path structure:
      curated/{domain}/{entity_id}/curated_date={YYYY-MM-DD}/run_id={run_id}/

    Traversal order:
      1. List all curated_date= partitions — ISO date strings sort lexicographically.
      2. Within the latest date partition, list all run_id= sub-prefixes.
      3. Return the lexicographically latest run_id prefix (most recent run).

    Returns:
        Full S3 prefix string with trailing slash for the latest run, or None if
        no curated data exists yet (first run for this entity).

    Security: domain and entity_id are server-side derived values — never accepted
    from user input (OWASP A03).
    """
    base_prefix = f"curated/{domain}/{entity_id}/"
    paginator = s3.get_paginator("list_objects_v2")

    # Collect curated_date= partition prefixes using the delimiter trick
    # (only returns "directory" entries, not individual files).
    date_prefixes: list[str] = []
    for page in paginator.paginate(Bucket=bucket, Prefix=base_prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            pfx: str = cp["Prefix"]
            if "curated_date=" in pfx:
                date_prefixes.append(pfx)

    if not date_prefixes:
        _logger.info(
            "curated_no_previous_partitions",
            domain=domain,
            entity_id=entity_id,
        )
        return None

    # ISO date format sorts lexicographically — latest is always the last entry.
    latest_date_prefix = sorted(date_prefixes)[-1]

    # Within the date partition, collect run_id= sub-prefixes.
    run_prefixes: list[str] = []
    for page in paginator.paginate(
        Bucket=bucket, Prefix=latest_date_prefix, Delimiter="/"
    ):
        for cp in page.get("CommonPrefixes", []):
            pfx = cp["Prefix"]
            if "run_id=" in pfx:
                run_prefixes.append(pfx)

    if not run_prefixes:
        return None

    return sorted(run_prefixes)[-1]


def load_curated_records(
    s3: Any,
    bucket: str,
    prefix: str,
) -> list[dict[str, Any]]:
    """
    Load all Parquet files from a curated S3 prefix into a flat list of dicts.

    Reads files sequentially — only one Parquet file is held in memory at a
    time to bound peak memory consumption (performance / F-05).

    Args:
        s3:     Boto3 S3 client.
        bucket: Curated layer S3 bucket name.
        prefix: S3 key prefix to list and read (with or without trailing slash).

    Returns:
        Flat list of record dicts from all Parquet files under the prefix.

    Raises:
        ValueError: If the prefix contains path traversal characters or
                    disallowed characters (OWASP A03 / CWE-22).
    """
    clean_prefix = prefix.strip()
    if ".." in clean_prefix or clean_prefix.startswith("/"):
        raise ValueError(f"Unsafe curated prefix rejected: {clean_prefix!r}")
    if not SAFE_S3_PREFIX_PATTERN.match(clean_prefix.rstrip("/")):
        raise ValueError(
            f"Curated prefix {clean_prefix!r} contains disallowed characters."
        )

    paginator = s3.get_paginator("list_objects_v2")
    records: list[dict[str, Any]] = []

    for page in paginator.paginate(
        Bucket=bucket, Prefix=clean_prefix.rstrip("/") + "/"
    ):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(".parquet"):
                continue
            raw = s3.get_object(Bucket=bucket, Key=obj["Key"])
            buf = io.BytesIO(raw["Body"].read())
            table = pq.read_table(buf)  # type: ignore[no-untyped-call]
            records.extend(table.to_pylist())
            del table  # release Arrow memory before reading next file

    return records
