"""
Per-tenant usage metering (L17, gap 20).

Nothing tracked records processed per tenant per period, which blocks any consumption-based
billing model and also blocks the cheaper question a platform team asks first: *which tenant is
driving the cost*.

Design decisions worth stating, because each rules out an easier option:

- **Derived from the run audit log, not from a new counter.** Every stage already writes an audit
  record with its record count; a parallel counter would be a second source of truth that can
  disagree with the audit log an invoice would have to be defended against.
- **Idempotent by period key.** A metering run that executes twice must not double-count, so the
  aggregate is *written* per (tenant, period) rather than incremented — re-running recomputes the
  same value from the same audit records.
- **Records processed, not records stored.** Storage is a separate meter with a separate unit, and
  conflating them produces a number nobody can reconcile against either.
"""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import boto3

from contracts.identifier_policy import validate_tenant_code
from contracts.observability_contract import PipelineStage
from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger
from persistence.dynamodb_paging import iter_items

_logger = get_platform_logger(__name__)

_USAGE_TABLE_NAME: Final[str] = "EdlTenantUsage"

# The GSI added in S12; metering is its first consumer.
_AUDIT_TENANT_INDEX: Final[str] = "tenant-started-index"

# Stages whose record counts are billable throughput. Extraction is the meter: a record is counted
# once where it enters the platform, not again at every stage it passes through — summing all
# stages would multiply one record by the number of stages and inflate an invoice fourfold.
BILLABLE_STAGES: Final[frozenset[str]] = frozenset({PipelineStage.EXTRACTION.value})


@dataclass
class TenantUsage:
    """One tenant's measured usage for one period."""

    tenant_code: str
    period: str  # YYYY-MM
    records_processed: int = 0
    runs_completed: int = 0
    runs_failed: int = 0
    per_entity_records: dict[str, int] = field(default_factory=dict)

    @property
    def usage_key(self) -> str:
        return f"usage#{self.period}"

    def to_item(self, environment: str) -> dict[str, Any]:
        return {
            "tenant_code": self.tenant_code,
            "usage_key": self.usage_key,
            "period": self.period,
            "records_processed": self.records_processed,
            "runs_completed": self.runs_completed,
            "runs_failed": self.runs_failed,
            "per_entity_records": self.per_entity_records,
            "environment": environment,
            "computed_at": datetime.now(UTC).isoformat(),
        }


def aggregate_usage(
    audit_records: list[dict[str, Any]], tenant_code: str, period: str
) -> TenantUsage:
    """
    Aggregate one tenant's audit records into a usage total for a period.

    Pure so it can be tested without AWS and re-run over the same input to the same answer, which
    is what makes the write idempotent.
    """
    usage = TenantUsage(tenant_code=validate_tenant_code(tenant_code), period=period)
    per_entity: dict[str, int] = defaultdict(int)

    for record in audit_records:
        stage = str(record.get("stage", ""))
        status = str(record.get("status", "")).lower()

        if status == "success" and stage in BILLABLE_STAGES:
            count = int(record.get("record_count", 0) or 0)
            usage.records_processed += count
            per_entity[str(record.get("entity_id", "unknown"))] += count

        # Run outcomes are counted at the terminal stage so one run contributes one outcome,
        # regardless of how many stages it wrote records for.
        if stage in BILLABLE_STAGES:
            if status == "success":
                usage.runs_completed += 1
            elif status == "failed":
                usage.runs_failed += 1

    usage.per_entity_records = dict(per_entity)
    return usage


class TenantUsageRepository:
    """Persists computed usage, one item per tenant per period."""

    def __init__(self, environment: str, region_name: str) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        self._environment = environment
        table_name = os.environ.get("TENANT_USAGE_TABLE") or _USAGE_TABLE_NAME
        self._table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)

    def save(self, usage: TenantUsage) -> None:
        """
        Write the period's usage, overwriting any previous computation for that period.

        Overwrite rather than increment: re-running the metering job must produce the same number,
        and an increment would double-count on the second run. The audit log remains the source of
        truth, so a recomputation is always safe.
        """
        self._table.put_item(Item=usage.to_item(self._environment))
        record_platform_metric(
            PlatformMetric.TENANT_RECORDS_PROCESSED,
            float(usage.records_processed),
            Period=usage.period,
        )
        _logger.info(
            "tenant_usage_recorded",
            tenant_code=usage.tenant_code,
            period=usage.period,
            records_processed=usage.records_processed,
            runs_completed=usage.runs_completed,
            runs_failed=usage.runs_failed,
            entities=len(usage.per_entity_records),
        )

    def get(self, tenant_code: str, period: str) -> dict[str, Any] | None:
        tenant_code = validate_tenant_code(tenant_code)
        response = self._table.get_item(
            Key={"tenant_code": tenant_code, "usage_key": f"usage#{period}"}
        )
        item = response.get("Item")
        return dict(item) if item else None

    def list_periods(self, tenant_code: str) -> list[dict[str, Any]]:
        """Every computed period for one tenant, newest first."""
        tenant_code = validate_tenant_code(tenant_code)
        response = self._table.query(
            KeyConditionExpression="tenant_code = :tc AND begins_with(usage_key, :prefix)",
            ExpressionAttributeValues={":tc": tenant_code, ":prefix": "usage#"},
        )
        return sorted(
            (dict(item) for item in response.get("Items", [])),
            key=lambda item: str(item.get("period", "")),
            reverse=True,
        )


def read_audit_records_for_period(
    *, tenant_code: str, period: str, region_name: str
) -> list[dict[str, Any]]:
    """
    Read one tenant's audit records for a period via the `tenant-started-index` GSI.

    This is the first consumer of that index (added in S12): the base table is keyed on
    (run_id, stage) because a run is the natural aggregate, and nothing about that key answers
    "this tenant's runs in July", which is precisely the metering question.

    `period` is YYYY-MM, and `started_at` is an ISO-8601 string, so a `begins_with` on the range
    key selects the month without a scan or a filter expression.
    """
    tenant_code = validate_tenant_code(tenant_code)
    table_name = os.environ.get("AUDIT_LOG_TABLE") or "EdlRunAuditLog"
    table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)

    # Drains deliberately: a usage period must be metered whole, because an invoice built on a
    # truncated read understates. This is the caller `iter_items` exists for.
    return list(
        iter_items(
            table,
            IndexName=_AUDIT_TENANT_INDEX,
            KeyConditionExpression="tenant_code = :tc AND begins_with(started_at, :period)",
            ExpressionAttributeValues={":tc": tenant_code, ":period": period},
        )
    )


def current_period() -> str:
    """The billing period a run happening now belongs to."""
    return datetime.now(UTC).strftime("%Y-%m")
