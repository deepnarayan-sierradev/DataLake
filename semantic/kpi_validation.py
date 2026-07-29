"""
KPI validation harness (DL-SEM-08).

For each KPI, a stored expected-value test against a known period, executed in CI against a
fixture dataset and post-deploy against real data as a smoke check. This is what makes §9
"KPI validation" a repeatable gate rather than a meeting.

The harness deliberately validates two different things: that the KPI *compiles* (a structural
check that runs with no data at all), and that it *computes the expected figure* (a value
check that needs a fixture or a real period). A green structural check with no value check is
reported as such rather than counted as a pass.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import record_platform_metric
from semantic.enterprise_model import SOW_KPI_MAP, expand_access_tags
from semantic.query_compiler import (
    QueryCompiler,
    SemanticQueryError,
    SemanticQueryRequest,
    TimeRangeFilter,
)
from semantic.semantic_model import SemanticModel, TimeGrain
from tenancy.scope_predicate import UnrestrictedScopeReason, unrestricted_predicate

QueryExecutor = Callable[[str, list[Any]], list[dict[str, Any]]]
"""(sql, parameters) -> rows. Supplied by CI (fixture) or the smoke suite (real engine)."""


class KpiCheckOutcome(StrEnum):
    """What one KPI expectation produced."""

    PASSED = "passed"
    FAILED = "failed"
    COMPILE_ONLY = "compile_only"
    COMPILE_FAILED = "compile_failed"
    NOT_EXECUTED = "not_executed"


@dataclass(frozen=True)
class KpiExpectation:
    """A stored expected value for one KPI over one known period."""

    kpi_name: str
    entity: str
    metric: str
    period_start: date
    period_end: date
    expected_value: str | None = None
    tolerance_pct: float = 0.0
    time_dimension: str | None = None
    time_grain: TimeGrain = TimeGrain.MONTH
    dimensions: tuple[str, ...] = ()
    required_access_tags: frozenset[str] = frozenset()

    def to_request(self) -> SemanticQueryRequest:
        return SemanticQueryRequest(
            entity=self.entity,
            metrics=(self.metric,),
            dimensions=self.dimensions,
            time_dimension=self.time_dimension,
            time_grain=self.time_grain if self.time_dimension else None,
            time_range=(
                TimeRangeFilter(
                    time_dimension=self.time_dimension,
                    start=self.period_start,
                    end=self.period_end,
                )
                if self.time_dimension
                else None
            ),
        )


@dataclass(frozen=True)
class KpiCheckResult:
    """The outcome of one expectation."""

    kpi_name: str
    outcome: KpiCheckOutcome
    expected_value: str | None = None
    observed_value: str | None = None
    variance_pct: float = 0.0
    detail: str = ""

    @property
    def is_failure(self) -> bool:
        return self.outcome in (KpiCheckOutcome.FAILED, KpiCheckOutcome.COMPILE_FAILED)


@dataclass
class KpiValidationReport:
    """Aggregate of every expectation in one harness run."""

    results: tuple[KpiCheckResult, ...] = field(default_factory=tuple)

    @property
    def failures(self) -> tuple[KpiCheckResult, ...]:
        return tuple(r for r in self.results if r.is_failure)

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def value_checked_count(self) -> int:
        return sum(1 for r in self.results if r.outcome is KpiCheckOutcome.PASSED)

    @property
    def compile_only_count(self) -> int:
        return sum(1 for r in self.results if r.outcome is KpiCheckOutcome.COMPILE_ONLY)

    def render_summary(self) -> str:
        lines = [
            f"KPI validation: {len(self.results)} expectation(s), "
            f"{self.value_checked_count} value-checked, "
            f"{self.compile_only_count} compile-only, {len(self.failures)} failed.",
        ]
        for result in self.results:
            marker = "FAIL" if result.is_failure else result.outcome.value.upper()
            lines.append(f"  [{marker}] {result.kpi_name}: {result.detail}".rstrip())
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(
            [
                {
                    "kpi": r.kpi_name,
                    "outcome": r.outcome.value,
                    "expected": r.expected_value,
                    "observed": r.observed_value,
                    "variance_pct": round(r.variance_pct, 6),
                    "detail": r.detail,
                }
                for r in self.results
            ],
            indent=2,
        )


class KpiValidationHarness:
    """Compiles and, where an executor is supplied, evaluates every stored expectation."""

    def __init__(
        self,
        model: SemanticModel,
        expectations: Sequence[KpiExpectation],
        executor: QueryExecutor | None = None,
    ) -> None:
        self._model = model
        self._compiler = QueryCompiler(model)
        self._expectations = list(expectations)
        self._executor = executor

    def run(self, *, granted_access_tags: frozenset[str]) -> KpiValidationReport:
        # Non-nullable: `None` and `frozenset()` meant the same thing here, so the union bought
        # nothing and left a guarded parameter looking omittable (G4).
        tags = expand_access_tags(granted_access_tags)
        results = [self._run_one(expectation, tags) for expectation in self._expectations]
        report = KpiValidationReport(results=tuple(results))
        record_platform_metric(PlatformMetric.KPI_VALIDATION_FAILURES, len(report.failures))
        return report

    def _run_one(self, expectation: KpiExpectation, granted_tags: frozenset[str]) -> KpiCheckResult:
        try:
            compiled = self._compiler.compile(
                expectation.to_request(),
                granted_access_tags=granted_tags | expectation.required_access_tags,
                # Validation compiles the definition, not a caller's request: there is no
                # end-user claim to scope by. Expressed as an affirmative, audited object rather
                # than `None`, so this decision is countable in `UnrestrictedScopeReads` instead
                # of being indistinguishable from a caller who forgot (DL-SCOPE-14).
                scope_predicate=unrestricted_predicate(
                    UnrestrictedScopeReason.DEFINITION_VALIDATION
                ),
            )
        except SemanticQueryError as exc:
            return KpiCheckResult(
                kpi_name=expectation.kpi_name,
                outcome=KpiCheckOutcome.COMPILE_FAILED,
                detail=f"failed to compile: {exc}",
            )
        if self._executor is None or expectation.expected_value is None:
            return KpiCheckResult(
                kpi_name=expectation.kpi_name,
                outcome=KpiCheckOutcome.COMPILE_ONLY,
                detail="compiled; no expected value or no executor supplied",
            )
        try:
            rows = self._executor(compiled.sql, compiled.parameters)
        except Exception as exc:
            return KpiCheckResult(
                kpi_name=expectation.kpi_name,
                outcome=KpiCheckOutcome.FAILED,
                expected_value=expectation.expected_value,
                detail=f"execution failed: {type(exc).__name__}: {exc}",
            )
        observed = _first_metric_value(rows, expectation.metric)
        return self._compare(expectation, observed)

    @staticmethod
    def _compare(expectation: KpiExpectation, observed: str | None) -> KpiCheckResult:
        expected_decimal = _to_decimal(expectation.expected_value)
        observed_decimal = _to_decimal(observed)
        if expected_decimal is None or observed_decimal is None:
            matched = str(observed) == str(expectation.expected_value)
            return KpiCheckResult(
                kpi_name=expectation.kpi_name,
                outcome=KpiCheckOutcome.PASSED if matched else KpiCheckOutcome.FAILED,
                expected_value=expectation.expected_value,
                observed_value=observed,
                detail="" if matched else "non-numeric value mismatch",
            )
        if expected_decimal == 0:
            variance = 0.0 if observed_decimal == 0 else 100.0
        else:
            variance = float(abs(observed_decimal - expected_decimal) / abs(expected_decimal) * 100)
        within = variance <= expectation.tolerance_pct
        return KpiCheckResult(
            kpi_name=expectation.kpi_name,
            outcome=KpiCheckOutcome.PASSED if within else KpiCheckOutcome.FAILED,
            expected_value=expectation.expected_value,
            observed_value=observed,
            variance_pct=variance,
            detail=""
            if within
            else f"variance {variance:.4f}% exceeds tolerance {expectation.tolerance_pct:.4f}%",
        )


def _first_metric_value(rows: list[dict[str, Any]], metric: str) -> str | None:
    if not rows:
        return None
    value = rows[0].get(metric)
    return None if value is None else str(value)


def _to_decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def structural_expectations(model: SemanticModel) -> list[KpiExpectation]:
    """
    One compile-only expectation per SOW §4 KPI.

    Runs with no data, so it is the CI gate that catches a model change breaking a named KPI
    before any fixture or deployment is involved.
    """
    expectations: list[KpiExpectation] = []
    for kpi_name, (entity_name, metric_name) in SOW_KPI_MAP.items():
        entity = model.entity(entity_name)
        metric = entity.metric(metric_name)
        time_dimension = entity.time_dimensions[0].name if entity.time_dimensions else None
        expectations.append(
            KpiExpectation(
                kpi_name=kpi_name,
                entity=entity_name,
                metric=metric_name,
                period_start=date(2026, 1, 1),
                period_end=date(2026, 1, 31),
                time_dimension=time_dimension,
                required_access_tags=(
                    frozenset({metric.access_tag}) if metric.access_tag else frozenset()
                ),
            )
        )
    return expectations
