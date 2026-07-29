"""
Reconciliation to source and cross-layer (DL-DQ-02, DL-DQ-03, DL-DQ-04).

Comparator strategies — count, monetary sum, min/max watermark, deterministic sampled field
compare — producing a signed `EdlReconciliationReport`. Financial entities reconcile on
monetary sums, not only counts (§3.6), and the measure definition comes from the semantic
layer so "revenue" reconciles against the same definition the dashboards show.

An undetected revenue mismatch is the highest-consequence failure mode in this platform, so
a variance beyond tolerance is a paging signal, not an informational one.
"""

from __future__ import annotations

import abc
import hashlib
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Final

import boto3

from contracts.identifier_policy import validate_tenant_code
from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import record_platform_metric
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

_TABLE_NAME: Final[str] = "EdlReconciliationReport"

# Default tolerances. Counts may drift slightly between layers on a live source; monetary
# sums must not — a cent of unexplained variance in revenue is a real finding.
DEFAULT_COUNT_TOLERANCE_PCT: Final[float] = 0.1
DEFAULT_SUM_TOLERANCE_PCT: Final[float] = 0.0

# Deterministic sampling divisor; the same rows are sampled across runs so drift is visible.
DEFAULT_SAMPLE_MODULO: Final[int] = 100


class ComparatorKind(StrEnum):
    """The reconciliation comparators."""

    COUNT = "count"
    MONETARY_SUM = "monetary_sum"
    WATERMARK_BOUNDS = "watermark_bounds"
    SAMPLED_FIELD = "sampled_field"


class ReconciliationVerdict(StrEnum):
    """Outcome of one comparison."""

    MATCHED = "matched"
    WITHIN_TOLERANCE = "within_tolerance"
    VARIANCE = "variance"
    NOT_COMPARABLE = "not_comparable"


class DataLayerName(StrEnum):
    """Layers a reconciliation compares."""

    SOURCE = "source"
    RAW = "raw"
    CURATED = "curated"
    GOLDEN = "golden"
    ANALYTICS = "analytics"


@dataclass(frozen=True)
class LayerMeasurement:
    """One layer's measured value for one comparator."""

    layer: DataLayerName
    value: str
    row_count: int = 0

    def as_decimal(self) -> Decimal | None:
        try:
            return Decimal(self.value)
        except (InvalidOperation, ValueError):
            return None


@dataclass(frozen=True)
class ComparisonResult:
    """One comparator's outcome across two layers."""

    comparator: ComparatorKind
    measure_name: str
    expected: LayerMeasurement
    observed: LayerMeasurement
    variance_pct: float
    tolerance_pct: float
    verdict: ReconciliationVerdict
    detail: str = ""

    @property
    def is_finding(self) -> bool:
        return self.verdict is ReconciliationVerdict.VARIANCE


class ReconciliationComparator(abc.ABC):
    """Strategy port for one kind of comparison."""

    kind: ComparatorKind

    @abc.abstractmethod
    def compare(
        self, expected: LayerMeasurement, observed: LayerMeasurement, measure_name: str
    ) -> ComparisonResult:
        raise NotImplementedError


def _variance_pct(expected: Decimal, observed: Decimal) -> float:
    if expected == 0:
        return 0.0 if observed == 0 else 100.0
    return float(abs(observed - expected) / abs(expected) * 100)


@dataclass
class CountComparator(ReconciliationComparator):
    """Record-count validation per chunk or period (DL-DQ-02)."""

    tolerance_pct: float = DEFAULT_COUNT_TOLERANCE_PCT
    kind: ComparatorKind = field(default=ComparatorKind.COUNT, init=False)

    def compare(
        self, expected: LayerMeasurement, observed: LayerMeasurement, measure_name: str
    ) -> ComparisonResult:
        expected_value = expected.as_decimal()
        observed_value = observed.as_decimal()
        if expected_value is None or observed_value is None:
            return _not_comparable(self.kind, measure_name, expected, observed, self.tolerance_pct)
        variance = _variance_pct(expected_value, observed_value)
        return ComparisonResult(
            comparator=self.kind,
            measure_name=measure_name,
            expected=expected,
            observed=observed,
            variance_pct=variance,
            tolerance_pct=self.tolerance_pct,
            verdict=_verdict(variance, self.tolerance_pct),
        )


@dataclass
class MonetarySumComparator(ReconciliationComparator):
    """
    Monetary-sum reconciliation for financial entities (DL-DQ-04, §3.6).

    Decimal arithmetic throughout: float summation of currency reintroduces the rounding
    error the reconciliation exists to detect.
    """

    tolerance_pct: float = DEFAULT_SUM_TOLERANCE_PCT
    kind: ComparatorKind = field(default=ComparatorKind.MONETARY_SUM, init=False)

    def compare(
        self, expected: LayerMeasurement, observed: LayerMeasurement, measure_name: str
    ) -> ComparisonResult:
        expected_value = expected.as_decimal()
        observed_value = observed.as_decimal()
        if expected_value is None or observed_value is None:
            return _not_comparable(self.kind, measure_name, expected, observed, self.tolerance_pct)
        variance = _variance_pct(expected_value, observed_value)
        difference = observed_value - expected_value
        return ComparisonResult(
            comparator=self.kind,
            measure_name=measure_name,
            expected=expected,
            observed=observed,
            variance_pct=variance,
            tolerance_pct=self.tolerance_pct,
            verdict=_verdict(variance, self.tolerance_pct),
            detail=f"difference={difference}",
        )


