"""Tests for QualityPolicyEvaluator — Phase 6."""

from __future__ import annotations

from transformation.quality_evaluation.quality_policy_evaluator import (
    AllowedValuesCheck,
    NullCheck,
    PatternCheck,
    QualityCheckSeverity,
    QualityPolicy,
    QualityPolicyEvaluator,
    RangeCheck,
)

_SOURCE_ID = "salesforce"
_ENTITY_ID = "salesforce-account"
_RUN_ID = "run-test-001"


def _policy(*checks, version="1.0.0"):
    return QualityPolicy(
        source_id=_SOURCE_ID,
        entity_id=_ENTITY_ID,
        policy_version=version,
        checks=tuple(checks),
    )


class TestNullCheck:
    def setup_method(self, method=None):
        self.evaluator = QualityPolicyEvaluator()

    def test_null_field_blocking_violation(self):
        policy = _policy(NullCheck("name", QualityCheckSeverity.BLOCKING))
        report = self.evaluator.evaluate([{"name": None}], policy, _RUN_ID)
        assert report.is_publication_blocked is True
        assert report.records_blocked == 1
        assert len(report.violations) == 1

    def test_null_field_warning_does_not_block(self):
        policy = _policy(NullCheck("name", QualityCheckSeverity.WARNING))
        report = self.evaluator.evaluate([{"name": None}], policy, _RUN_ID)
        assert report.is_publication_blocked is False
        assert report.records_with_warnings == 1

    def test_empty_string_counts_as_null(self):
        policy = _policy(NullCheck("name", QualityCheckSeverity.BLOCKING))
        report = self.evaluator.evaluate([{"name": "  "}], policy, _RUN_ID)
        assert report.is_publication_blocked is True

    def test_non_null_field_passes(self):
        policy = _policy(NullCheck("name", QualityCheckSeverity.BLOCKING))
        report = self.evaluator.evaluate([{"name": "Acme Corp"}], policy, _RUN_ID)
        assert report.is_publication_blocked is False
        assert report.records_passed == 1
        assert len(report.violations) == 0

    def test_absent_field_treated_as_null(self):
        policy = _policy(NullCheck("name", QualityCheckSeverity.BLOCKING))
        report = self.evaluator.evaluate([{"other": "x"}], policy, _RUN_ID)
        assert report.is_publication_blocked is True


class TestRangeCheck:
    def setup_method(self, method=None):
        self.evaluator = QualityPolicyEvaluator()

    def test_value_below_min_is_violation(self):
        policy = _policy(RangeCheck("age", QualityCheckSeverity.BLOCKING, min_value=0))
        report = self.evaluator.evaluate([{"age": -1}], policy, _RUN_ID)
        assert report.is_publication_blocked is True

    def test_value_above_max_is_violation(self):
        policy = _policy(RangeCheck("score", QualityCheckSeverity.WARNING, max_value=100))
        report = self.evaluator.evaluate([{"score": 150}], policy, _RUN_ID)
        assert report.records_with_warnings == 1

    def test_value_within_range_passes(self):
        policy = _policy(
            RangeCheck("score", QualityCheckSeverity.BLOCKING, min_value=0, max_value=100)
        )
        report = self.evaluator.evaluate([{"score": 50}], policy, _RUN_ID)
        assert report.is_publication_blocked is False

    def test_non_numeric_value_is_violation(self):
        policy = _policy(RangeCheck("count", QualityCheckSeverity.BLOCKING, min_value=0))
        report = self.evaluator.evaluate([{"count": "not-a-number"}], policy, _RUN_ID)
        assert report.is_publication_blocked is True

    def test_null_value_skipped(self):
        policy = _policy(RangeCheck("count", QualityCheckSeverity.BLOCKING, min_value=0))
        report = self.evaluator.evaluate([{"count": None}], policy, _RUN_ID)
        assert report.is_publication_blocked is False


class TestPatternCheck:
    def setup_method(self, method=None):
        self.evaluator = QualityPolicyEvaluator()

    def test_pattern_mismatch_is_violation(self):
        policy = _policy(
            PatternCheck("email", QualityCheckSeverity.BLOCKING, r"^[\w.]+@[\w.]+\.\w+$")
        )
        report = self.evaluator.evaluate([{"email": "not-an-email"}], policy, _RUN_ID)
        assert report.is_publication_blocked is True

    def test_pattern_match_passes(self):
        policy = _policy(
            PatternCheck("email", QualityCheckSeverity.BLOCKING, r"^[\w.]+@[\w.]+\.\w+$")
        )
        report = self.evaluator.evaluate([{"email": "user@example.com"}], policy, _RUN_ID)
        assert report.is_publication_blocked is False

    def test_null_value_skipped(self):
        policy = _policy(
            PatternCheck("email", QualityCheckSeverity.BLOCKING, r"^[\w.]+@[\w.]+\.\w+$")
        )
        report = self.evaluator.evaluate([{"email": None}], policy, _RUN_ID)
        assert report.is_publication_blocked is False


class TestAllowedValuesCheck:
    def setup_method(self, method=None):
        self.evaluator = QualityPolicyEvaluator()

    def test_value_not_in_allowed_set_is_violation(self):
        policy = _policy(
            AllowedValuesCheck(
                "status", QualityCheckSeverity.BLOCKING, frozenset({"active", "inactive"})
            )
        )
        report = self.evaluator.evaluate([{"status": "unknown"}], policy, _RUN_ID)
        assert report.is_publication_blocked is True

    def test_value_in_allowed_set_passes(self):
        policy = _policy(
            AllowedValuesCheck(
                "status", QualityCheckSeverity.BLOCKING, frozenset({"active", "inactive"})
            )
        )
        report = self.evaluator.evaluate([{"status": "active"}], policy, _RUN_ID)
        assert report.is_publication_blocked is False

    def test_null_value_skipped(self):
        policy = _policy(
            AllowedValuesCheck("status", QualityCheckSeverity.BLOCKING, frozenset({"active"}))
        )
        report = self.evaluator.evaluate([{"status": None}], policy, _RUN_ID)
        assert report.is_publication_blocked is False


class TestQualityReportMetrics:
    def setup_method(self, method=None):
        self.evaluator = QualityPolicyEvaluator()

    def test_multi_record_mixed_results(self):
        checks = [
            NullCheck("name", QualityCheckSeverity.BLOCKING),
            RangeCheck("score", QualityCheckSeverity.WARNING, min_value=0, max_value=100),
        ]
        policy = _policy(*checks)
        records = [
            {"name": "Alice", "score": 80},  # passes
            {"name": None, "score": 50},  # blocked (name)
            {"name": "Bob", "score": 150},  # warning (score)
        ]
        report = self.evaluator.evaluate(records, policy, _RUN_ID)
        assert report.total_records == 3
        assert report.records_blocked == 1
        assert report.records_with_warnings == 1
        assert report.records_passed == 1
        assert report.is_publication_blocked is True

    def test_empty_records_no_violations(self):
        policy = _policy(NullCheck("name", QualityCheckSeverity.BLOCKING))
        report = self.evaluator.evaluate([], policy, _RUN_ID)
        assert report.total_records == 0
        assert report.is_publication_blocked is False
