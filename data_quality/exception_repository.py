"""
`EdlDataQualityException` — the structured exception store (DL-DQ-14).

PK `tenant_code`, SK `{run_id}#{rule_id}#{seq}`, GSI on `entity_id` + `detected_at`.

Every quality violation, count mismatch, and orphan lands here with a resolution state, so
quality findings become an operational process (DL-WF-06) rather than a log line.

Security (OWASP A01, A02): tenant-partitioned at the key level from creation, and offending
key samples are masked through the classification policy before persistence —
SENSITIVE_PII never appears, even masked.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final

import boto3

from contracts.identifier_policy import validate_tenant_code
from governance.data_classification_policy import (
    DataClassificationLevel,
    EntityClassificationPolicy,
)
from observability.structured_logger import get_platform_logger
from persistence.dynamodb_paging import DEFAULT_PAGE_SIZE, Page, fetch_page, iter_items

_logger = get_platform_logger(__name__)

_TABLE_NAME: Final[str] = "EdlDataQualityException"

# Offending-key samples are capped so one bad batch cannot write an unbounded item.
MAX_SAMPLE_KEYS: Final[int] = 20

DEFAULT_EXCEPTION_TTL_DAYS: Final[int] = 180


class ExceptionKind(StrEnum):
    """What produced the exception."""

    QUALITY_VIOLATION = "quality_violation"
    RECORD_COUNT_MISMATCH = "record_count_mismatch"
    KEY_FIELD_MISMATCH = "key_field_mismatch"
    COMPLETENESS_BELOW_THRESHOLD = "completeness_below_threshold"
    DUPLICATE_RATE_EXCEEDED = "duplicate_rate_exceeded"
    REFERENTIAL_ORPHAN = "referential_orphan"
    DATE_VALIDATION = "date_validation"
    RECONCILIATION_VARIANCE = "reconciliation_variance"
    UNATTRIBUTED_ROWS = "unattributed_rows"


class ExceptionSeverity(StrEnum):
    """Severity drives the promotion gate (DL-DQ-15)."""

    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class ResolutionState(StrEnum):
    """Triage lifecycle, consumed by the workflow engine's task model."""

    OPEN = "open"
    ASSIGNED = "assigned"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    ACCEPTED = "accepted"
    CLOSED = "closed"


_TERMINAL_STATES: Final[frozenset[ResolutionState]] = frozenset(
    {ResolutionState.RESOLVED, ResolutionState.ACCEPTED, ResolutionState.CLOSED}
)


@dataclass
class QualityException:
    """One structured exception record."""

    tenant_code: str
    run_id: str
    rule_id: str
    entity_id: str
    kind: ExceptionKind
    severity: ExceptionSeverity
    message: str
    correlation_id: str
    sequence: int = 0
    source_id: str = ""
    connection_id: str | None = None
    scope_unit_id: str | None = None
    sample_keys: tuple[str, ...] = ()
    key_field_name: str = ""
    observed_value: str = ""
    expected_value: str = ""
    resolution_state: ResolutionState = ResolutionState.OPEN
    assignee: str | None = None
    resolution_note: str = ""
    detected_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def __post_init__(self) -> None:
        validate_tenant_code(self.tenant_code)

    @property
    def sort_key(self) -> str:
        return f"{self.run_id}#{self.rule_id}#{self.sequence:04d}"

    @property
    def blocks_promotion(self) -> bool:
        return self.severity is ExceptionSeverity.ERROR


