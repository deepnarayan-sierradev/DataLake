"""
Tests for the Schema Drift Evaluator (2.4).

Covers:
  - NO_DRIFT when previous snapshot is None (first run)
  - NO_DRIFT when fingerprints are identical
  - NON_BREAKING: new nullable field added
  - BREAKING: new non-nullable field added
  - BREAKING: field removed
  - BREAKING: field type changed
  - POTENTIALLY_BREAKING: precision changed
  - POTENTIALLY_BREAKING: scale changed
  - POTENTIALLY_BREAKING: length changed
  - BREAKING: nullability changed nullable → non-nullable
  - NON_BREAKING: nullability changed non-nullable → nullable
  - Overall classification escalates to highest severity
  - is_transformation_blocked on BREAKING
  - has_changes on non-NO_DRIFT reports
  - to_json produces valid JSON
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from contracts.pipeline_stage_contract import DriftClassification
from schema_management.drift_evaluation.drift_evaluator import (
    DriftReport,
    FieldChangeKind,
    SchemaDriftEvaluator,
    _compute_overall_classification,
)
from schema_management.snapshot_repository.snapshot_repository import (
    FieldSnapshot,
    SchemaSnapshot,
)

_CAPTURED_AT = datetime(2026, 6, 11, 14, 0, 0, tzinfo=UTC).isoformat()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _snap(
    fields: tuple[FieldSnapshot, ...],
    schema_version: str = "v1",
    source_id: str = "salesforce",
    entity_id: str = "salesforce-account",
) -> SchemaSnapshot:
    return SchemaSnapshot(
        source_id=source_id,
        entity_id=entity_id,
        schema_version=schema_version,
        extraction_date="2026-06-11",
        captured_at=_CAPTURED_AT,
        fields=fields,
    )


def _field(
    name: str,
    data_type: str = "string",
    is_nullable: bool = True,
    is_queryable: bool = True,
    length: int | None = None,
    precision: int | None = None,
    scale: int | None = None,
) -> FieldSnapshot:
    return FieldSnapshot(
        name=name,
        data_type=data_type,
        is_nullable=is_nullable,
        is_queryable=is_queryable,
        length=length,
        precision=precision,
        scale=scale,
    )


_EVALUATOR = SchemaDriftEvaluator()


# ---------------------------------------------------------------------------
# NO_DRIFT
# ---------------------------------------------------------------------------


class TestNoDrift:
    def test_no_drift_when_previous_is_none(self) -> None:
        current = _snap((_field("Id"),))
        report = _EVALUATOR.evaluate(current, previous=None)
        assert report.overall_classification == DriftClassification.NO_DRIFT
        assert not report.has_changes
        assert not report.is_transformation_blocked
        assert report.previous_schema_version is None

    def test_no_drift_when_fingerprints_match(self) -> None:
        fields = (_field("Id"), _field("Name"))
        current = _snap(fields, schema_version="same")
        previous = _snap(fields, schema_version="same")
        report = _EVALUATOR.evaluate(current, previous)
        assert report.overall_classification == DriftClassification.NO_DRIFT
        assert len(report.field_changes) == 0


# ---------------------------------------------------------------------------
# NON_BREAKING
# ---------------------------------------------------------------------------


class TestNonBreaking:
    def test_new_nullable_field_is_non_breaking(self) -> None:
        prev_fields = (_field("Id"),)
        curr_fields = (_field("Id"), _field("NewField__c", is_nullable=True))
        previous = _snap(prev_fields, schema_version="v1")
        current = _snap(curr_fields, schema_version="v2")

        report = _EVALUATOR.evaluate(current, previous)

        assert report.overall_classification == DriftClassification.NON_BREAKING
        assert not report.is_transformation_blocked
        assert report.has_changes
        assert any(
            c.change_kind == FieldChangeKind.ADDED
            and c.classification == DriftClassification.NON_BREAKING
            for c in report.field_changes
        )

    def test_nullability_relaxation_is_non_breaking(self) -> None:
        prev = _snap((_field("Amount", is_nullable=False),), schema_version="v1")
        curr = _snap((_field("Amount", is_nullable=True),), schema_version="v2")
        report = _EVALUATOR.evaluate(curr, prev)
        assert report.overall_classification == DriftClassification.NON_BREAKING


# ---------------------------------------------------------------------------
# POTENTIALLY_BREAKING
# ---------------------------------------------------------------------------


class TestPotentiallyBreaking:
    def test_precision_change_is_potentially_breaking(self) -> None:
        prev = _snap((_field("Amount", precision=18),), schema_version="v1")
        curr = _snap((_field("Amount", precision=10),), schema_version="v2")
        report = _EVALUATOR.evaluate(curr, prev)
        assert report.overall_classification == DriftClassification.POTENTIALLY_BREAKING
        assert any(c.change_kind == FieldChangeKind.PRECISION_CHANGED for c in report.field_changes)

    def test_scale_change_is_potentially_breaking(self) -> None:
        prev = _snap((_field("Amount", scale=4),), schema_version="v1")
        curr = _snap((_field("Amount", scale=2),), schema_version="v2")
        report = _EVALUATOR.evaluate(curr, prev)
        assert report.overall_classification == DriftClassification.POTENTIALLY_BREAKING

    def test_length_change_is_potentially_breaking(self) -> None:
        prev = _snap((_field("Name", length=255),), schema_version="v1")
        curr = _snap((_field("Name", length=128),), schema_version="v2")
        report = _EVALUATOR.evaluate(curr, prev)
        assert report.overall_classification == DriftClassification.POTENTIALLY_BREAKING
        assert any(c.change_kind == FieldChangeKind.LENGTH_CHANGED for c in report.field_changes)


# ---------------------------------------------------------------------------
# BREAKING
# ---------------------------------------------------------------------------


class TestBreaking:
    def test_field_removed_is_breaking(self) -> None:
        prev = _snap((_field("Id"), _field("Name")), schema_version="v1")
        curr = _snap((_field("Id"),), schema_version="v2")
        report = _EVALUATOR.evaluate(curr, prev)
        assert report.overall_classification == DriftClassification.BREAKING
        assert report.is_transformation_blocked
        assert any(c.change_kind == FieldChangeKind.REMOVED for c in report.field_changes)

    def test_type_change_is_breaking(self) -> None:
        prev = _snap((_field("Amount", data_type="double"),), schema_version="v1")
        curr = _snap((_field("Amount", data_type="string"),), schema_version="v2")
        report = _EVALUATOR.evaluate(curr, prev)
        assert report.overall_classification == DriftClassification.BREAKING
        assert any(c.change_kind == FieldChangeKind.TYPE_CHANGED for c in report.field_changes)

    def test_non_nullable_field_added_is_breaking(self) -> None:
        prev = _snap((_field("Id"),), schema_version="v1")
        curr = _snap(
            (_field("Id"), _field("RequiredField", is_nullable=False)),
            schema_version="v2",
        )
        report = _EVALUATOR.evaluate(curr, prev)
        assert report.overall_classification == DriftClassification.BREAKING
        assert any(
            c.change_kind == FieldChangeKind.ADDED
            and c.classification == DriftClassification.BREAKING
            for c in report.field_changes
        )

    def test_nullability_tightening_is_breaking(self) -> None:
        prev = _snap((_field("Amount", is_nullable=True),), schema_version="v1")
        curr = _snap((_field("Amount", is_nullable=False),), schema_version="v2")
        report = _EVALUATOR.evaluate(curr, prev)
        assert report.overall_classification == DriftClassification.BREAKING


# ---------------------------------------------------------------------------
# Escalation: multiple changes escalate to highest severity
# ---------------------------------------------------------------------------


class TestClassificationEscalation:
    def test_breaking_change_overrides_non_breaking(self) -> None:
        # One NON_BREAKING (new nullable field) + one BREAKING (field removed)
        prev = _snap((_field("Id"), _field("OldField")), schema_version="v1")
        curr = _snap((_field("Id"), _field("NewNullable", is_nullable=True)), schema_version="v2")
        report = _EVALUATOR.evaluate(curr, prev)
        assert report.overall_classification == DriftClassification.BREAKING

    def test_breaking_overrides_potentially_breaking(self) -> None:
        # POTENTIALLY_BREAKING: precision change
        # BREAKING: type change
        prev = _snap((_field("A", data_type="double", precision=18),), schema_version="v1")
        curr = _snap((_field("A", data_type="string", precision=10),), schema_version="v2")
        report = _EVALUATOR.evaluate(curr, prev)
        assert report.overall_classification == DriftClassification.BREAKING

    def test_compute_overall_with_empty_changes_is_no_drift(self) -> None:
        assert _compute_overall_classification([]) == DriftClassification.NO_DRIFT


# ---------------------------------------------------------------------------
# DriftReport properties and serialisation
# ---------------------------------------------------------------------------


class TestDriftReport:
    def test_is_transformation_blocked_false_for_non_breaking(self) -> None:
        report = DriftReport(
            source_id="salesforce",
            entity_id="salesforce-account",
            evaluated_at=_CAPTURED_AT,
            previous_schema_version="v1",
            current_schema_version="v2",
            overall_classification=DriftClassification.NON_BREAKING,
            field_changes=(),
        )
        assert not report.is_transformation_blocked

    def test_is_transformation_blocked_true_for_breaking(self) -> None:
        report = DriftReport(
            source_id="salesforce",
            entity_id="salesforce-account",
            evaluated_at=_CAPTURED_AT,
            previous_schema_version="v1",
            current_schema_version="v2",
            overall_classification=DriftClassification.BREAKING,
            field_changes=(),
        )
        assert report.is_transformation_blocked

    def test_to_json_produces_valid_json(self) -> None:
        prev = _snap((_field("Id"), _field("Name")), schema_version="v1")
        curr = _snap((_field("Id"),), schema_version="v2")
        report = _EVALUATOR.evaluate(curr, prev)
        json_str = report.to_json()
        parsed = json.loads(json_str)
        assert parsed["overall_classification"] == "breaking"
        assert parsed["is_transformation_blocked"] is True
        assert "field_changes" in parsed
        assert "evaluated_at" in parsed

    def test_to_json_contains_no_sensitive_values(self) -> None:
        """Confirm the drift report only contains structural metadata."""
        prev = _snap((_field("Id"),), schema_version="v1")
        curr = _snap((_field("Id"), _field("NewField", data_type="string")), schema_version="v2")
        report = _EVALUATOR.evaluate(curr, prev)
        parsed = json.loads(report.to_json())
        for change in parsed["field_changes"]:
            # Only metadata keys present; no data values
            assert set(change.keys()) == {
                "field_name",
                "change_kind",
                "classification",
                "previous_value",
                "current_value",
            }


# ---------------------------------------------------------------------------
# Regression tests for fixed bugs
# ---------------------------------------------------------------------------


class TestFingerprintShortCircuitRegression:
    """
    Regression tests for the fingerprint equality short-circuit removal (Bug #1).

    FieldContract.compute_fingerprint() excludes is_nullable.  Two snapshots
    with the same schema_version (fingerprint) can therefore differ in
    nullability.  The evaluator must perform a full field diff and detect the
    change rather than returning NO_DRIFT based on fingerprint equality alone.
    """

    def test_same_fingerprint_nullable_to_non_nullable_is_breaking(self) -> None:
        same_fp = "aaabbbccc111222"
        prev = _snap((_field("Amount", is_nullable=True),), schema_version=same_fp)
        curr = _snap((_field("Amount", is_nullable=False),), schema_version=same_fp)
        report = _EVALUATOR.evaluate(curr, prev)
        # nullable → non-nullable is BREAKING even when schema_version strings are equal.
        assert report.overall_classification == DriftClassification.BREAKING
        assert report.is_transformation_blocked
        assert any(
            c.change_kind == FieldChangeKind.NULLABILITY_CHANGED for c in report.field_changes
        )

    def test_same_fingerprint_non_nullable_to_nullable_is_non_breaking(self) -> None:
        same_fp = "aaabbbccc111222"
        prev = _snap((_field("Amount", is_nullable=False),), schema_version=same_fp)
        curr = _snap((_field("Amount", is_nullable=True),), schema_version=same_fp)
        report = _EVALUATOR.evaluate(curr, prev)
        # non-nullable → nullable is NON_BREAKING; still detected even with same fingerprint.
        assert report.overall_classification == DriftClassification.NON_BREAKING
        assert not report.is_transformation_blocked

    def test_same_fingerprint_identical_fields_is_no_drift(self) -> None:
        same_fp = "aaabbbccc111222"
        fields = (_field("Id"), _field("Name", is_nullable=True))
        prev = _snap(fields, schema_version=same_fp)
        curr = _snap(fields, schema_version=same_fp)
        report = _EVALUATOR.evaluate(curr, prev)
        # Truly identical fields → NO_DRIFT even after full diff.
        assert report.overall_classification == DriftClassification.NO_DRIFT
        assert len(report.field_changes) == 0
