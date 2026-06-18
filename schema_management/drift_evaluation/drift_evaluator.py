"""
Schema drift evaluator for the Enterprise Data Lake platform.

Compares a current SchemaSnapshot against the previous snapshot and produces
a DriftReport capturing every per-field change and an overall classification.

Classification rules (applied in precedence order — highest severity wins):

  BREAKING (halts downstream transformation):
    - A field is removed
    - A field's data_type changes
    - A non-nullable field is added (downstream systems cannot handle new NULL column)
    - A field's nullability changes from nullable → non-nullable

  POTENTIALLY_BREAKING (alerts downstream; extraction continues):
    - A field's precision, scale, or length changes in any direction

  NON_BREAKING (additive; extraction continues; downstream alerted):
    - A new nullable field is added
    - A field's nullability changes from non-nullable → nullable

  NO_DRIFT:
    - Current and previous schema fingerprints are identical
    - No previous snapshot exists (first run)

When the overall classification is BREAKING:
  - Extraction continues (raw data is written for manual review)
  - Downstream transformation is paused (DriftReport.is_transformation_blocked → True)
  - The RunCoordinator emits a CloudWatch alarm via the metrics emitter

The drift report is written to S3 alongside every schema snapshot on every run,
including NO_DRIFT runs (provides an auditable history of each evaluation).

Security:
  - Drift reports contain only structural metadata (field names, types, flags).
    Field VALUES are never included.  There is no path for sensitive data to
    enter the report via this module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from contracts.pipeline_stage_contract import DriftClassification
from schema_management.snapshot_repository.snapshot_repository import FieldSnapshot, SchemaSnapshot

# Severity ordering — used to promote the overall classification.
_SEVERITY: Final[dict[DriftClassification, int]] = {
    DriftClassification.NO_DRIFT: 0,
    DriftClassification.NON_BREAKING: 1,
    DriftClassification.POTENTIALLY_BREAKING: 2,
    DriftClassification.BREAKING: 3,
}


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


class FieldChangeKind(StrEnum):
    """Describes the type of structural change detected for a single field."""

    ADDED = "added"
    REMOVED = "removed"
    TYPE_CHANGED = "type_changed"
    PRECISION_CHANGED = "precision_changed"
    SCALE_CHANGED = "scale_changed"
    LENGTH_CHANGED = "length_changed"
    NULLABILITY_CHANGED = "nullability_changed"


@dataclass(frozen=True)
class FieldDriftDetail:
    """
    Describes the drift detected for a single field.

    Contains only structural metadata — field values are never stored here.
    previous_value and current_value are string representations of metadata
    attributes (e.g. "double", "10", "True") — never actual field data.
    """

    field_name: str
    change_kind: FieldChangeKind
    classification: DriftClassification
    previous_value: str | None = None
    current_value: str | None = None


@dataclass(frozen=True)
class DriftReport:
    """
    Complete drift evaluation result produced by comparing two schema snapshots.

    Written to S3 alongside every schema snapshot (including NO_DRIFT runs) to
    provide an auditable evaluation history.
    """

    source_id: str
    entity_id: str
    evaluated_at: str  # ISO-8601 datetime (UTC)
    previous_schema_version: str | None
    current_schema_version: str
    overall_classification: DriftClassification
    field_changes: tuple[FieldDriftDetail, ...]

    @property
    def is_transformation_blocked(self) -> bool:
        """True when downstream transformation must be paused (BREAKING drift)."""
        return self.overall_classification == DriftClassification.BREAKING

    @property
    def has_changes(self) -> bool:
        """True when at least one structural change was detected."""
        return self.overall_classification != DriftClassification.NO_DRIFT

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain dict suitable for JSON persistence to S3."""
        return {
            "source_id": self.source_id,
            "entity_id": self.entity_id,
            "evaluated_at": self.evaluated_at,
            "previous_schema_version": self.previous_schema_version,
            "current_schema_version": self.current_schema_version,
            "overall_classification": str(self.overall_classification),
            "is_transformation_blocked": self.is_transformation_blocked,
            "field_changes": [
                {
                    "field_name": c.field_name,
                    "change_kind": str(c.change_kind),
                    "classification": str(c.classification),
                    "previous_value": c.previous_value,
                    "current_value": c.current_value,
                }
                for c in self.field_changes
            ],
        }

    def to_json(self) -> str:
        """Serialise to a compact JSON string."""
        return json.dumps(self.to_dict(), separators=(",", ":"))


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------


