"""
Match evaluation report (spec §7 AC: precision and recall evaluation workflow).

Provides a deterministic precision/recall evaluation framework for comparing
two sets of match decisions from the same input records.

Use cases:
  1. Compare a candidate matching run against a gold-standard labelled dataset.
  2. Compare two rule-set versions to detect regression before promotion.
  3. Run periodic evaluation on a held-out sample to track match quality over time.

Definitions (binary classification on record pairs):
  TP — pair that is a true match and the engine predicted MATCH.
  FP — pair that is NOT a true match but the engine predicted MATCH.
  FN — pair that IS a true match but the engine predicted NO MATCH.
  TN — pair that is NOT a true match and the engine predicted NO MATCH.

  Precision = TP / (TP + FP)   — of predicted matches, how many were correct
  Recall    = TP / (TP + FN)   — of true matches, how many were found
  F1        = 2 * P * R / (P + R)

Security (OWASP A09):
  - Evaluation report contains only aggregate statistics.
  - Individual record identifiers are included only in per-pair detail
    (no data values written to the report).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from entity_resolution.matching_engine.match_rule_engine import MatchDecision
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)


@dataclass(frozen=True)
class MatchPairLabel:
    """
    Ground-truth label for a pair of record IDs.

    record_a_id / record_b_id must match the id_field values used during
    matching.  Order is canonical (a < b lexicographically).
    """

    record_a_id: str
    record_b_id: str
    is_true_match: bool


@dataclass(frozen=True)
class PairEvaluationDetail:
    """Per-pair evaluation outcome."""

    record_a_id: str
    record_b_id: str
    predicted_match: bool
    true_match: bool
    outcome: str  # "TP" | "FP" | "FN" | "TN"


@dataclass(frozen=True)
class MatchEvaluationReport:
    """
    Precision / recall / F1 evaluation report for one matching run.

    All metrics computed over the labelled pairs only.
    """

    run_id: str
    rule_set_version: str
    labelled_pair_count: int
    true_positive_count: int
    false_positive_count: int
    false_negative_count: int
    true_negative_count: int
    precision: float
    recall: float
    f1_score: float
    pair_details: tuple[PairEvaluationDetail, ...]
    evaluated_at: str  # ISO-8601 UTC

    @property
    def accuracy(self) -> float:
        total = (
            self.true_positive_count
            + self.true_negative_count
            + self.false_positive_count
            + self.false_negative_count
        )
        if total == 0:
            return 0.0
        return (self.true_positive_count + self.true_negative_count) / total


class MatchEvaluator:
    """
    Computes precision, recall, and F1 for a set of MatchDecisions given
    ground-truth labels.
    """

    def evaluate(
        self,
        decisions: list[MatchDecision],
        labels: list[MatchPairLabel],
        run_id: str,
        rule_set_version: str,
    ) -> MatchEvaluationReport:
        """
        Evaluate match decisions against ground-truth labels.

        Args:
            decisions:        List of MatchDecision objects from a matching run.
            labels:           Ground-truth pair labels (true/false match).
            run_id:           Run ID for traceability.
            rule_set_version: Version of the rule set that produced decisions.

        Returns:
            MatchEvaluationReport.
        """
        # Build lookup: canonical pair key → predicted match result
        predicted: dict[tuple[str, str], bool] = {}
        for d in decisions:
            pair_key = _canonical_pair(d.record_a_id, d.record_b_id)
            # Multiple rules may evaluate same pair; use OR logic (any match = predicted match)
            predicted[pair_key] = predicted.get(pair_key, False) or d.is_match

        pair_details: list[PairEvaluationDetail] = []
        tp = fp = fn = tn = 0

        for label in labels:
            pair_key = _canonical_pair(label.record_a_id, label.record_b_id)
            pred = predicted.get(pair_key, False)
            true = label.is_true_match

            if pred and true:
                outcome = "TP"
                tp += 1
            elif pred and not true:
                outcome = "FP"
                fp += 1
            elif not pred and true:
                outcome = "FN"
                fn += 1
            else:
                outcome = "TN"
                tn += 1

            pair_details.append(
                PairEvaluationDetail(
                    record_a_id=label.record_a_id,
                    record_b_id=label.record_b_id,
                    predicted_match=pred,
                    true_match=true,
                    outcome=outcome,
                )
            )

        precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = (2 * precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0

        report = MatchEvaluationReport(
            run_id=run_id,
            rule_set_version=rule_set_version,
            labelled_pair_count=len(labels),
            true_positive_count=tp,
            false_positive_count=fp,
            false_negative_count=fn,
            true_negative_count=tn,
            precision=precision,
            recall=recall,
            f1_score=f1,
            pair_details=tuple(pair_details),
            evaluated_at=datetime.now(UTC).isoformat(),
        )

        _logger.info(
            "match_evaluation_complete",
            run_id=run_id,
            rule_set_version=rule_set_version,
            labelled_pairs=len(labels),
            precision=round(precision, 4),
            recall=round(recall, 4),
            f1=round(f1, 4),
        )

        return report


def _canonical_pair(a: str, b: str) -> tuple[str, str]:
    """Return pair in canonical (lexicographic) order to make lookup order-independent."""
    return (a, b) if a <= b else (b, a)
