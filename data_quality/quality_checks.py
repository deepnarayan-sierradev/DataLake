"""
Completeness, duplicate, referential and date checks (DL-DQ-10 … DL-DQ-13).

Specification pattern: each check is a composable predicate object producing exceptions, so
a new check is a new specification rather than a branch in an evaluator. These are
batch-level checks over a columnar pass, distinct from the per-record checks the existing
`QualityPolicyEvaluator` already performs — extended, not replaced.
"""

from __future__ import annotations

import abc
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any, Final

from contracts.platform_metrics import PlatformMetric
from data_quality.exception_repository import (
    ExceptionKind,
    ExceptionSeverity,
    QualityException,
)
from observability.metric_recorder import record_platform_metric

DEFAULT_FUTURE_TOLERANCE_DAYS: Final[int] = 1

DEFAULT_EPOCH: Final[date] = date(1990, 1, 1)


_OUTCOME_METRICS: Final[dict[str, PlatformMetric]] = {
    "completeness": PlatformMetric.COMPLETENESS_RATE,
    "duplicate-rate": PlatformMetric.DUPLICATE_RATE,
    "referential-integrity": PlatformMetric.ORPHAN_RATE,
}


@dataclass(frozen=True)
class BatchCheckContext:
    """The run identity every produced exception inherits."""

    tenant_code: str
    run_id: str
    entity_id: str
    correlation_id: str
    source_id: str = ""
    connection_id: str | None = None


@dataclass(frozen=True)
class BatchCheckOutcome:
    """The measured value plus any exceptions the check produced."""

    rule_id: str
    measured_value: float
    threshold: float
    passed: bool
    exceptions: tuple[QualityException, ...] = ()


class BatchQualitySpecification(abc.ABC):
    """One composable batch-level check."""

    rule_id: str
    severity: ExceptionSeverity

    @abc.abstractmethod
    def evaluate(
        self, records: Sequence[dict[str, Any]], context: BatchCheckContext
    ) -> BatchCheckOutcome:
        raise NotImplementedError

    def _exception(
        self,
        context: BatchCheckContext,
        kind: ExceptionKind,
        message: str,
        *,
        observed: str,
        expected: str,
        samples: tuple[str, ...] = (),
        key_field_name: str = "",
    ) -> QualityException:
        return QualityException(
            tenant_code=context.tenant_code,
            run_id=context.run_id,
            rule_id=self.rule_id,
            entity_id=context.entity_id,
            source_id=context.source_id,
            connection_id=context.connection_id,
            kind=kind,
            severity=self.severity,
            message=message,
            observed_value=observed,
            expected_value=expected,
            sample_keys=samples,
            key_field_name=key_field_name,
            correlation_id=context.correlation_id,
        )


@dataclass
class CompletenessCheck(BatchQualitySpecification):
    """Required-field population rate against a threshold (DL-DQ-10)."""

    required_fields: tuple[str, ...]
    minimum_population_rate_pct: float = 95.0
    severity: ExceptionSeverity = ExceptionSeverity.WARN
    rule_id: str = "completeness"

    def evaluate(
        self, records: Sequence[dict[str, Any]], context: BatchCheckContext
    ) -> BatchCheckOutcome:
        if not records or not self.required_fields:
            return BatchCheckOutcome(self.rule_id, 100.0, self.minimum_population_rate_pct, True)
        exceptions: list[QualityException] = []
        rates: list[float] = []
        for field_name in self.required_fields:
            populated = sum(1 for record in records if record.get(field_name) not in (None, ""))
            rate = 100.0 * populated / len(records)
            rates.append(rate)
            if rate < self.minimum_population_rate_pct:
                exceptions.append(
                    self._exception(
                        context,
                        ExceptionKind.COMPLETENESS_BELOW_THRESHOLD,
                        f"Field {field_name!r} is populated in {rate:.1f}% of records, below "
                        f"the {self.minimum_population_rate_pct:.1f}% threshold.",
                        observed=f"{rate:.2f}",
                        expected=f">={self.minimum_population_rate_pct:.2f}",
                        key_field_name=field_name,
                    )
                )
        worst = min(rates) if rates else 100.0
        return BatchCheckOutcome(
            rule_id=self.rule_id,
            measured_value=worst,
            threshold=self.minimum_population_rate_pct,
            passed=not exceptions,
            exceptions=tuple(exceptions),
        )


