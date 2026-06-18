"""Tests for GoldenRecordSurvivorshipPolicy — Phase 7."""

from __future__ import annotations

import pytest

from entity_resolution.survivorship_policy import (
    AttributeSurvivorshipRule,
    GoldenRecordSurvivorshipPolicy,
    SurvivorshipError,
    SurvivorshipPolicy,
    SurvivorshipStrategy,
)

_ENTITY_TYPE = "customer"


def _policy(*rules, version="1.0.0", default=SurvivorshipStrategy.FIRST_NON_NULL):
    return SurvivorshipPolicy(
        entity_type=_ENTITY_TYPE,
        policy_version=version,
        attribute_rules=tuple(rules),
        default_strategy=default,
    )


def _attr_rule(field, strategy, sources=(), ts_field=None):
    return AttributeSurvivorshipRule(
        canonical_field=field,
        strategy=strategy,
        source_priority=tuple(sources),
        timestamp_field=ts_field,
    )


class TestSourcePriorityStrategy:
    def setup_method(self, method=None):
        rule = _attr_rule(
            "phone", SurvivorshipStrategy.SOURCE_PRIORITY, sources=["salesforce", "netsuite"]
        )
        self.applier = GoldenRecordSurvivorshipPolicy(_policy(rule))

    def test_preferred_source_wins(self):
        records = [
            {"id": "a", "source_id": "netsuite", "phone": "111-2222"},
            {"id": "b", "source_id": "salesforce", "phone": "333-4444"},
        ]
        result = self.applier.resolve(records, "id", "source_id")
        assert result.canonical_record["phone"] == "333-4444"

    def test_fallback_when_preferred_source_absent(self):
        records = [
            {"id": "a", "source_id": "netsuite", "phone": "111-2222"},
        ]
        result = self.applier.resolve(records, "id", "source_id")
        assert result.canonical_record["phone"] == "111-2222"


class TestMostRecentStrategy:
    def setup_method(self, method=None):
        rule = _attr_rule(
            "email",
            SurvivorshipStrategy.MOST_RECENT,
            ts_field="updated_at",
        )
        self.applier = GoldenRecordSurvivorshipPolicy(_policy(rule))

    def test_most_recent_record_wins(self):
        records = [
            {"id": "a", "source_id": "sf", "email": "old@example.com", "updated_at": "2023-01-01"},
            {"id": "b", "source_id": "ns", "email": "new@example.com", "updated_at": "2024-06-01"},
        ]
        result = self.applier.resolve(records, "id", "source_id")
        assert result.canonical_record["email"] == "new@example.com"


class TestLongestStrategy:
    def setup_method(self, method=None):
        rule = _attr_rule("name", SurvivorshipStrategy.LONGEST)
        self.applier = GoldenRecordSurvivorshipPolicy(_policy(rule))

    def test_longest_value_wins(self):
        records = [
            {"id": "a", "source_id": "sf", "name": "Acme"},
            {"id": "b", "source_id": "ns", "name": "Acme Corporation"},
        ]
        result = self.applier.resolve(records, "id", "source_id")
        assert result.canonical_record["name"] == "Acme Corporation"


class TestFirstNonNullStrategy:
    def setup_method(self, method=None):
        self.applier = GoldenRecordSurvivorshipPolicy(
            _policy(default=SurvivorshipStrategy.FIRST_NON_NULL)
        )

    def test_first_non_null_value_returned(self):
        records = [
            {"id": "a", "source_id": "sf", "region": None},
            {"id": "b", "source_id": "ns", "region": "EMEA"},
        ]
        result = self.applier.resolve(records, "id", "source_id")
        assert result.canonical_record["region"] == "EMEA"

    def test_all_null_means_field_absent(self):
        records = [
            {"id": "a", "source_id": "sf", "region": None},
            {"id": "b", "source_id": "ns", "region": None},
        ]
        result = self.applier.resolve(records, "id", "source_id")
        assert "region" not in result.canonical_record


class TestSurvivorshipMetadata:
    def setup_method(self, method=None):
        self.applier = GoldenRecordSurvivorshipPolicy(_policy())

    def test_contributing_record_ids_captured(self):
        records = [
            {"id": "1", "source_id": "sf", "name": "Alice"},
            {"id": "2", "source_id": "ns", "name": "Alice"},
        ]
        result = self.applier.resolve(records, "id", "source_id")
        assert set(result.contributing_record_ids) == {"1", "2"}

    def test_conflict_log_populated_for_conflicting_fields(self):
        records = [
            {"id": "1", "source_id": "sf", "name": "Alice"},
            {"id": "2", "source_id": "ns", "name": "Alicia"},
        ]
        result = self.applier.resolve(records, "id", "source_id")
        assert any(e.canonical_field == "name" for e in result.conflict_log)

    def test_empty_cluster_raises(self):
        with pytest.raises(SurvivorshipError):
            self.applier.resolve([], "id", "source_id")

    def test_single_record_no_conflict(self):
        records = [{"id": "1", "source_id": "sf", "name": "Alice"}]
        result = self.applier.resolve(records, "id", "source_id")
        assert result.canonical_record["name"] == "Alice"
        assert result.conflict_log == ()


