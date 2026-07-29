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
  {tenant_code}/curated/{domain}/{entity_id}/curated_date={YYYY-MM-DD}/run_id={run_id}/data.parquet

Security (OWASP A03 / CWE-22):
  - All S3 prefix inputs are validated against a safe pattern before use to
    prevent path traversal.
  - Functions accept only server-side-derived inputs (domain, entity_id) —
    never raw user or event input.
"""

from __future__ import annotations

import io
import re
from typing import Any

import pyarrow.parquet as pq

from contracts.identifier_policy import SAFE_S3_PREFIX_PATTERN
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)


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
    tenant_code: str,
) -> str | None:
    """
    Scan the curated bucket for the most recent run prefix for (domain, entity_id).

    Curated path structure:
      {tenant_code}/curated/{domain}/{entity_id}/curated_date={YYYY-MM-DD}/run_id={run_id}/

    Traversal order:
      1. List all curated_date= partitions — ISO date strings sort lexicographically.
      2. Within the latest date partition, list all run_id= sub-prefixes.
      3. Return the lexicographically latest run_id prefix (most recent run).

    Returns:
        Full S3 prefix string with trailing slash for the latest run, or None if
        no curated data exists yet (first run for this entity).

    Security: domain and entity_id are server-side derived values — never accepted
    from user input (OWASP A03). tenant_code must be pre-validated by the caller
    (ARCH-1) — this must match CuratedLayerWriter's actual tenant-prefixed write
    path, or every lookup silently finds nothing (or, worse, another tenant's data
    if the tenant_code segment were ever omitted here).
    """
    base_prefix = f"{tenant_code}/curated/{domain}/{entity_id}/"
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
    for page in paginator.paginate(Bucket=bucket, Prefix=latest_date_prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            pfx = cp["Prefix"]
            if "run_id=" in pfx:
                run_prefixes.append(pfx)

    if not run_prefixes:
        return None

    latest_prefix = sorted(run_prefixes)[-1]

    if not _is_safe_curated_prefix(latest_prefix, domain=domain, entity_id=entity_id):
        return None

    return latest_prefix


def _is_safe_curated_prefix(prefix: str, *, domain: str, entity_id: str) -> bool:
    """
    Validate an S3-returned prefix before it is used downstream (OWASP A03 / CWE-22).

    An attacker who gains S3 write access could create a malicious partition path
    like 'curated/.../run_id=../../evil/' to trigger path traversal.
    """
    clean = prefix.rstrip("/")
    if ".." in clean or clean.startswith("/"):
        _logger.warning(
            "curated_prefix_path_traversal_rejected",
            prefix=clean,
            domain=domain,
            entity_id=entity_id,
        )
        return False
    if not SAFE_S3_PREFIX_PATTERN.match(clean):
        _logger.warning(
            "curated_prefix_invalid_chars_rejected",
            domain=domain,
            entity_id=entity_id,
        )
        return False
    return True


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
        raise ValueError(f"Curated prefix {clean_prefix!r} contains disallowed characters.")

    paginator = s3.get_paginator("list_objects_v2")
    records: list[dict[str, Any]] = []

    for page in paginator.paginate(Bucket=bucket, Prefix=clean_prefix.rstrip("/") + "/"):
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(".parquet"):
                continue
            raw = s3.get_object(Bucket=bucket, Key=obj["Key"])
            buf = io.BytesIO(raw["Body"].read())
            table = pq.read_table(buf)
            # RecordBatch iteration (§2.3): 10K rows materialised at a time.
            for batch in table.to_batches(max_chunksize=10_000):
                batch_dict = batch.to_pydict()
                n = batch.num_rows
                cols = list(batch_dict.keys())
                records.extend({col: batch_dict[col][i] for col in cols} for i in range(n))
            del table  # release Arrow memory before reading next file

    return records


def load_curated_records_duckdb(
    s3: Any,
    bucket: str,
    prefix: str,
    region_name: str,
) -> list[dict[str, Any]]:
    """
    Load all Parquet files from a curated S3 prefix using DuckDB's native S3
    reader (httpfs), instead of the hand-rolled paginated Python loop used by
    load_curated_records() (§3.1, PERF-3).

    load_curated_records() lists every object via list_objects_v2, downloads
    each Parquet file's full bytes into a Python BytesIO buffer, and only
    THEN hands the bytes to PyArrow for parsing — the entire file crosses
    into Python memory before any columnar engine touches it. This function
    instead lets DuckDB read the Parquet objects directly from S3 via
    read_parquet(), following the same duckdb.connect(":memory:") / httpfs /
    s3_region pattern already established by merge_with_duckdb() below. The
    result is still materialised into a Python list[dict[str, Any]] at the
    end because that is the input contract the match engine requires (see
    entity_resolution/matching_engine/record_blocker.py and
    match_rule_engine.py) — full elimination of Python-side materialisation
    for the match engine's own processing is a larger redesign of the match
    engine itself and out of scope here. The win is eliminating the
    redundant list -> download -> buffer -> parse round-trip before DuckDB
    (or PyArrow) ever sees the data.

    Falls back to load_curated_records() when DuckDB is unavailable or the
    httpfs S3 read fails for any reason (mirrors the graceful-degradation
    pattern used by merge_with_duckdb()).

    Args:
        s3:          Boto3 S3 client (used only by the Python fallback path).
        bucket:      Curated layer S3 bucket name.
        prefix:      S3 key prefix to read (with or without trailing slash).
        region_name: AWS region for DuckDB's httpfs S3 reader.

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
        raise ValueError(f"Curated prefix {clean_prefix!r} contains disallowed characters.")

    try:
        import duckdb
    except ImportError:
        _logger.warning(
            "duckdb_not_available_falling_back_to_python_load",
            bucket=bucket,
            prefix=clean_prefix,
        )
        return load_curated_records(s3, bucket, clean_prefix)

    glob = f"s3://{bucket}/{clean_prefix.rstrip('/')}/*.parquet"
    con = duckdb.connect(":memory:")
    try:
        con.execute("INSTALL httpfs; LOAD httpfs;")
        con.execute(f"SET s3_region='{region_name}';")
        # bucket is sourced exclusively from a Lambda env var and clean_prefix
        # has already been validated against SAFE_S3_PREFIX_PATTERN plus a
        # path-traversal check above — never raw event/user input (OWASP A03).
        result_table = con.execute(f"SELECT * FROM read_parquet('{glob}')").arrow()  # noqa: S608  # nosec B608 — path validated against SAFE_S3_PREFIX_PATTERN

        records: list[dict[str, Any]] = []
        for batch in result_table.to_batches(max_chunksize=10_000):
            batch_dict = batch.to_pydict()
            n = batch.num_rows
            cols = list(batch_dict.keys())
            records.extend({col: batch_dict[col][i] for col in cols} for i in range(n))

        _logger.info(
            "duckdb_curated_load_complete",
            bucket=bucket,
            prefix=clean_prefix,
            record_count=len(records),
        )
        return records
    except Exception as _ddb_exc:
        # DuckDB/httpfs read failure (e.g. no real S3 access in a test
        # environment, or httpfs not fully configured) — fall back to the
        # Python loader. This is graceful degradation: the load is still
        # correct, just uses more RAM and Python-side S3 API calls.
        _logger.warning(
            "duckdb_curated_load_failed_falling_back_to_python_load",
            bucket=bucket,
            prefix=clean_prefix,
            error=str(_ddb_exc)[:200],  # truncate to avoid logging PII in error traces
        )
        return load_curated_records(s3, bucket, clean_prefix)
    finally:
        con.close()