@dataclass
class DuplicateCheck(BatchQualitySpecification):
    """
    Intra-source duplicate rate on the declared natural key (DL-DQ-11).

    Deliberately distinct from entity resolution: this reports duplicates *within* one
    source before resolution runs, which is a source-quality signal, whereas resolution
    merges across sources by design.
    """

    natural_key_fields: tuple[str, ...]
    maximum_duplicate_rate_pct: float = 0.5
    severity: ExceptionSeverity = ExceptionSeverity.WARN
    rule_id: str = "duplicate-rate"

    def evaluate(
        self, records: Sequence[dict[str, Any]], context: BatchCheckContext
    ) -> BatchCheckOutcome:
        if not records or not self.natural_key_fields:
            return BatchCheckOutcome(self.rule_id, 0.0, self.maximum_duplicate_rate_pct, True)
        seen: dict[tuple[Any, ...], int] = {}
        for record in records:
            key = tuple(record.get(f) for f in self.natural_key_fields)
            seen[key] = seen.get(key, 0) + 1
        duplicate_keys = [key for key, count in seen.items() if count > 1]
        duplicate_rows = sum(seen[key] - 1 for key in duplicate_keys)
        rate = 100.0 * duplicate_rows / len(records)
        exceptions: tuple[QualityException, ...] = ()
        if rate > self.maximum_duplicate_rate_pct:
            exceptions = (
                self._exception(
                    context,
                    ExceptionKind.DUPLICATE_RATE_EXCEEDED,
                    f"{duplicate_rows} duplicate row(s) on natural key "
                    f"{list(self.natural_key_fields)} ({rate:.2f}%).",
                    observed=f"{rate:.2f}",
                    expected=f"<={self.maximum_duplicate_rate_pct:.2f}",
                    samples=tuple("|".join(str(part) for part in key) for key in duplicate_keys),
                    key_field_name=self.natural_key_fields[0],
                ),
            )
        return BatchCheckOutcome(
            rule_id=self.rule_id,
            measured_value=rate,
            threshold=self.maximum_duplicate_rate_pct,
            passed=not exceptions,
            exceptions=exceptions,
        )


@dataclass(frozen=True)
class ForeignKeyDeclaration:
    """A declared relationship between two entity types."""

    child_field: str
    parent_entity_type: str
    parent_key_field: str


@dataclass
class ReferentialIntegrityCheck(BatchQualitySpecification):
    """Orphan rate against a declared parent key set, after resolution (DL-DQ-12)."""

    declaration: ForeignKeyDeclaration
    parent_keys: frozenset[str]
    maximum_orphan_rate_pct: float = 1.0
    severity: ExceptionSeverity = ExceptionSeverity.WARN
    rule_id: str = "referential-integrity"

    def evaluate(
        self, records: Sequence[dict[str, Any]], context: BatchCheckContext
    ) -> BatchCheckOutcome:
        if not records:
            return BatchCheckOutcome(self.rule_id, 0.0, self.maximum_orphan_rate_pct, True)
        orphans = [
            str(record[self.declaration.child_field])
            for record in records
            if record.get(self.declaration.child_field) not in (None, "")
            and str(record[self.declaration.child_field]) not in self.parent_keys
        ]
        rate = 100.0 * len(orphans) / len(records)
        exceptions: tuple[QualityException, ...] = ()
        if rate > self.maximum_orphan_rate_pct:
            exceptions = (
                self._exception(
                    context,
                    ExceptionKind.REFERENTIAL_ORPHAN,
                    f"{len(orphans)} row(s) reference a missing "
                    f"{self.declaration.parent_entity_type} ({rate:.2f}% orphan rate).",
                    observed=f"{rate:.2f}",
                    expected=f"<={self.maximum_orphan_rate_pct:.2f}",
                    samples=tuple(orphans),
                    key_field_name=self.declaration.child_field,
                ),
            )
        return BatchCheckOutcome(
            rule_id=self.rule_id,
            measured_value=rate,
            threshold=self.maximum_orphan_rate_pct,
            passed=not exceptions,
            exceptions=exceptions,
        )