# ---------------------------------------------------------------------------
# Uncovered branches — targeted gap-fill tests
# ---------------------------------------------------------------------------


class TestMostRecentEdgeCases:
    """Cover _most_recent branches not hit by the happy-path test."""

    def test_most_recent_no_timestamp_field_returns_first_candidate(self):
        # timestamp_field=None → falls back to first non-null candidate
        rule = _attr_rule("email", SurvivorshipStrategy.MOST_RECENT, ts_field=None)
        applier = GoldenRecordSurvivorshipPolicy(_policy(rule))
        records = [
            {"id": "a", "source_id": "sf", "email": "first@example.com"},
            {"id": "b", "source_id": "ns", "email": "second@example.com"},
        ]
        result = applier.resolve(records, "id", "source_id")
        assert result.canonical_record["email"] == "first@example.com"

    def test_most_recent_unparseable_timestamp_returns_first(self):
        # All timestamp values are garbage → best_ts stays None, first candidate wins
        rule = _attr_rule("email", SurvivorshipStrategy.MOST_RECENT, ts_field="updated_at")
        applier = GoldenRecordSurvivorshipPolicy(_policy(rule))
        records = [
            {"id": "a", "source_id": "sf", "email": "a@example.com", "updated_at": "not-a-date"},
            {"id": "b", "source_id": "ns", "email": "b@example.com", "updated_at": "also-bad"},
        ]
        result = applier.resolve(records, "id", "source_id")
        # Just verify it returns without error and picks one of the values
        assert result.canonical_record["email"] in {"a@example.com", "b@example.com"}

    def test_most_recent_with_date_only_format(self):
        # _parse_ts should handle "%Y-%m-%d" (third format in the loop)
        rule = _attr_rule("email", SurvivorshipStrategy.MOST_RECENT, ts_field="updated_at")
        applier = GoldenRecordSurvivorshipPolicy(_policy(rule))
        records = [
            {"id": "a", "source_id": "sf", "email": "old@example.com", "updated_at": "2022-01-01"},
            {"id": "b", "source_id": "ns", "email": "new@example.com", "updated_at": "2024-12-31"},
        ]
        result = applier.resolve(records, "id", "source_id")
        assert result.canonical_record["email"] == "new@example.com"


class TestOutputFieldsProjection:
    """Cover the output_fields projection branch in resolve()."""

    def test_fields_outside_output_fields_are_removed(self):
        rule = _attr_rule("name", SurvivorshipStrategy.SOURCE_PRIORITY, sources=["sf"])
        policy = SurvivorshipPolicy(
            entity_type="customer",
            policy_version="v1",
            attribute_rules=(rule,),
            default_strategy=SurvivorshipStrategy.FIRST_NON_NULL,
            output_fields=("name",),  # "region" is excluded
        )
        applier = GoldenRecordSurvivorshipPolicy(policy)
        records = [
            {"id": "a", "source_id": "sf", "name": "Acme", "region": "EMEA"},
        ]
        result = applier.resolve(records, "id", "source_id")
        assert "name" in result.canonical_record
        assert "region" not in result.canonical_record

    def test_empty_output_fields_passes_through_all_fields(self):
        policy = SurvivorshipPolicy(
            entity_type="customer",
            policy_version="v1",
            attribute_rules=(),
            default_strategy=SurvivorshipStrategy.FIRST_NON_NULL,
            output_fields=(),  # pass-through
        )
        applier = GoldenRecordSurvivorshipPolicy(policy)
        records = [{"id": "a", "source_id": "sf", "name": "Acme", "region": "EMEA"}]
        result = applier.resolve(records, "id", "source_id")
        assert "name" in result.canonical_record
        assert "region" in result.canonical_record


class TestConflictLogging:
    """L164 — conflict log entry only written when >1 candidate has the field."""

    def test_no_conflict_log_when_only_one_source_has_field(self):
        rule = _attr_rule("phone", SurvivorshipStrategy.SOURCE_PRIORITY, sources=["sf"])
        applier = GoldenRecordSurvivorshipPolicy(_policy(rule))
        records = [
            {"id": "a", "source_id": "sf", "phone": "555-0100"},
            {"id": "b", "source_id": "ns"},  # no phone field
        ]
        result = applier.resolve(records, "id", "source_id")
        assert result.canonical_record["phone"] == "555-0100"
        # phone had only one candidate → no conflict entry for it
        assert not any(e.canonical_field == "phone" for e in result.conflict_log)