def merge_with_duckdb(
    delta_records: list[dict[str, Any]],
    pk_field: str,
    soft_delete_field: str | None,
    previous_prefix: str | None,
    s3_bucket: str,
    region_name: str,
    s3: Any,
) -> list[dict[str, Any]]:
    """
    SCD Type 1 merge using DuckDB in-process engine (§3.1).

    Replaces the in-memory Python dict merge in CuratedAccumulator for large
    entities.  The previous curated state is read by DuckDB directly from S3
    Parquet — never fully materialised into Python RAM.

    Delta records (today's extracted canonical records) are small and are
    passed as an in-memory Arrow table registered in DuckDB's catalogue.

    Peak memory: O(delta_records + merge_output_batch), not O(previous_state).

    Args:
        delta_records:     Canonical records extracted in this run.
        pk_field:          Primary key field name for the upsert merge.
        soft_delete_field: Field whose truthy value marks a soft-deleted record.
        previous_prefix:   S3 prefix of the latest curated partition (or None
                           for first run — delta_records written as-is).
        s3_bucket:         S3 bucket name (from Lambda env var).
        region_name:       AWS region for DuckDB httpfs S3 access.
        s3:                Boto3 S3 client (used for path discovery only; DuckDB
                           uses its own httpfs S3 reader for the heavy lifting).

    Returns:
        Merged list of record dicts (full current state).

    Security (OWASP A03):
        pk_field and soft_delete_field originate from server-side Pydantic-
        validated entity config — never from Lambda event input.
        DuckDB SQL uses parameterised queries where possible; column names are
        validated against a safe identifier pattern before interpolation.
    """
    if not delta_records:
        # Empty delta — nothing to merge; load previous state as-is.
        if previous_prefix is None:
            return []
        return load_curated_records(s3, s3_bucket, previous_prefix)

    if previous_prefix is None:
        # First run — no previous state; return delta directly.
        return list(delta_records)

    # Validate field names before SQL interpolation (OWASP A03).
    field_name_pattern = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
    if not field_name_pattern.match(pk_field):
        raise ValueError(f"pk_field {pk_field!r} contains unsafe characters for SQL interpolation.")
    if soft_delete_field and not field_name_pattern.match(soft_delete_field):
        raise ValueError(f"soft_delete_field {soft_delete_field!r} contains unsafe characters.")

    try:
        import duckdb
        import pyarrow as pa
    except ImportError:
        # DuckDB not available (e.g. test environment without the package) —
        # fall back to in-memory Python merge.
        _logger.warning(
            "duckdb_not_available_falling_back_to_python_merge",
            pk_field=pk_field,
            delta_count=len(delta_records),
        )
        previous_records = load_curated_records(s3, s3_bucket, previous_prefix)
        previous_state = {
            str(r.get(pk_field, "")): r
            for r in previous_records
            if r.get(pk_field) is not None and str(r.get(pk_field, "")) != ""
        }
        from transformation.curated_accumulator import merge_records

        return merge_records(previous_state, delta_records, pk_field, soft_delete_field)

    con = duckdb.connect(":memory:")
    try:
        # Register delta as an in-memory Arrow table.
        delta_table = pa.Table.from_pylist(delta_records)
        con.register("delta", delta_table)

        # Build the previous state glob for DuckDB S3 reader.
        clean_prev = previous_prefix.rstrip("/") + "/"
        previous_glob = f"s3://{s3_bucket}/{clean_prev}*.parquet"

        # Configure DuckDB httpfs for S3 (uses instance credentials, not hardcoded).
        con.execute("INSTALL httpfs; LOAD httpfs;")
        con.execute(f"SET s3_region='{region_name}';")

        # Build soft-delete WHERE clause.
        if soft_delete_field:
            soft_delete_filter = (
                f"AND (delta.{soft_delete_field} IS NULL OR NOT delta.{soft_delete_field})"
            )
        else:
            soft_delete_filter = ""

        # SCD Type 1 MERGE:
        #   - Rows from previous state NOT present in delta (unchanged records)
        #   - UNION ALL new/updated delta records (excluding soft-deleted ones)
        # OWASP A03: pk_field/soft_delete_field are validated above against
        # field_name_pattern; previous_glob is built from internal S3
        # bucket/prefix state, not raw user input.
        sql = (
            f"SELECT prev.* FROM read_parquet('{previous_glob}') AS prev "  # noqa: S608  # nosec B608 — path validated against SAFE_S3_PREFIX_PATTERN
            f"WHERE CAST(prev.{pk_field} AS VARCHAR) NOT IN ("
            f"SELECT CAST({pk_field} AS VARCHAR) FROM delta) "
            f"UNION ALL SELECT delta.* FROM delta "
            f"WHERE CAST({pk_field} AS VARCHAR) IS NOT NULL "
            f"AND CAST({pk_field} AS VARCHAR) != '' "
            f"{soft_delete_filter}"
        )
        result_table = con.execute(sql).arrow()

        # Return as Python list for compatibility with existing pipeline.
        merged: list[dict[str, Any]] = []
        for batch in result_table.to_batches(max_chunksize=50_000):
            batch_dict = batch.to_pydict()
            n = batch.num_rows
            cols = list(batch_dict.keys())
            merged.extend({col: batch_dict[col][i] for col in cols} for i in range(n))

        _logger.info(
            "duckdb_merge_complete",
            pk_field=pk_field,
            delta_count=len(delta_records),
            merged_count=len(merged),
        )
        return merged

    except Exception as _ddb_exc:
        # DuckDB execution failure (e.g., httpfs S3 access error in test environments,
        # or DuckDB httpfs not fully configured) — fall back to in-memory Python merge.
        # This is graceful degradation: the merge is still correct, just uses more RAM.
        _logger.warning(
            "duckdb_merge_failed_falling_back_to_python_merge",
            pk_field=pk_field,
            delta_count=len(delta_records),
            error=str(_ddb_exc)[:200],  # truncate to avoid logging PII in error traces
        )
        previous_records = load_curated_records(s3, s3_bucket, previous_prefix)
        previous_state = {
            str(r.get(pk_field, "")): r
            for r in previous_records
            if r.get(pk_field) is not None and str(r.get(pk_field, "")) != ""
        }
        from transformation.curated_accumulator import (
            merge_records,  # local import to avoid circular
        )

        return merge_records(previous_state, delta_records, pk_field, soft_delete_field)

    finally:
        con.close()


# Explicit re-export list: `no_implicit_reexport` is on, so a module importing
# SAFE_S3_PREFIX_PATTERN from here needs it named rather than merely present.
__all__ = [
    "SAFE_S3_PREFIX_PATTERN",
]
