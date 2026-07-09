"""
DynamoDB-backed watermark repository for the Enterprise Data Lake platform.

The watermark tracks extraction progress for each source entity:
  - last_successful_watermark: upper bound of the last fully-committed extraction
  - upper_watermark: planned upper bound of the in-progress extraction

Watermark advancement is protected by optimistic concurrency (DynamoDB
conditional expressions on the version attribute).  The watermark is advanced
ONLY after a fully successful extraction run — partial or failed runs leave
the watermark unchanged.

DynamoDB table: EdlWatermarkRepository
  PK: source_id (string) — stores tenant_scoped_key(tenant_code, source_id) (ARCH-1),
      e.g. "demo#salesforce", so two tenants extracting the same source_id/entity_id
      never share an item
  SK: entity_id (string)

Replay support: callers compute a historic window using compute_replay_window()
and execute extraction without advancing the watermark.  The run_id still
advances so the replay is auditable.

Security:
  - DynamoDB access uses the IAM extraction_runtime role (injected boto3 session).
  - Conditional expressions prevent lost-update races between concurrent processes.
  - Watermark datetimes are stored as ISO-8601 strings (DynamoDB has no native
    datetime type); they are always parsed back through datetime.fromisoformat().
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import boto3
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field, field_validator

from contracts.entity_configuration_contract import EntityExtractionConfig, LoadType
from contracts.identifier_policy import (
    DEFAULT_TENANT_CODE,
    tenant_scoped_key,
    validate_tenant_code,
)
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

_WATERMARK_TABLE_NAME: Final[str] = "EdlWatermarkRepository"

# Lower-bound sentinel for an entity's first-ever run: initialise_watermark()
# already seeds last_successful_watermark to this value, and
# compute_extraction_window() now uses it directly for the "no watermark yet"
# case too, so the actual extraction query and the watermark bookkeeping agree
# on what "first run" means. Deliberately epoch, not extraction_window_days —
# that field is capped at 365 days (contracts/entity_configuration_contract.py)
# as a steady-state guardrail, never meant to bound a first-time backfill.
_EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


class WatermarkRecord(BaseModel):
    """
    Immutable snapshot of the watermark state for one source entity.

    Returned by the repository; never mutated by callers.  To advance the
    watermark, pass the record to WatermarkRepository.advance_watermark().
    """

    model_config = {"frozen": True, "extra": "ignore"}

    source_id: str
    entity_id: str
    environment: str
    last_successful_watermark: datetime
    upper_watermark: datetime
    run_id: str
    updated_at: datetime
    version: int = Field(ge=0)
    tenant_code: str = DEFAULT_TENANT_CODE

    @field_validator("last_successful_watermark", "upper_watermark", "updated_at", mode="before")
    @classmethod
    def _coerce_datetime(cls, value: datetime | str) -> datetime:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=UTC)
            return value
        if isinstance(value, str):
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=UTC)
            return dt
        raise ValueError(f"Expected a datetime or ISO-8601 string, got {type(value).__name__!r}.")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class WatermarkConcurrencyError(Exception):
    """
    Raised when an optimistic concurrency check fails during watermark update.

    The caller should reload the watermark record and retry if appropriate.
    """


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class WatermarkRepository:
    """
    DynamoDB-backed watermark repository with optimistic concurrency.

    One instance per service invocation; boto3 DynamoDB resource is thread-safe
    for read/write operations under the hood.
    """

    def __init__(self, environment: str, region_name: str) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        self._environment = environment
        self._table_name = os.environ.get("WATERMARK_TABLE") or _WATERMARK_TABLE_NAME
        dynamodb = boto3.resource("dynamodb", region_name=region_name)
        self._table = dynamodb.Table(self._table_name)

    # ── Read ───────────────────────────────────────────────────────────────────

    def get_watermark(
        self, source_id: str, entity_id: str, tenant_code: str = DEFAULT_TENANT_CODE
    ) -> WatermarkRecord | None:
        """
        Load the current watermark for a source entity.

        Returns None on first run (no prior record exists for this tenant).
        The DynamoDB key itself is tenant-scoped (ARCH-1: tenant_scoped_key()
        applied to source_id), so a different tenant's record can never be
        read here — not merely masked after the fact.

        Uses a strongly-consistent read to prevent stale-read races.
        """
        tenant_code = validate_tenant_code(tenant_code)
        scoped_source_id = tenant_scoped_key(tenant_code, source_id)
        try:
            response = self._table.get_item(
                Key={"source_id": scoped_source_id, "entity_id": entity_id},
                ConsistentRead=True,
            )
        except ClientError as exc:
            _logger.warning(
                "watermark_load_error",
                source_id=source_id,
                entity_id=entity_id,
                error_code=exc.response["Error"]["Code"],
            )
            raise

        item = response.get("Item")
        if not item:
            return None
        # The stored item's "source_id" attribute is the tenant-scoped composite
        # key value — restore the plain source_id before constructing the model
        # so callers always see the unscoped identifier they passed in.
        record_item = {**item, "source_id": source_id}
        return WatermarkRecord(**record_item)  # type: ignore[arg-type]

    # ── Write ──────────────────────────────────────────────────────────────────

    def initialise_watermark(
        self,
        source_id: str,
        entity_id: str,
        upper_watermark: datetime,
        run_id: str,
        tenant_code: str = DEFAULT_TENANT_CODE,
    ) -> WatermarkRecord:
        """
        Create the initial watermark record for a source entity (first run only).

        Uses a conditional PutItem to prevent overwriting if a concurrent
        initialisation wins the race.  When the condition fails the existing
        record is loaded and returned instead.
        """
        tenant_code = validate_tenant_code(tenant_code)
        now = datetime.now(tz=UTC)
        record = WatermarkRecord(
            source_id=source_id,
            entity_id=entity_id,
            environment=self._environment,
            last_successful_watermark=_EPOCH,
            upper_watermark=upper_watermark,
            run_id=run_id,
            updated_at=now,
            version=0,
            tenant_code=tenant_code,
        )
        try:
            self._table.put_item(
                Item=_serialise_watermark(record),
                ConditionExpression="attribute_not_exists(source_id)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # A concurrent initialisation succeeded first — return that record.
                existing = self.get_watermark(source_id, entity_id, tenant_code=tenant_code)
                if existing is not None:
                    return existing
            raise
        return record

    def advance_watermark(
        self,
        current: WatermarkRecord,
        new_upper_watermark: datetime,
        run_id: str,
    ) -> WatermarkRecord:
        """
        Advance the watermark to new_upper_watermark after a successful run.

        Optimistic concurrency: the DynamoDB write succeeds only when the stored
        version equals current.version.  If another process advanced the watermark
        concurrently, WatermarkConcurrencyError is raised.

        The caller must NOT call this method on failed or partial runs.

        Raises:
            ValueError: new_upper_watermark is earlier than current.upper_watermark,
                which would create a gap in incremental extraction coverage.
            WatermarkConcurrencyError: stored version differs from current.version.
        """
        if new_upper_watermark < current.upper_watermark:
            raise ValueError(
                f"new_upper_watermark ({new_upper_watermark.isoformat()}) must be >= "
                f"current upper_watermark ({current.upper_watermark.isoformat()}). "
                "Advancing the watermark backward would create an extraction gap in "
                "subsequent incremental runs."
            )
        now = datetime.now(tz=UTC)
        new_version = current.version + 1
        updated = WatermarkRecord(
            source_id=current.source_id,
            entity_id=current.entity_id,
            environment=current.environment,
            last_successful_watermark=current.upper_watermark,
            upper_watermark=new_upper_watermark,
            run_id=run_id,
            updated_at=now,
            version=new_version,
            tenant_code=current.tenant_code,
        )
        try:
            self._table.put_item(
                Item=_serialise_watermark(updated),
                ConditionExpression="version = :expected",
                ExpressionAttributeValues={":expected": current.version},
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise WatermarkConcurrencyError(
                    f"Watermark concurrency conflict for "
                    f"source_id={current.source_id!r} entity_id={current.entity_id!r}: "
                    f"expected version {current.version} but the record was modified "
                    "by another process.  Reload and retry."
                ) from exc
            raise
        return updated

    # ── Window computation ─────────────────────────────────────────────────────

    @staticmethod
    def compute_extraction_window(
        watermark: WatermarkRecord | None,
        config: EntityExtractionConfig,
        reference_time: datetime,
    ) -> tuple[datetime, datetime]:
        """
        Compute the (lower_bound, upper_bound) extraction window.

        INCREMENTAL with prior watermark:
          lower = last_successful_watermark - watermark_overlap_hours
          upper = reference_time

        INCREMENTAL first run (no watermark) — full historical backfill:
          lower = epoch (1970-01-01), regardless of extraction_window_days
          upper = reference_time

        FULL load:
          lower = reference_time - extraction_window_days (for audit; connector
                  typically ignores lower on full loads)
          upper = reference_time
        """
        upper = reference_time
        if config.load_type == LoadType.FULL:
            lower = reference_time - timedelta(days=config.extraction_window_days)
        elif watermark is None:
            # First-ever incremental run for this entity (per tenant) — pull
            # full history from epoch. extraction_window_days does not apply
            # here: it's capped at 365 days as a steady-state guardrail, which
            # would silently truncate a first-time backfill of older data.
            # Large/slow first runs rely on the existing checkpoint mechanism
            # (max_records_per_lambda_run / LambdaTimeoutWarning) rather than
            # a bounded window, same as any other incremental run would.
            lower = _EPOCH
        else:
            overlap = timedelta(hours=config.watermark_overlap_hours)
            lower = watermark.last_successful_watermark - overlap
        return lower, upper

    @staticmethod
    def compute_replay_window(
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[datetime, datetime]:
        """
        Return the explicitly provided historic window for a replay run.

        The watermark is NEVER advanced after a replay; this method just
        validates and surfaces the caller-provided bounds as a typed pair.
        """
        if window_start >= window_end:
            raise ValueError(
                f"Replay window_start ({window_start.isoformat()}) must be "
                f"before window_end ({window_end.isoformat()})."
            )
        return window_start, window_end


# ---------------------------------------------------------------------------
# Serialisation helpers (module-level; not methods — simplifies testing)
# ---------------------------------------------------------------------------


def _serialise_watermark(record: WatermarkRecord) -> dict[str, Any]:
    """
    Convert a WatermarkRecord to a DynamoDB-compatible item dict.

    The persisted "source_id" attribute is the tenant-scoped composite key
    (ARCH-1) — get_watermark() restores the plain source_id when reading back.
    """
    return {
        "source_id": tenant_scoped_key(record.tenant_code, record.source_id),
        "entity_id": record.entity_id,
        "environment": record.environment,
        "last_successful_watermark": record.last_successful_watermark.isoformat(),
        "upper_watermark": record.upper_watermark.isoformat(),
        "run_id": record.run_id,
        "updated_at": record.updated_at.isoformat(),
        "version": record.version,
        "tenant_code": record.tenant_code,
    }