@dataclass
class DateValidationCheck(BatchQualitySpecification):
    """Future-dated, pre-epoch, and period-boundary anomalies (DL-DQ-13)."""

    date_fields: tuple[str, ...]
    epoch: date = DEFAULT_EPOCH
    future_tolerance_days: int = DEFAULT_FUTURE_TOLERANCE_DAYS
    maximum_anomaly_rate_pct: float = 0.5
    severity: ExceptionSeverity = ExceptionSeverity.WARN
    rule_id: str = "date-validation"
    _today: date | None = field(default=None, repr=False)

    def evaluate(
        self, records: Sequence[dict[str, Any]], context: BatchCheckContext
    ) -> BatchCheckOutcome:
        if not records or not self.date_fields:
            return BatchCheckOutcome(self.rule_id, 0.0, self.maximum_anomaly_rate_pct, True)
        today = self._today or datetime.now(UTC).date()
        anomalies: list[str] = []
        for index, record in enumerate(records):
            for field_name in self.date_fields:
                parsed = _parse_date(record.get(field_name))
                if parsed is None:
                    continue
                if (parsed - today).days > self.future_tolerance_days:
                    anomalies.append(f"row{index}:{field_name}:future:{parsed.isoformat()}")
                elif parsed < self.epoch:
                    anomalies.append(f"row{index}:{field_name}:pre-epoch:{parsed.isoformat()}")
        rate = 100.0 * len(anomalies) / len(records)
        exceptions: tuple[QualityException, ...] = ()
        if rate > self.maximum_anomaly_rate_pct:
            exceptions = (
                self._exception(
                    context,
                    ExceptionKind.DATE_VALIDATION,
                    f"{len(anomalies)} date anomaly/anomalies across "
                    f"{list(self.date_fields)} ({rate:.2f}%).",
                    observed=f"{rate:.2f}",
                    expected=f"<={self.maximum_anomaly_rate_pct:.2f}",
                    samples=tuple(anomalies),
                    key_field_name=self.date_fields[0],
                ),
            )
        return BatchCheckOutcome(
            rule_id=self.rule_id,
            measured_value=rate,
            threshold=self.maximum_anomaly_rate_pct,
            passed=not exceptions,
            exceptions=exceptions,
        )


def _parse_date(value: Any) -> date | None:
    """Parse a source date value; anything unparseable is a type problem, not a date one."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for candidate in (text, text.replace("Z", "+00:00")):
        try:
            return datetime.fromisoformat(candidate).date()
        except ValueError:
            continue
    return None


@dataclass
class BatchQualityResult:
    """Aggregate of every batch check for one run."""

    outcomes: tuple[BatchCheckOutcome, ...]

    @property
    def exceptions(self) -> tuple[QualityException, ...]:
        return tuple(e for outcome in self.outcomes for e in outcome.exceptions)

    @property
    def all_passed(self) -> bool:
        return all(outcome.passed for outcome in self.outcomes)

    def measured(self, rule_id: str) -> float | None:
        for outcome in self.outcomes:
            if outcome.rule_id == rule_id:
                return outcome.measured_value
        return None


def evaluate_batch_checks(
    checks: Sequence[BatchQualitySpecification],
    records: Sequence[dict[str, Any]],
    context: BatchCheckContext,
) -> BatchQualityResult:
    """Run every declared batch check over one record set, recording each measured value."""
    result = BatchQualityResult(tuple(check.evaluate(records, context) for check in checks))
    for outcome in result.outcomes:
        metric = _OUTCOME_METRICS.get(outcome.rule_id)
        if metric is not None:
            record_platform_metric(metric, outcome.measured_value, EntityId=context.entity_id)
    if result.exceptions:
        record_platform_metric(
            PlatformMetric.QUALITY_VIOLATIONS,
            len(result.exceptions),
            EntityId=context.entity_id,
        )
    return result
