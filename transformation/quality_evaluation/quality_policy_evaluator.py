"""
Data quality policy evaluator.

Quality policies define checks that run against canonical (post-mapping) records
before publication to the curated layer.  Each check has a severity:
  WARNING  — publication continues; violation logged and metered
  BLOCKING — publication halted; downstream transformation paused

Check types:
  null_check      — field value must not be null/empty
  range_check     — numeric value must be within [min, max] bounds
  pattern_check   — string value must match a compiled regex
  allowed_values  — value must be in a predefined set

Security (OWASP A03, A05):
  - Pattern expressions loaded from policy config, not runtime user input.
  - Regex compiled at policy construction time via re.compile (not at eval time).
  - Record payloads never included in log emissions (PII protection).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)


class QualityCheckSeverity(StrEnum):
    WARNING = "warning"
    BLOCKING = "blocking"


class QualityCheckKind(StrEnum):
    NULL_CHECK = "null_check"
    RANGE_CHECK = "range_check"
    PATTERN_CHECK = "pattern_check"
    ALLOWED_VALUES = "allowed_values"


@dataclass(frozen=True)
class QualityCheckViolation:
    """Single policy violation on a specific record."""

    field_name: str
    check_kind: QualityCheckKind
    severity: QualityCheckSeverity
    record_index: int
    detail: str


@dataclass(frozen=True)
class QualityReport:
    """Summary of quality evaluation results for one pipeline run."""

    source_id: str
    entity_id: str
    run_id: str
    total_records: int
    records_passed: int
    records_with_warnings: int
    records_blocked: int
    violations: tuple[QualityCheckViolation, ...]
    is_publication_blocked: bool


# ---------------------------------------------------------------------------
# Check definitions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NullCheck:
    """Field value must not be null or empty string."""

    field_name: str
    severity: QualityCheckSeverity
    kind: QualityCheckKind = field(default=QualityCheckKind.NULL_CHECK, init=False)


@dataclass(frozen=True)
class RangeCheck:
    """Numeric field must be within [min_value, max_value]."""

    field_name: str
    severity: QualityCheckSeverity
    min_value: float | None = None
    max_value: float | None = None
    kind: QualityCheckKind = field(default=QualityCheckKind.RANGE_CHECK, init=False)


@dataclass(frozen=True)
class PatternCheck:
    """String field must match a regex pattern."""

    field_name: str
    severity: QualityCheckSeverity
    pattern: str
    kind: QualityCheckKind = field(default=QualityCheckKind.PATTERN_CHECK, init=False)

    def compiled_pattern(self) -> re.Pattern[str]:
        return re.compile(self.pattern)


@dataclass(frozen=True)
class AllowedValuesCheck:
    """Field value must be a member of an allowed set."""

    field_name: str
    severity: QualityCheckSeverity
    allowed: frozenset[str]
    kind: QualityCheckKind = field(default=QualityCheckKind.ALLOWED_VALUES, init=False)


type QualityCheck = NullCheck | RangeCheck | PatternCheck | AllowedValuesCheck


@dataclass(frozen=True)
class QualityPolicy:
    """Versioned set of quality checks for a source+entity combination."""

    source_id: str
    entity_id: str
    policy_version: str
    checks: tuple[QualityCheck, ...]


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


class QualityPolicyEvaluator:
    """
    Evaluates a sequence of canonical records against a QualityPolicy.

    Produces a QualityReport indicating pass/warning/blocked status.
    Record payloads are never logged.
    """

    def evaluate(
        self,
        records: list[dict[str, Any]],
        policy: QualityPolicy,
        run_id: str,
    ) -> QualityReport:
        violations: list[QualityCheckViolation] = []
        blocked_indices: set[int] = set()
        warned_indices: set[int] = set()

        # Pre-compile all PatternCheck regexes once before the record loop (F-06).
        # re.compile() per-record for large datasets is O(n * pattern_count) overhead.
        compiled_patterns: dict[str, re.Pattern[str]] = {
            check.pattern: re.compile(check.pattern)
            for check in policy.checks
            if isinstance(check, PatternCheck)
        }

        for idx, record in enumerate(records):
            for check in policy.checks:
                violation = self._evaluate_check(record, check, idx, compiled_patterns)
                if violation is not None:
                    violations.append(violation)
                    if violation.severity == QualityCheckSeverity.BLOCKING:
                        blocked_indices.add(idx)
                    else:
                        warned_indices.add(idx)

        is_blocked = len(blocked_indices) > 0

        if is_blocked:
            _logger.warning(
                "quality_policy_blocking_violation",
                source_id=policy.source_id,
                entity_id=policy.entity_id,
                run_id=run_id,
                blocked_record_count=len(blocked_indices),
                total_violation_count=len(violations),
            )
        elif violations:
            _logger.info(
                "quality_policy_warnings",
                source_id=policy.source_id,
                entity_id=policy.entity_id,
                run_id=run_id,
                warning_count=len(violations),
            )

        pure_warned = warned_indices - blocked_indices
        pure_pass = len(records) - len(blocked_indices | pure_warned)

        return QualityReport(
            source_id=policy.source_id,
            entity_id=policy.entity_id,
            run_id=run_id,
            total_records=len(records),
            records_passed=pure_pass,
            records_with_warnings=len(pure_warned),
            records_blocked=len(blocked_indices),
            violations=tuple(violations),
            is_publication_blocked=is_blocked,
        )

    def _evaluate_check(
        self,
        record: dict[str, Any],
        check: QualityCheck,
        idx: int,
        compiled_patterns: dict[str, re.Pattern[str]] | None = None,
    ) -> QualityCheckViolation | None:
        if isinstance(check, NullCheck):
            return self._check_null(record, check, idx)
        if isinstance(check, RangeCheck):
            return self._check_range(record, check, idx)
        if isinstance(check, PatternCheck):
            return self._check_pattern(record, check, idx, compiled_patterns or {})
        if isinstance(check, AllowedValuesCheck):
            return self._check_allowed_values(record, check, idx)
        return None

    @staticmethod
    def _check_null(
        record: dict[str, Any], check: NullCheck, idx: int
    ) -> QualityCheckViolation | None:
        val = record.get(check.field_name)
        if val is None or (isinstance(val, str) and val.strip() == ""):
            return QualityCheckViolation(
                field_name=check.field_name,
                check_kind=QualityCheckKind.NULL_CHECK,
                severity=check.severity,
                record_index=idx,
                detail=f"Field '{check.field_name}' is null or empty",
            )
        return None

    @staticmethod
    def _check_range(
        record: dict[str, Any], check: RangeCheck, idx: int
    ) -> QualityCheckViolation | None:
        val = record.get(check.field_name)
        if val is None:
            return None
        try:
            numeric = float(val)
        except TypeError, ValueError:
            return QualityCheckViolation(
                field_name=check.field_name,
                check_kind=QualityCheckKind.RANGE_CHECK,
                severity=check.severity,
                record_index=idx,
                detail=f"Field '{check.field_name}' is not numeric",
            )
        if check.min_value is not None and numeric < check.min_value:
            return QualityCheckViolation(
                field_name=check.field_name,
                check_kind=QualityCheckKind.RANGE_CHECK,
                severity=check.severity,
                record_index=idx,
                detail=f"Value {numeric} < min={check.min_value}",
            )
        if check.max_value is not None and numeric > check.max_value:
            return QualityCheckViolation(
                field_name=check.field_name,
                check_kind=QualityCheckKind.RANGE_CHECK,
                severity=check.severity,
                record_index=idx,
                detail=f"Value {numeric} > max={check.max_value}",
            )
        return None

    @staticmethod
    def _check_pattern(
        record: dict[str, Any],
        check: PatternCheck,
        idx: int,
        compiled_patterns: dict[str, re.Pattern[str]] | None = None,
    ) -> QualityCheckViolation | None:
        val = record.get(check.field_name)
        if val is None:
            return None
        # Use pre-compiled pattern when available (avoids re.compile per record).
        compiled = (compiled_patterns or {}).get(check.pattern) or re.compile(check.pattern)
        if not compiled.match(str(val)):
            return QualityCheckViolation(
                field_name=check.field_name,
                check_kind=QualityCheckKind.PATTERN_CHECK,
                severity=check.severity,
                record_index=idx,
                detail=f"Field '{check.field_name}' does not match pattern",
            )
        return None

    @staticmethod
    def _check_allowed_values(
        record: dict[str, Any], check: AllowedValuesCheck, idx: int
    ) -> QualityCheckViolation | None:
        val = record.get(check.field_name)
        if val is None:
            return None
        if str(val) not in check.allowed:
            return QualityCheckViolation(
                field_name=check.field_name,
                check_kind=QualityCheckKind.ALLOWED_VALUES,
                severity=check.severity,
                record_index=idx,
                detail=f"Field '{check.field_name}' value not in allowed set",
            )
        return None
