"""
Curated-layer accumulator for incremental entity extraction.

Implements the industry-standard SCD Type 1 (current-state) merge pattern:
the curated layer always holds the full current state of all records, not just
the latest incremental delta.

On each incremental run the accumulator:
  1. Loads the previous curated snapshot from S3 (first run → empty dict).
  2. Merges today's extracted delta (canonical records) into it using a primary
     key lookup (O(n) — single pass over delta, dict lookup for each record).
  3. Removes soft-deleted records when soft_delete_field is configured.
  4. Returns the full merged result for writing to the new curated partition.

Effect on downstream stages:
  - Entity resolution always sees the complete current state → correct golden
    records regardless of how many records changed in any single run.
  - Analytics publisher writes a full daily snapshot → consistent BI queries.

Design decisions:
  - merge_records() is a pure function (no I/O) — fully unit-testable without
    mocks.
  - CuratedAccumulator is injected into TransformationPipeline as an optional
    dependency — full-load entities pass None and are completely unaffected.
  - Idempotent: re-running the same extraction twice produces the same merged
    output (same delta applied to the same previous state).
  - First-run safe: when no previous curated partition exists, merge behaves
    identically to a plain write (previous_state = empty dict).

Security (OWASP A03, A04):
  - pk_field and soft_delete_field originate from the server-side entity config
    (DynamoDB — validated by Pydantic) and are never derived from user-controlled
    Lambda event input.
  - S3 prefix validation is delegated to load_curated_records() in curated_layer_reader.

Performance:
  - Previous state is loaded into a dict keyed by pk_value — O(1) lookup per
    delta record.  Peak memory = O(n) where n = total current state size.
  - At 36 K records x ~200 bytes avg ≈ 7 MB — well within Lambda 1 GB limit.
  - For future entities with millions of records, primary_key_field can be left
    as None to bypass merge entirely (append-only behaviour).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from contracts.identifier_policy import validate_tenant_code
from observability.structured_logger import get_platform_logger
from transformation.curated_layer_reader import (
    find_latest_curated_prefix,
    merge_with_duckdb,
)

_logger = get_platform_logger(__name__)


def merge_records(
    previous_state: dict[str, Any],
    delta_records: list[dict[str, Any]],
    pk_field: str,
    soft_delete_field: str | None = None,
) -> list[dict[str, Any]]:
    """
    Merge an incremental delta into the previous curated state (pure function).

    Implements SCD Type 1: latest value wins.  Each delta record either:
      - Upserts into the merged state (new or modified record).
      - Removes from the merged state when soft_delete_field is truthy.

    Args:
        previous_state:    Mapping of pk_value (str) → record representing the
                           full state from the last curated partition.  Pass an
                           empty dict for the first incremental run.
        delta_records:     Canonical records extracted in this run (new and
                           modified records since the last watermark).
        pk_field:          Canonical field name used as the merge key.
        soft_delete_field: When set, records where this canonical field evaluates
                           to a truthy value are removed from the merged state.
                           None means no soft-delete tracking.

    Returns:
        New list of records representing the full current state after merge.
        Order is not guaranteed (dict insertion order for previous records,
        then updated/inserted delta records at the end).

    Notes:
        - Delta records without a pk_field value are skipped with a warning.
        - Re-applying the same delta to the same previous_state produces the
          same output (idempotent).
        - pk_field and soft_delete_field must originate from server-side config
          only (OWASP A03).
    """
    merged: dict[str, Any] = dict(previous_state)
    missing_pk_count = 0

    for record in delta_records:
        pk_raw = record.get(pk_field)
        if pk_raw is None or str(pk_raw) == "":
            missing_pk_count += 1
            continue

        pk_value = str(pk_raw)

        if soft_delete_field is not None:
            delete_marker = record.get(soft_delete_field)
            if delete_marker is not None and bool(delete_marker):
                merged.pop(pk_value, None)
                continue

        merged[pk_value] = record

    if missing_pk_count:
        _logger.warning(
            "curated_merge_records_missing_pk",
            pk_field=pk_field,
            missing_count=missing_pk_count,
        )

    return list(merged.values())


@dataclass(frozen=True)
class AccumulateResult:
    """Outcome of a single CuratedAccumulator.accumulate() call."""

    merged_records: list[dict[str, Any]]
    previous_record_count: int
    delta_record_count: int
    merged_record_count: int
    deleted_record_count: int


class CuratedAccumulator:
    """
    Loads the previous curated state and merges today's delta into it.

    Injected into TransformationPipeline as an optional dependency.  When None
    is passed, the pipeline behaves identically to its original behaviour — no
    merge, no extra S3 reads, no code path change.

    One instance per Lambda invocation.  The S3 client is injected at
    construction time and reused across calls within the same warm invocation.
    """

    def __init__(
        self,
        s3: Any,
        curated_s3_bucket: str,
        primary_key_field: str,
        tenant_code: str,
        soft_delete_field: str | None = None,
        region_name: str = "us-east-1",
    ) -> None:
        """
        Args:
            s3:                Boto3 S3 client.
            curated_s3_bucket: Curated layer S3 bucket name — from Lambda env var.
            primary_key_field: Canonical field used as the upsert key.
            tenant_code:       Tenant identity for this run — the previous-state
                               lookup must match CuratedLayerWriter's tenant-
                               prefixed write path (ARCH-1), or SCD merge silently
                               reads no previous state (or another tenant's).
            soft_delete_field: Optional canonical field whose truthy value marks a
                               record as soft-deleted.
            region_name:       AWS region for DuckDB httpfs S3 configuration.
        """
        if not primary_key_field or not primary_key_field.strip():
            raise ValueError(
                "primary_key_field must be a non-empty string. "
                "This field is required by CuratedAccumulator for SCD Type 1 merge."
            )
        self._s3 = s3
        self._bucket = curated_s3_bucket
        self._pk_field = primary_key_field
        self._tenant_code = validate_tenant_code(tenant_code)
        self._soft_delete_field = soft_delete_field
        self._region_name = region_name

    def accumulate(
        self,
        delta_records: list[dict[str, Any]],
        domain: str,
        entity_id: str,
        run_id: str,
    ) -> AccumulateResult:
        """
        Load the previous curated snapshot and merge today's delta into it.

        Args:
            delta_records: Canonical records from today's extraction (already
                           field-mapped and masked).
            domain:        S3/Glue domain string (e.g. "salesforce").
            entity_id:     Stable entity identifier (e.g. "salesforce-contact").
            run_id:        Current run identifier for observability.

        Returns:
            AccumulateResult with the merged records list and counts for logging.

        Behaviour on first run (no previous curated partition):
            Returns delta_records as-is — no previous state to merge with.
        """
        previous_prefix = find_latest_curated_prefix(
            self._s3, self._bucket, domain, entity_id, self._tenant_code
        )

        if previous_prefix is None:
            _logger.info(
                "curated_accumulator_first_run",
                domain=domain,
                entity_id=entity_id,
                run_id=run_id,
            )
            return AccumulateResult(
                merged_records=list(delta_records),
                previous_record_count=0,
                delta_record_count=len(delta_records),
                merged_record_count=len(delta_records),
                deleted_record_count=0,
            )

        merged = merge_with_duckdb(
            delta_records=delta_records,
            pk_field=self._pk_field,
            soft_delete_field=self._soft_delete_field,
            previous_prefix=previous_prefix,
            s3_bucket=self._bucket,
            region_name=self._region_name,
            s3=self._s3,
        )

        merged_pk_set = {
            str(r.get(self._pk_field, "")) for r in merged if r.get(self._pk_field) is not None
        }
        delta_pk_set = {
            str(r.get(self._pk_field, ""))
            for r in delta_records
            if r.get(self._pk_field) is not None
        }
        deleted_count = sum(1 for pk in delta_pk_set if pk not in merged_pk_set)

        result = AccumulateResult(
            merged_records=merged,
            previous_record_count=-1,  # Not loaded into Python RAM with DuckDB merge
            delta_record_count=len(delta_records),
            merged_record_count=len(merged),
            deleted_record_count=deleted_count,
        )

        _logger.info(
            "curated_accumulator_merge_complete",
            domain=domain,
            entity_id=entity_id,
            run_id=run_id,
            delta_record_count=result.delta_record_count,
            merged_record_count=result.merged_record_count,
            deleted_record_count=result.deleted_record_count,
        )

        return result