class DataQualityExceptionRepository:
    """Writes and reads exception records; shared by DL-02, DL-06, and DL-09."""

    def __init__(
        self,
        environment: str,
        region_name: str,
        classification_policy: EntityClassificationPolicy | None = None,
        ttl_days: int = DEFAULT_EXCEPTION_TTL_DAYS,
    ) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        self._environment = environment
        self._classification_policy = classification_policy
        self._ttl_days = ttl_days
        table_name = os.environ.get("DATA_QUALITY_EXCEPTION_TABLE") or _TABLE_NAME
        self._table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)

    # ── Writes ────────────────────────────────────────────────────────────────

    def record(self, exception: QualityException) -> str:
        """Persist one exception, masking its samples first."""
        item = self._serialise(exception)
        self._table.put_item(Item=item)
        _logger.info(
            "data_quality_exception_recorded",
            tenant_code=exception.tenant_code,
            entity_id=exception.entity_id,
            rule_id=exception.rule_id,
            kind=exception.kind.value,
            severity=exception.severity.value,
        )
        return str(item["exception_key"])

    def record_many(self, exceptions: list[QualityException]) -> int:
        """Batch-write exceptions; sequence numbers are assigned per (run, rule)."""
        counters: dict[tuple[str, str], int] = {}
        with self._table.batch_writer() as batch:
            for exception in exceptions:
                key = (exception.run_id, exception.rule_id)
                exception.sequence = counters.get(key, 0)
                counters[key] = exception.sequence + 1
                batch.put_item(Item=self._serialise(exception))
        return len(exceptions)

    def transition(
        self,
        tenant_code: str,
        exception_key: str,
        target: ResolutionState,
        *,
        assignee: str | None = None,
        resolution_note: str = "",
    ) -> None:
        """Move an exception through triage; a terminal state requires a note."""
        if target in _TERMINAL_STATES and not resolution_note:
            raise ValueError(
                f"Transitioning an exception to {target.value!r} requires a resolution note — "
                "a closed finding with no explanation is not an audit record."
            )
        expression = "SET resolution_state = :state, resolution_note = :note, updated_at = :ts"
        values: dict[str, Any] = {
            ":state": target.value,
            ":note": resolution_note,
            ":ts": datetime.now(UTC).isoformat(),
        }
        if assignee is not None:
            expression += ", assignee = :assignee"
            values[":assignee"] = assignee
        self._table.update_item(
            Key={
                "tenant_code": validate_tenant_code(tenant_code),
                "exception_key": exception_key,
            },
            UpdateExpression=expression,
            ExpressionAttributeValues=values,
        )

    # ── Reads ─────────────────────────────────────────────────────────────────

    def list_for_run(self, tenant_code: str, run_id: str) -> list[dict[str, Any]]:
        """
        Every exception recorded for one run.

        Drains every page. A single `query` stops at DynamoDB's 1 MB limit, and a partial list is
        indistinguishable from a clean run — any caller deciding whether to promote on that basis
        would fail open. Bounded in practice by the run, not by the tenant's history.
        """
        tenant_code = validate_tenant_code(tenant_code)
        return list(
            iter_items(
                self._table,
                KeyConditionExpression=("tenant_code = :tc AND begins_with(exception_key, :run)"),
                ExpressionAttributeValues={":tc": tenant_code, ":run": f"{run_id}#"},
            )
        )

    def list_open(
        self,
        tenant_code: str,
        *,
        limit: int = DEFAULT_PAGE_SIZE,
        start_key: dict[str, Any] | None = None,
    ) -> Page:
        """
        One page of open findings, for the operations dashboard and the triage inbox.

        Returns a `Page`, not a list. This used to drain the tenant's entire exception history and
        filter states in Python — and because the table's TTL is deliberately disabled to preserve
        audit evidence, that history grows with data volume rather than with run count. At the
        12-month target (see docs/SCALE_AND_DLQ_THRESHOLDS.md) that is a Lambda OOM waiting to
        happen on a dashboard load.

        Filtering server-side via `FilterExpression` means a page can come back empty while more
        pages remain, so callers must follow `next_key` and never treat an empty page as the end.
        """
        tenant_code = validate_tenant_code(tenant_code)
        open_states = (
            ResolutionState.OPEN.value,
            ResolutionState.ASSIGNED.value,
            ResolutionState.IN_PROGRESS.value,
        )
        placeholders = {f":s{index}": state for index, state in enumerate(open_states)}
        return fetch_page(
            self._table,
            limit=limit,
            start_key=start_key,
            KeyConditionExpression="tenant_code = :tc",
            FilterExpression=f"resolution_state IN ({', '.join(placeholders)})",
            ExpressionAttributeValues={":tc": tenant_code, **placeholders},
        )

    # `blocking_exceptions()` was removed on 2026-07-29. Its docstring called it "the input to
    # the promotion gate" and it had no production caller — only a unit test, which made dead
    # code read as wired. Promotion is already blocked in-run: `run_batch_quality_gate` raises
    # `QualityGateBlockedError` after persisting the evidence, so a second mechanism reading the
    # same rows back would be the duplication the `build_merge_plan` decision already rejected.
    # If an out-of-run promotion check is ever needed, it should query by severity through a
    # sparse index rather than re-filter a full-run read in Python.

    # ── Private ───────────────────────────────────────────────────────────────

    def _serialise(self, exception: QualityException) -> dict[str, Any]:
        expires_at = int((datetime.now(UTC) + timedelta(days=self._ttl_days)).timestamp())
        return {
            "tenant_code": exception.tenant_code,
            "exception_key": exception.sort_key,
            "run_id": exception.run_id,
            "rule_id": exception.rule_id,
            "entity_id": exception.entity_id,
            "source_id": exception.source_id,
            "connection_id": exception.connection_id,
            "scope_unit_id": exception.scope_unit_id,
            "kind": exception.kind.value,
            "severity": exception.severity.value,
            "message": exception.message,
            "observed_value": exception.observed_value,
            "expected_value": exception.expected_value,
            "sample_keys": list(self._mask_samples(exception)),
            "resolution_state": exception.resolution_state.value,
            "assignee": exception.assignee,
            "resolution_note": exception.resolution_note,
            "correlation_id": exception.correlation_id,
            "detected_at": exception.detected_at,
            "environment": self._environment,
            "expires_at": expires_at,
        }

    def _mask_samples(self, exception: QualityException) -> tuple[str, ...]:
        """
        Mask offending-key samples before persistence.

        A SENSITIVE_PII key field is dropped entirely rather than masked — a masked value
        still confirms the record exists, which is disclosure the policy forbids. With no
        policy supplied, samples are redacted anyway: fail closed, not open.
        """
        samples = exception.sample_keys[:MAX_SAMPLE_KEYS]
        policy = self._classification_policy
        if policy is None:
            return tuple(_redact(sample) for sample in samples)
        level = next(
            (
                f.classification
                for f in policy.field_classifications
                if f.field_name == exception.key_field_name
            ),
            DataClassificationLevel.INTERNAL,
        )
        if level is DataClassificationLevel.SENSITIVE_PII:
            return ()
        if level is DataClassificationLevel.PII:
            return tuple(_redact(sample) for sample in samples)
        return samples


def _redact(value: str) -> str:
    """Keep enough of a key to correlate it with the source, never enough to read it."""
    text = str(value)
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}{'*' * (len(text) - 4)}{text[-2:]}"
