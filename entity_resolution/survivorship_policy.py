"""
Golden record survivorship policy.

Defines which source record "wins" per attribute when matched records from
multiple sources conflict.  Policies are configurable per entity type and
attribute — no hardcoded source priorities.

Survivorship strategies per attribute:
  SOURCE_PRIORITY — prefer the value from the highest-priority source
  MOST_RECENT     — prefer the value with the latest timestamp
  LONGEST         — prefer the longest non-null string value
  FIRST_NON_NULL  — use the first non-null value in source priority order

Every survivorship decision is written to the conflict_resolution_log so that
the choice is auditable and traceable to the contributing source records.

Security (OWASP A09):
  - Conflict log never includes raw field values — only record IDs and
    source metadata (PII protection in diagnostics).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)


class SurvivorshipStrategy(StrEnum):
    SOURCE_PRIORITY = "source_priority"
    MOST_RECENT = "most_recent"
    LONGEST = "longest"
    FIRST_NON_NULL = "first_non_null"


@dataclass(frozen=True)
class AttributeSurvivorshipRule:
    """Survivorship rule for one canonical attribute."""

    canonical_field: str
    strategy: SurvivorshipStrategy
    source_priority: tuple[str, ...]  # ordered list of source_ids (highest priority first)
    timestamp_field: str | None = None  # required for MOST_RECENT strategy


@dataclass(frozen=True)
class SurvivorshipPolicy:
    """Versioned survivorship policy for an entity type."""

    entity_type: str
    policy_version: str
    attribute_rules: tuple[AttributeSurvivorshipRule, ...]
    default_strategy: SurvivorshipStrategy = SurvivorshipStrategy.FIRST_NON_NULL
    # Explicit output schema — only these fields appear in the canonical record.
    # System fields (golden_id, contributing_source_records, etc.) are always
    # included by the publisher regardless of this list.
    # Empty tuple means pass-through (union of all source fields — use only in tests).
    output_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConflictResolutionEntry:
    """
    Audit record for one attribute survivorship decision.
    Values are NOT included — only structural metadata.
    """

    canonical_field: str
    winning_source_id: str
    winning_record_id: str
    strategy_applied: SurvivorshipStrategy
    candidate_source_count: int


@dataclass(frozen=True)
class SurvivorshipResult:
    """Result of applying a survivorship policy to a cluster of matched records."""

    canonical_record: dict[str, Any]
    contributing_record_ids: tuple[str, ...]
    conflict_log: tuple[ConflictResolutionEntry, ...]
    # Maps every output field to the source_id that won for that field.
    # Queryable directly in Athena via json_extract_scalar(field_provenance, '$.full_name').
    field_provenance: dict[str, str]


class GoldenRecordSurvivorshipPolicy:
    """
    Applies a SurvivorshipPolicy to a cluster of matched canonical records.

    Usage:
      policy_applier = GoldenRecordSurvivorshipPolicy(policy)
      result = policy_applier.resolve(records, id_field="record_id", source_field="source_id")
    """

    def __init__(self, policy: SurvivorshipPolicy) -> None:
        self._policy = policy
        self._rules_by_field: dict[str, AttributeSurvivorshipRule] = {
            r.canonical_field: r for r in policy.attribute_rules
        }

    def resolve(
        self,
        records: list[dict[str, Any]],
        id_field: str,
        source_field: str,
    ) -> SurvivorshipResult:
        """
        Resolve a cluster of matched records into a single canonical record.

        Args:
            records:      Matched canonical records from one cluster.
            id_field:     Field name containing the record identifier.
            source_field: Field name containing the source_id.

        Returns:
            SurvivorshipResult with the merged canonical record + audit log.
        """
        if not records:
            raise SurvivorshipError("Cannot resolve empty cluster")

        # Collect all unique field names across all records
        all_fields = {k for r in records for k in r}
        canonical: dict[str, Any] = {}
        provenance: dict[str, str] = {}
        conflict_log: list[ConflictResolutionEntry] = []

        record_ids = tuple(str(r.get(id_field, "?")) for r in records)

        for field_name in sorted(all_fields):
            rule = self._rules_by_field.get(field_name)
            strategy = rule.strategy if rule else self._policy.default_strategy
            source_priority = rule.source_priority if rule else ()
            timestamp_field = rule.timestamp_field if rule else None

            winner_record, winner_value = self._apply_strategy(
                records=records,
                field_name=field_name,
                strategy=strategy,
                source_priority=source_priority,
                source_field=source_field,
                timestamp_field=timestamp_field,
            )

            if winner_value is not None:
                canonical[field_name] = winner_value
                winning_src = str(winner_record.get(source_field, "?"))
                provenance[field_name] = winning_src
                candidates_with_value = sum(1 for r in records if r.get(field_name) is not None)
                if candidates_with_value > 1:
                    # Only log when there was actual conflict
                    conflict_log.append(
                        ConflictResolutionEntry(
                            canonical_field=field_name,
                            winning_source_id=winning_src,
                            winning_record_id=str(winner_record.get(id_field, "?")),
                            strategy_applied=strategy,
                            candidate_source_count=candidates_with_value,
                        )
                    )

        # Apply output schema projection: drop any field not in output_fields.
        # System fields are excluded from the projection check — the publisher adds them.
        if self._policy.output_fields:
            canonical = {
                k: v for k, v in canonical.items()
                if k in self._policy.output_fields
            }
            provenance = {k: v for k, v in provenance.items() if k in self._policy.output_fields}

        return SurvivorshipResult(
            canonical_record=canonical,
            contributing_record_ids=record_ids,
            conflict_log=tuple(conflict_log),
            field_provenance=provenance,
        )

    def _apply_strategy(
        self,
        records: list[dict[str, Any]],
        field_name: str,
        strategy: SurvivorshipStrategy,
        source_priority: tuple[str, ...],
        source_field: str,
        timestamp_field: str | None,
    ) -> tuple[dict[str, Any], Any]:
        """Return (winner_record, winning_value) for the given field."""
        candidates = [(r, r.get(field_name)) for r in records if r.get(field_name) is not None]
        if not candidates:
            return records[0], None

        if strategy == SurvivorshipStrategy.FIRST_NON_NULL:
            return self._first_non_null(candidates, source_priority, source_field)
        if strategy == SurvivorshipStrategy.SOURCE_PRIORITY:
            return self._source_priority_pick(candidates, source_priority, source_field)
        if strategy == SurvivorshipStrategy.MOST_RECENT:
            return self._most_recent(candidates, timestamp_field)
        if strategy == SurvivorshipStrategy.LONGEST:
            return self._longest(candidates)
        return candidates[0]

    @staticmethod
    def _first_non_null(
        candidates: list[tuple[dict[str, Any], Any]],
        source_priority: tuple[str, ...],
        source_field: str,
    ) -> tuple[dict[str, Any], Any]:
        if source_priority:
            for src in source_priority:
                for r, v in candidates:
                    if str(r.get(source_field, "")) == src:
                        return r, v
        return candidates[0]

    @staticmethod
    def _source_priority_pick(
        candidates: list[tuple[dict[str, Any], Any]],
        source_priority: tuple[str, ...],
        source_field: str,
    ) -> tuple[dict[str, Any], Any]:
        for src in source_priority:
            for r, v in candidates:
                if str(r.get(source_field, "")) == src:
                    return r, v
        return candidates[0]

    @staticmethod
    def _most_recent(
        candidates: list[tuple[dict[str, Any], Any]],
        timestamp_field: str | None,
    ) -> tuple[dict[str, Any], Any]:
        if not timestamp_field:
            return candidates[0]
        best_record, best_value = candidates[0]
        best_ts: datetime | None = _parse_ts(str(candidates[0][0].get(timestamp_field, "")))
        for r, v in candidates[1:]:
            ts = _parse_ts(str(r.get(timestamp_field, "")))
            if ts and (best_ts is None or ts > best_ts):
                best_ts = ts
                best_record, best_value = r, v
        return best_record, best_value

    @staticmethod
    def _longest(
        candidates: list[tuple[dict[str, Any], Any]],
    ) -> tuple[dict[str, Any], Any]:
        return max(candidates, key=lambda cv: len(str(cv[1])))


def _parse_ts(value: str) -> datetime | None:
    """Attempt to parse an ISO-8601 timestamp; return None on failure."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


class SurvivorshipError(Exception):
    """Raised when survivorship resolution encounters an unrecoverable error."""