@dataclass
class WatermarkBoundsComparator(ReconciliationComparator):
    """Min/max of the watermark field, so a truncated period is visible."""

    kind: ComparatorKind = field(default=ComparatorKind.WATERMARK_BOUNDS, init=False)
    tolerance_pct: float = 0.0

    def compare(
        self, expected: LayerMeasurement, observed: LayerMeasurement, measure_name: str
    ) -> ComparisonResult:
        matched = expected.value == observed.value
        return ComparisonResult(
            comparator=self.kind,
            measure_name=measure_name,
            expected=expected,
            observed=observed,
            variance_pct=0.0 if matched else 100.0,
            tolerance_pct=self.tolerance_pct,
            verdict=ReconciliationVerdict.MATCHED if matched else ReconciliationVerdict.VARIANCE,
        )


def _verdict(variance_pct: float, tolerance_pct: float) -> ReconciliationVerdict:
    if variance_pct == 0.0:
        return ReconciliationVerdict.MATCHED
    if variance_pct <= tolerance_pct:
        return ReconciliationVerdict.WITHIN_TOLERANCE
    return ReconciliationVerdict.VARIANCE


def _not_comparable(
    kind: ComparatorKind,
    measure_name: str,
    expected: LayerMeasurement,
    observed: LayerMeasurement,
    tolerance_pct: float,
) -> ComparisonResult:
    return ComparisonResult(
        comparator=kind,
        measure_name=measure_name,
        expected=expected,
        observed=observed,
        variance_pct=0.0,
        tolerance_pct=tolerance_pct,
        verdict=ReconciliationVerdict.NOT_COMPARABLE,
        detail="one or both measurements are not numeric",
    )


# ---------------------------------------------------------------------------
# Sampled field comparison (DL-DQ-03)
# ---------------------------------------------------------------------------


def deterministic_sample(
    records: Sequence[dict[str, Any]], natural_key_field: str, modulo: int = DEFAULT_SAMPLE_MODULO
) -> list[dict[str, Any]]:
    """
    Hash-of-key modulo N sampling, so successive runs sample the same rows.

    Random sampling would make field-level drift undetectable: a difference could always be
    explained as "a different sample this time".
    """
    if modulo < 1:
        raise ValueError("modulo must be at least 1.")
    sampled: list[dict[str, Any]] = []
    for record in records:
        key = str(record.get(natural_key_field, ""))
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        if int(digest[:8], 16) % modulo == 0:
            sampled.append(record)
    return sampled


@dataclass(frozen=True)
class FieldMatchRate:
    """Per-field match rate from a sampled comparison."""

    field_name: str
    compared: int
    matched: int

    @property
    def match_rate_pct(self) -> float:
        return 100.0 if self.compared == 0 else 100.0 * self.matched / self.compared


def compare_key_fields(
    source_records: Sequence[dict[str, Any]],
    curated_records: Sequence[dict[str, Any]],
    natural_key_field: str,
    key_fields: Sequence[str],
) -> list[FieldMatchRate]:
    """Field-by-field comparison over the intersection of the two record sets (DL-DQ-03)."""
    curated_by_key = {str(r.get(natural_key_field, "")): r for r in curated_records}
    rates: list[FieldMatchRate] = []
    for field_name in key_fields:
        compared = 0
        matched = 0
        for source_record in source_records:
            key = str(source_record.get(natural_key_field, ""))
            curated_record = curated_by_key.get(key)
            if curated_record is None:
                continue
            compared += 1
            if _normalise(source_record.get(field_name)) == _normalise(
                curated_record.get(field_name)
            ):
                matched += 1
        rates.append(FieldMatchRate(field_name=field_name, compared=compared, matched=matched))
    return rates


def _normalise(value: Any) -> str:
    """Compare source and curated values as trimmed strings; typing is a curated concern."""
    if value is None:
        return ""
    return str(value).strip()


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