class SchemaDriftEvaluator:
    """
    Stateless evaluator that compares two SchemaSnapshot instances.

    evaluate() is a pure function with no side effects.  Callers are
    responsible for writing the returned DriftReport to S3.
    """

    def evaluate(
        self,
        current: SchemaSnapshot,
        previous: SchemaSnapshot | None,
    ) -> DriftReport:
        """
        Compare the current snapshot against the previous snapshot.

        When previous is None (first run) the result is always NO_DRIFT —
        there is nothing to compare against.

        NOTE: schema_version fingerprint equality is intentionally NOT used as a
        short-circuit.  FieldContract.compute_fingerprint() excludes is_nullable
        (per Phase 1 design — label/description changes don't affect the hash).
        Two snapshots with identical fingerprints can still differ in nullability,
        which is a BREAKING or NON_BREAKING change.  A full field-by-field diff is
        always performed when a previous snapshot exists.
        """
        evaluated_at = datetime.now(tz=UTC).isoformat()

        if previous is None:
            return DriftReport(
                source_id=current.source_id,
                entity_id=current.entity_id,
                evaluated_at=evaluated_at,
                previous_schema_version=None,
                current_schema_version=current.schema_version,
                overall_classification=DriftClassification.NO_DRIFT,
                field_changes=(),
            )

        previous_by_name: dict[str, FieldSnapshot] = {f.name: f for f in previous.fields}
        current_by_name: dict[str, FieldSnapshot] = {f.name: f for f in current.fields}

        changes: list[FieldDriftDetail] = []

        # 1. Removed fields — always BREAKING
        for name, prev_field in previous_by_name.items():
            if name not in current_by_name:
                changes.append(
                    FieldDriftDetail(
                        field_name=name,
                        change_kind=FieldChangeKind.REMOVED,
                        classification=DriftClassification.BREAKING,
                        previous_value=prev_field.data_type,
                        current_value=None,
                    )
                )

        # 2. Added fields — classification depends on nullability
        for name, curr_field in current_by_name.items():
            if name not in previous_by_name:
                classification = (
                    DriftClassification.NON_BREAKING
                    if curr_field.is_nullable
                    else DriftClassification.BREAKING
                )
                changes.append(
                    FieldDriftDetail(
                        field_name=name,
                        change_kind=FieldChangeKind.ADDED,
                        classification=classification,
                        previous_value=None,
                        current_value=curr_field.data_type,
                    )
                )

        # 3. Modified fields — evaluate attribute-by-attribute
        for name in previous_by_name:
            if name not in current_by_name:
                continue  # already captured as REMOVED above
            changes.extend(
                _evaluate_field_modifications(name, previous_by_name[name], current_by_name[name])
            )

        overall = _compute_overall_classification(changes)
        return DriftReport(
            source_id=current.source_id,
            entity_id=current.entity_id,
            evaluated_at=evaluated_at,
            previous_schema_version=previous.schema_version,
            current_schema_version=current.schema_version,
            # _compute_overall_classification returns NO_DRIFT when changes is empty,
            # which covers the case where schema_version strings differ but the fields
            # are structurally identical (e.g. fingerprint computed with a different
            # algorithm version).
            overall_classification=overall,
            field_changes=tuple(changes),
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _evaluate_field_modifications(
    name: str,
    prev: FieldSnapshot,
    curr: FieldSnapshot,
) -> list[FieldDriftDetail]:
    changes: list[FieldDriftDetail] = []

    if prev.data_type != curr.data_type:
        changes.append(
            FieldDriftDetail(
                field_name=name,
                change_kind=FieldChangeKind.TYPE_CHANGED,
                classification=DriftClassification.BREAKING,
                previous_value=prev.data_type,
                current_value=curr.data_type,
            )
        )

    if prev.precision != curr.precision:
        changes.append(
            FieldDriftDetail(
                field_name=name,
                change_kind=FieldChangeKind.PRECISION_CHANGED,
                classification=DriftClassification.POTENTIALLY_BREAKING,
                previous_value=str(prev.precision) if prev.precision is not None else None,
                current_value=str(curr.precision) if curr.precision is not None else None,
            )
        )

    if prev.scale != curr.scale:
        changes.append(
            FieldDriftDetail(
                field_name=name,
                change_kind=FieldChangeKind.SCALE_CHANGED,
                classification=DriftClassification.POTENTIALLY_BREAKING,
                previous_value=str(prev.scale) if prev.scale is not None else None,
                current_value=str(curr.scale) if curr.scale is not None else None,
            )
        )

    if prev.length != curr.length:
        changes.append(
            FieldDriftDetail(
                field_name=name,
                change_kind=FieldChangeKind.LENGTH_CHANGED,
                classification=DriftClassification.POTENTIALLY_BREAKING,
                previous_value=str(prev.length) if prev.length is not None else None,
                current_value=str(curr.length) if curr.length is not None else None,
            )
        )

    if prev.is_nullable != curr.is_nullable:
        # nullable → non-nullable: downstream data may break on unexpected NULLs → BREAKING
        # non-nullable → nullable: additive relaxation → NON_BREAKING
        classification = (
            DriftClassification.BREAKING
            if not curr.is_nullable
            else DriftClassification.NON_BREAKING
        )
        changes.append(
            FieldDriftDetail(
                field_name=name,
                change_kind=FieldChangeKind.NULLABILITY_CHANGED,
                classification=classification,
                previous_value=str(prev.is_nullable),
                current_value=str(curr.is_nullable),
            )
        )

    return changes


def _compute_overall_classification(changes: list[FieldDriftDetail]) -> DriftClassification:
    if not changes:
        return DriftClassification.NO_DRIFT
    return max(
        (c.classification for c in changes),
        key=lambda c: _SEVERITY[c],
    )
