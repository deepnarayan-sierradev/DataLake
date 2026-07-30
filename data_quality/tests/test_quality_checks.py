"""Batch quality specification tests (DL-DQ-10 … DL-DQ-13)."""

from __future__ import annotations

from datetime import date

import pytest

from data_quality.exception_repository import ExceptionKind, ExceptionSeverity
from data_quality.quality_checks import (
    BatchCheckContext,
    CompletenessCheck,
    DateValidationCheck,
    DuplicateCheck,
    ForeignKeyDeclaration,
    ReferentialIntegrityCheck,
    evaluate_batch_checks,
)

_CONTEXT = BatchCheckContext(
    tenant_code="evive",
    run_id="run-20260728-120000000000-aaaaaaaa",
    entity_id="hubspot-company",
    correlation_id="corr-1",
    source_id="hubspot",
    connection_id="hubspot-grasons",
)


class TestCompletenessCheck:
    def test_fully_populated_passes(self):
        check = CompletenessCheck(required_fields=("name", "email"))
        records = [{"name": "A", "email": "a@x.test"}, {"name": "B", "email": "b@x.test"}]
        outcome = check.evaluate(records, _CONTEXT)
        assert outcome.passed is True
        assert outcome.measured_value == 100.0
        assert outcome.exceptions == ()

    def test_sparse_field_raises_an_exception(self):
        check = CompletenessCheck(required_fields=("email",), minimum_population_rate_pct=90.0)
        records = [{"email": "a@x.test"}, {"email": None}, {"email": ""}]
        outcome = check.evaluate(records, _CONTEXT)
        assert outcome.passed is False
        assert len(outcome.exceptions) == 1
        exception = outcome.exceptions[0]
        assert exception.kind is ExceptionKind.COMPLETENESS_BELOW_THRESHOLD
        assert exception.key_field_name == "email"
        assert exception.tenant_code == "evive"

    def test_worst_field_is_the_measured_value(self):
        check = CompletenessCheck(required_fields=("a", "b"), minimum_population_rate_pct=100.0)
        records = [{"a": 1, "b": 1}, {"a": 1, "b": None}]
        assert check.evaluate(records, _CONTEXT).measured_value == 50.0

    def test_empty_batch_passes_vacuously(self):
        check = CompletenessCheck(required_fields=("a",))
        assert check.evaluate([], _CONTEXT).passed is True


class TestDuplicateCheck:
    def test_unique_keys_pass(self):
        check = DuplicateCheck(natural_key_fields=("id",))
        outcome = check.evaluate([{"id": "1"}, {"id": "2"}], _CONTEXT)
        assert outcome.passed is True
        assert outcome.measured_value == 0.0

    def test_duplicates_are_reported_with_samples(self):
        check = DuplicateCheck(natural_key_fields=("id",), maximum_duplicate_rate_pct=0.0)
        outcome = check.evaluate([{"id": "1"}, {"id": "1"}, {"id": "2"}], _CONTEXT)
        assert outcome.passed is False
        assert outcome.exceptions[0].kind is ExceptionKind.DUPLICATE_RATE_EXCEEDED
        assert outcome.exceptions[0].sample_keys == ("1",)

    def test_composite_natural_key(self):
        check = DuplicateCheck(natural_key_fields=("brand", "id"), maximum_duplicate_rate_pct=0.0)
        records = [{"brand": "x", "id": "1"}, {"brand": "y", "id": "1"}]
        assert check.evaluate(records, _CONTEXT).passed is True

    def test_rate_within_tolerance_passes(self):
        check = DuplicateCheck(natural_key_fields=("id",), maximum_duplicate_rate_pct=50.0)
        assert check.evaluate([{"id": "1"}, {"id": "1"}], _CONTEXT).passed is True