@dataclass
class ReconciliationReport:
    """Signed, immutable reconciliation verdict for one entity and period."""

    tenant_code: str
    entity_id: str
    period: str
    run_id: str
    comparisons: tuple[ComparisonResult, ...]
    field_match_rates: tuple[FieldMatchRate, ...] = ()
    rule_version: str = "1.0.0"
    generated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def __post_init__(self) -> None:
        validate_tenant_code(self.tenant_code)

    @property
    def sort_key(self) -> str:
        return f"{self.entity_id}#{self.period}#{self.run_id}"

    @property
    def findings(self) -> tuple[ComparisonResult, ...]:
        return tuple(c for c in self.comparisons if c.is_finding)

    @property
    def matched(self) -> bool:
        return not self.findings

    @property
    def worst_variance_pct(self) -> float:
        return max((c.variance_pct for c in self.comparisons), default=0.0)

    def signature(self) -> str:
        """
        Content hash making the verdict tamper-evident.

        OWASP A08/A09: the report is an immutable audit record, so a changed figure must be
        detectable rather than merely unlikely.
        """
        payload = {
            "tenant_code": self.tenant_code,
            "entity_id": self.entity_id,
            "period": self.period,
            "run_id": self.run_id,
            "rule_version": self.rule_version,
            "comparisons": [
                {
                    "comparator": c.comparator.value,
                    "measure": c.measure_name,
                    "expected": c.expected.value,
                    "observed": c.observed.value,
                    "verdict": c.verdict.value,
                }
                for c in self.comparisons
            ],
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()


class ReconciliationReportRepository:
    """Persists reconciliation reports; one immutable audit record per verdict."""

    def __init__(self, environment: str, region_name: str) -> None:
        if not environment:
            raise ValueError("environment must not be empty.")
        self._environment = environment
        table_name = os.environ.get("RECONCILIATION_REPORT_TABLE") or _TABLE_NAME
        self._table = boto3.resource("dynamodb", region_name=region_name).Table(table_name)

    def save(self, report: ReconciliationReport) -> str:
        signature = report.signature()
        self._table.put_item(
            Item={
                "tenant_code": report.tenant_code,
                "report_key": report.sort_key,
                "entity_id": report.entity_id,
                "period": report.period,
                "run_id": report.run_id,
                "rule_version": report.rule_version,
                "matched": report.matched,
                "worst_variance_pct": json.dumps(round(report.worst_variance_pct, 6)),
                "comparisons": [
                    {
                        "comparator": c.comparator.value,
                        "measure_name": c.measure_name,
                        "expected_layer": c.expected.layer.value,
                        "expected_value": c.expected.value,
                        "observed_layer": c.observed.layer.value,
                        "observed_value": c.observed.value,
                        "variance_pct": json.dumps(round(c.variance_pct, 6)),
                        "tolerance_pct": json.dumps(c.tolerance_pct),
                        "verdict": c.verdict.value,
                        "detail": c.detail,
                    }
                    for c in report.comparisons
                ],
                "field_match_rates": [
                    {
                        "field_name": r.field_name,
                        "compared": r.compared,
                        "matched": r.matched,
                        "match_rate_pct": json.dumps(round(r.match_rate_pct, 4)),
                    }
                    for r in report.field_match_rates
                ],
                "signature": signature,
                "generated_at": report.generated_at,
                "environment": self._environment,
            }
        )
        record_platform_metric(
            PlatformMetric.RECONCILIATION_VARIANCE_PCT,
            report.worst_variance_pct,
            EntityId=report.entity_id,
        )
        if not report.matched:
            record_platform_metric(
                PlatformMetric.RECONCILIATION_FAILURES,
                len(report.findings),
                EntityId=report.entity_id,
            )
        _logger.info(
            "reconciliation_report_saved",
            tenant_code=report.tenant_code,
            entity_id=report.entity_id,
            period=report.period,
            matched=report.matched,
            worst_variance_pct=round(report.worst_variance_pct, 4),
        )
        return signature

    def list_for_entity(self, tenant_code: str, entity_id: str) -> list[dict[str, Any]]:
        tenant_code = validate_tenant_code(tenant_code)
        response = self._table.query(
            KeyConditionExpression="tenant_code = :tc AND begins_with(report_key, :entity)",
            ExpressionAttributeValues={":tc": tenant_code, ":entity": f"{entity_id}#"},
        )
        return [dict(item) for item in response.get("Items", [])]


def reconcile(
    tenant_code: str,
    entity_id: str,
    period: str,
    run_id: str,
    measurements: dict[ComparatorKind, tuple[LayerMeasurement, LayerMeasurement, str]],
    *,
    count_tolerance_pct: float = DEFAULT_COUNT_TOLERANCE_PCT,
    sum_tolerance_pct: float = DEFAULT_SUM_TOLERANCE_PCT,
    field_match_rates: tuple[FieldMatchRate, ...] = (),
) -> ReconciliationReport:
    """Run every supplied comparator and assemble the report."""
    comparators: dict[ComparatorKind, ReconciliationComparator] = {
        ComparatorKind.COUNT: CountComparator(tolerance_pct=count_tolerance_pct),
        ComparatorKind.MONETARY_SUM: MonetarySumComparator(tolerance_pct=sum_tolerance_pct),
        ComparatorKind.WATERMARK_BOUNDS: WatermarkBoundsComparator(),
    }
    comparisons: list[ComparisonResult] = []
    for kind, (expected, observed, measure_name) in measurements.items():
        comparator = comparators.get(kind)
        if comparator is None:
            continue
        comparisons.append(comparator.compare(expected, observed, measure_name))
    return ReconciliationReport(
        tenant_code=tenant_code,
        entity_id=entity_id,
        period=period,
        run_id=run_id,
        comparisons=tuple(comparisons),
        field_match_rates=field_match_rates,
    )
