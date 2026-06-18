"""
Tests for entity_resolution/matching_engine/match_evaluation.py.
"""

from __future__ import annotations

import pytest

from entity_resolution.matching_engine.match_evaluation import (
    MatchEvaluationReport,
    MatchEvaluator,
    MatchPairLabel,
    PairEvaluationDetail,
    _canonical_pair,
)
from entity_resolution.matching_engine.match_rule_engine import MatchDecision, MatchStrategy

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decision(a: str, b: str, *, is_match: bool, version: str = "v1.0") -> MatchDecision:
    return MatchDecision(
        record_a_id=a,
        record_b_id=b,
        rule_id="test-rule",
        strategy=MatchStrategy.DETERMINISTIC,
        is_match=is_match,
        confidence_score=1.0 if is_match else 0.0,
        matched_fields=("email",),
        rule_set_version=version,
    )


def _label(a: str, b: str, *, is_true_match: bool) -> MatchPairLabel:
    return MatchPairLabel(record_a_id=a, record_b_id=b, is_true_match=is_true_match)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMatchEvaluator:
    def test_perfect_precision_recall(self) -> None:
        decisions = [
            _decision("A", "B", is_match=True),
            _decision("C", "D", is_match=False),
        ]
        labels = [
            _label("A", "B", is_true_match=True),
            _label("C", "D", is_true_match=False),
        ]
        evaluator = MatchEvaluator()
        report = evaluator.evaluate(decisions, labels, run_id="run1", rule_set_version="v1.0")

        assert isinstance(report, MatchEvaluationReport)
        assert report.precision == pytest.approx(1.0)
        assert report.recall == pytest.approx(1.0)
        assert report.f1_score == pytest.approx(1.0)
        assert report.true_positive_count == 1
        assert report.false_positive_count == 0
        assert report.false_negative_count == 0
        assert report.true_negative_count == 1

    def test_false_positive_reduces_precision(self) -> None:
        decisions = [
            _decision("A", "B", is_match=True),  # TP
            _decision("C", "D", is_match=True),  # FP (ground truth says no match)
        ]
        labels = [
            _label("A", "B", is_true_match=True),
            _label("C", "D", is_true_match=False),
        ]
        evaluator = MatchEvaluator()
        report = evaluator.evaluate(decisions, labels, run_id="run2", rule_set_version="v1.0")

        assert report.precision == pytest.approx(0.5)
        assert report.recall == pytest.approx(1.0)
        assert report.false_positive_count == 1

    def test_false_negative_reduces_recall(self) -> None:
        decisions = [
            _decision("A", "B", is_match=False),  # FN (ground truth says match)
            _decision("C", "D", is_match=False),  # TN
        ]
        labels = [
            _label("A", "B", is_true_match=True),
            _label("C", "D", is_true_match=False),
        ]
        evaluator = MatchEvaluator()
        report = evaluator.evaluate(decisions, labels, run_id="run3", rule_set_version="v1.0")

        assert report.recall == pytest.approx(0.0)
        assert report.false_negative_count == 1
        assert report.true_negative_count == 1

    def test_empty_decisions_all_fn_or_tn(self) -> None:
        labels = [
            _label("A", "B", is_true_match=True),
            _label("C", "D", is_true_match=False),
        ]
        evaluator = MatchEvaluator()
        report = evaluator.evaluate([], labels, run_id="run4", rule_set_version="v1.0")

        assert report.precision == pytest.approx(0.0)
        assert report.recall == pytest.approx(0.0)
        assert report.false_negative_count == 1  # A-B labelled true but not predicted
        assert report.true_negative_count == 1  # C-D labelled false and not predicted

    def test_empty_labels_returns_zero_metrics(self) -> None:
        decisions = [_decision("A", "B", is_match=True)]
        evaluator = MatchEvaluator()
        report = evaluator.evaluate(decisions, [], run_id="run5", rule_set_version="v1.0")

        assert report.precision == pytest.approx(0.0)
        assert report.recall == pytest.approx(0.0)
        assert report.labelled_pair_count == 0

    def test_pair_order_is_canonical(self) -> None:
        """Decision order (A,B) should match label order (B,A) after canonicalisation."""
        decisions = [_decision("B", "A", is_match=True)]  # reversed order
        labels = [_label("A", "B", is_true_match=True)]
        evaluator = MatchEvaluator()
        report = evaluator.evaluate(decisions, labels, run_id="run6", rule_set_version="v1.0")

        assert report.true_positive_count == 1

    def test_report_has_correct_metadata(self) -> None:
        evaluator = MatchEvaluator()
        report = evaluator.evaluate([], [], run_id="test-run", rule_set_version="v2.0")
        assert report.run_id == "test-run"
        assert report.rule_set_version == "v2.0"
        assert "T" in report.evaluated_at

    def test_accuracy_property(self) -> None:
        decisions = [
            _decision("A", "B", is_match=True),
            _decision("C", "D", is_match=False),
        ]
        labels = [
            _label("A", "B", is_true_match=True),
            _label("C", "D", is_true_match=False),
        ]
        evaluator = MatchEvaluator()
        report = evaluator.evaluate(decisions, labels, run_id="acc", rule_set_version="v1.0")
        assert report.accuracy == pytest.approx(1.0)

    def test_pair_details_have_correct_outcomes(self) -> None:
        decisions = [_decision("A", "B", is_match=True)]
        labels = [_label("A", "B", is_true_match=True)]
        evaluator = MatchEvaluator()
        report = evaluator.evaluate(decisions, labels, run_id="r", rule_set_version="v1.0")

        assert len(report.pair_details) == 1
        detail = report.pair_details[0]
        assert isinstance(detail, PairEvaluationDetail)
        assert detail.outcome == "TP"
        assert detail.predicted_match is True
        assert detail.true_match is True

    def test_f1_harmonic_mean(self) -> None:
        """F1 should be the harmonic mean of precision and recall."""
        decisions = [
            _decision("A", "B", is_match=True),  # TP
            _decision("C", "D", is_match=True),  # FP
            _decision("E", "F", is_match=False),  # FN
        ]
        labels = [
            _label("A", "B", is_true_match=True),
            _label("C", "D", is_true_match=False),
            _label("E", "F", is_true_match=True),
        ]
        evaluator = MatchEvaluator()
        report = evaluator.evaluate(decisions, labels, run_id="f1", rule_set_version="v1.0")

        expected_precision = 1 / 2
        expected_recall = 1 / 2
        p, r = expected_precision, expected_recall
        expected_f1 = 2 * p * r / (p + r)
        assert report.f1_score == pytest.approx(expected_f1)


class TestCanonicalPair:
    def test_canonical_pair_same_order(self) -> None:
        assert _canonical_pair("A", "B") == ("A", "B")

    def test_canonical_pair_reversed(self) -> None:
        assert _canonical_pair("B", "A") == ("A", "B")

    def test_canonical_pair_equal_strings(self) -> None:
        assert _canonical_pair("X", "X") == ("X", "X")


class TestAccuracyEdgeCases:
    """Cover L94: `accuracy` returns 0.0 when all counts are zero."""

    def test_accuracy_zero_when_no_labels_and_no_decisions(self) -> None:
        evaluator = MatchEvaluator()
        report = evaluator.evaluate([], [], run_id="zero", rule_set_version="v1.0")
        # TP=FP=FN=TN=0 → total=0 → accuracy returns 0.0
        assert report.accuracy == pytest.approx(0.0)