class TestReferentialIntegrityCheck:
    def _check(self, **overrides) -> ReferentialIntegrityCheck:
        base = {
            "declaration": ForeignKeyDeclaration(
                child_field="company_id",
                parent_entity_type="company",
                parent_key_field="golden_id",
            ),
            "parent_keys": frozenset({"c1", "c2"}),
            "maximum_orphan_rate_pct": 0.0,
        }
        return ReferentialIntegrityCheck(**{**base, **overrides})

    def test_resolved_references_pass(self):
        outcome = self._check().evaluate([{"company_id": "c1"}, {"company_id": "c2"}], _CONTEXT)
        assert outcome.passed is True

    def test_orphans_are_reported(self):
        outcome = self._check().evaluate([{"company_id": "c9"}], _CONTEXT)
        assert outcome.passed is False
        assert outcome.exceptions[0].kind is ExceptionKind.REFERENTIAL_ORPHAN
        assert outcome.exceptions[0].sample_keys == ("c9",)

    def test_null_references_are_not_orphans(self):
        outcome = self._check().evaluate([{"company_id": None}, {"company_id": ""}], _CONTEXT)
        assert outcome.passed is True

    def test_orphan_rate_within_tolerance(self):
        check = self._check(maximum_orphan_rate_pct=60.0)
        outcome = check.evaluate([{"company_id": "c9"}, {"company_id": "c1"}], _CONTEXT)
        assert outcome.passed is True


class TestDateValidationCheck:
    def _check(self, **overrides) -> DateValidationCheck:
        base = {
            "date_fields": ("created_at",),
            "maximum_anomaly_rate_pct": 0.0,
            "_today": date(2026, 7, 28),
        }
        return DateValidationCheck(**{**base, **overrides})

    def test_recent_dates_pass(self):
        outcome = self._check().evaluate([{"created_at": "2026-07-01"}], _CONTEXT)
        assert outcome.passed is True

    def test_future_dates_are_flagged(self):
        outcome = self._check().evaluate([{"created_at": "2027-01-01"}], _CONTEXT)
        assert outcome.passed is False
        assert outcome.exceptions[0].kind is ExceptionKind.DATE_VALIDATION
        assert "future" in outcome.exceptions[0].sample_keys[0]

    def test_pre_epoch_dates_are_flagged(self):
        outcome = self._check().evaluate([{"created_at": "1970-01-01"}], _CONTEXT)
        assert outcome.passed is False
        assert "pre-epoch" in outcome.exceptions[0].sample_keys[0]

    def test_tomorrow_is_within_tolerance(self):
        outcome = self._check().evaluate([{"created_at": "2026-07-29"}], _CONTEXT)
        assert outcome.passed is True

    def test_unparseable_values_are_ignored(self):
        outcome = self._check().evaluate([{"created_at": "not-a-date"}], _CONTEXT)
        assert outcome.passed is True

    def test_iso_timestamps_with_zulu_suffix_parse(self):
        outcome = self._check().evaluate([{"created_at": "2027-01-01T00:00:00Z"}], _CONTEXT)
        assert outcome.passed is False


class TestBatchAggregate:
    def test_aggregate_collects_every_exception(self):
        checks = [
            CompletenessCheck(required_fields=("email",), minimum_population_rate_pct=100.0),
            DuplicateCheck(natural_key_fields=("id",), maximum_duplicate_rate_pct=0.0),
        ]
        records = [{"id": "1", "email": None}, {"id": "1", "email": "a@x.test"}]
        result = evaluate_batch_checks(checks, records, _CONTEXT)
        assert result.all_passed is False
        assert len(result.exceptions) == 2
        assert result.measured("duplicate-rate") == 50.0
        assert result.measured("not-a-rule") is None

    def test_all_severities_default_to_warn(self):
        checks = [CompletenessCheck(required_fields=("email",), minimum_population_rate_pct=100.0)]
        result = evaluate_batch_checks(checks, [{"email": None}], _CONTEXT)
        assert all(e.severity is ExceptionSeverity.WARN for e in result.exceptions)

    @pytest.mark.parametrize("records", [[], [{"id": "1", "email": "a@x.test"}]])
    def test_clean_batches_produce_no_exceptions(self, records):
        checks = [
            CompletenessCheck(required_fields=("email",)),
            DuplicateCheck(natural_key_fields=("id",)),
        ]
        assert evaluate_batch_checks(checks, records, _CONTEXT).exceptions == ()
