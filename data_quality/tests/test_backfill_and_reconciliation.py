"""Backfill orchestrator, reconciliation, exception store, brand registry, dictionary."""

from __future__ import annotations

from datetime import date

import boto3
import pytest
from moto import mock_aws

from config_propagation.capability import ConfigCapability
from data_quality.backfill_orchestrator import (
    BackfillJobNotFoundError,
    BackfillOrchestrator,
    ChunkState,
    JobState,
    derive_chunk_days,
    plan_chunks,
)
from data_quality.brand_registry import (
    EVIVE_BRANDS,
    Brand,
    BrandRegistry,
    UnknownBrandError,
    stamp_brand,
    validate_brand_code,
)
from data_quality.data_dictionary import (
    FieldProvenanceEntry,
    build_data_dictionary,
    build_field_provenance,
    build_golden_id_assignment,
    data_dictionary_s3_key,
    publish_data_dictionary,
)
from data_quality.exception_repository import (
    DataQualityExceptionRepository,
    ExceptionKind,
    ExceptionSeverity,
    QualityException,
    ResolutionState,
)
from data_quality.quality_policy_repository import (
    PolicyEnforcementMode,
    QualityGateBlockedError,
    QualityPolicyAttachment,
    QualityPolicyNotAttachedError,
    QualityPolicyRepository,
    enforce_quality_gate,
    evaluate_quality_gate,
    require_quality_policy,
)
from data_quality.reconciliation import (
    ComparatorKind,
    CountComparator,
    DataLayerName,
    LayerMeasurement,
    MonetarySumComparator,
    ReconciliationReport,
    ReconciliationReportRepository,
    ReconciliationVerdict,
    WatermarkBoundsComparator,
    compare_key_fields,
    deterministic_sample,
    reconcile,
)

_REGION = "us-east-1"
_RUN = "run-20260728-120000000000-aaaaaaaa"


def _table(name: str, pk: str, sk: str | None = None) -> None:
    key_schema = [{"AttributeName": pk, "KeyType": "HASH"}]
    attributes = [{"AttributeName": pk, "AttributeType": "S"}]
    if sk:
        key_schema.append({"AttributeName": sk, "KeyType": "RANGE"})
        attributes.append({"AttributeName": sk, "AttributeType": "S"})
    boto3.client("dynamodb", region_name=_REGION).create_table(
        TableName=name,
        KeySchema=key_schema,
        AttributeDefinitions=attributes,
        BillingMode="PAY_PER_REQUEST",
    )


class TestChunkPlanning:
    def test_chunk_days_scale_with_throughput(self):
        slow = derive_chunk_days(rows_per_second=10, estimated_rows_per_day=10_000)
        fast = derive_chunk_days(rows_per_second=5_000, estimated_rows_per_day=10_000)
        assert slow < fast

    def test_chunk_days_are_bounded(self):
        assert derive_chunk_days(1, 1_000_000) == 1
        assert derive_chunk_days(100_000, 1) == 90

    def test_unknown_volume_uses_the_widest_chunk(self):
        assert derive_chunk_days(500, 0) == 90

    def test_zero_throughput_is_rejected(self):
        with pytest.raises(ValueError, match="must be positive"):
            derive_chunk_days(0, 100)

    def test_chunks_are_contiguous_and_non_overlapping(self):
        chunks = plan_chunks(date(2026, 1, 1), date(2026, 1, 10), 3)
        assert [(c.window_start, c.window_end) for c in chunks] == [
            (date(2026, 1, 1), date(2026, 1, 3)),
            (date(2026, 1, 4), date(2026, 1, 6)),
            (date(2026, 1, 7), date(2026, 1, 9)),
            (date(2026, 1, 10), date(2026, 1, 10)),
        ]

    def test_reversed_window_is_rejected(self):
        with pytest.raises(ValueError, match="must not precede"):
            plan_chunks(date(2026, 2, 1), date(2026, 1, 1), 1)

    def test_unbounded_job_is_refused(self):
        with pytest.raises(ValueError, match="exceed"):
            plan_chunks(date(1900, 1, 1), date(2026, 1, 1), 1)


@mock_aws
class TestBackfillOrchestrator:
    def _orchestrator(self) -> BackfillOrchestrator:
        _table("EdlBackfillJob", "tenant_code", "job_key")
        return BackfillOrchestrator(environment="dev", region_name=_REGION)

    def _job(self, orchestrator: BackfillOrchestrator, **overrides):
        base = {
            "tenant_code": "evive",
            "entity_id": "hubspot-company",
            "window_start": date(2026, 1, 1),
            "window_end": date(2026, 1, 6),
            "estimated_rows_per_day": 100_000,
            "observed_rows_per_second": 500,
        }
        return orchestrator.plan_job(**{**base, **overrides})

    def test_plan_persists_chunks_and_resume_pointer(self):
        orchestrator = self._orchestrator()
        job = self._job(orchestrator)
        assert job.state is JobState.PLANNED
        assert job.resume_pointer == 0
        reloaded = orchestrator.load_job("evive", "hubspot-company", job.job_id)
        assert len(reloaded.chunks) == len(job.chunks)

    def test_resume_never_restarts_from_zero(self):
        orchestrator = self._orchestrator()
        job = self._job(orchestrator)
        orchestrator.mark_chunk_running("evive", "hubspot-company", job.job_id, 0)
        orchestrator.complete_chunk("evive", "hubspot-company", job.job_id, 0, 1_000)
        nxt = orchestrator.next_chunk("evive", "hubspot-company", job.job_id)
        assert nxt is not None
        assert nxt.sequence == 1

    def test_completing_the_last_chunk_completes_the_job(self):
        orchestrator = self._orchestrator()
        job = self._job(orchestrator, window_end=date(2026, 1, 1))
        orchestrator.complete_chunk("evive", "hubspot-company", job.job_id, 0, 500)
        assert orchestrator.next_chunk("evive", "hubspot-company", job.job_id) is None
        assert orchestrator.load_job("evive", "hubspot-company", job.job_id).state is (
            JobState.COMPLETED
        )

    def test_completion_is_idempotent_on_replay(self):
        orchestrator = self._orchestrator()
        job = self._job(orchestrator)
        orchestrator.complete_chunk("evive", "hubspot-company", job.job_id, 0, 1_000)
        again = orchestrator.complete_chunk("evive", "hubspot-company", job.job_id, 0, 9_999)
        assert again.chunk(0).rows_processed == 1_000

    def test_failed_chunk_is_resumable(self):
        orchestrator = self._orchestrator()
        job = self._job(orchestrator)
        orchestrator.fail_chunk("evive", "hubspot-company", job.job_id, 0, "source timeout")
        reloaded = orchestrator.load_job("evive", "hubspot-company", job.job_id)
        assert reloaded.chunk(0).state is ChunkState.FAILED
        assert reloaded.resume_pointer == 0

    def test_compensation_clears_partial_state(self):
        s3 = boto3.client("s3", region_name=_REGION)
        s3.create_bucket(Bucket="raw-bucket")
        s3.put_object(Bucket="raw-bucket", Key="evive/hubspot/x/data.parquet", Body=b"x")
        _table("EdlBackfillJob", "tenant_code", "job_key")
        orchestrator = BackfillOrchestrator(
            environment="dev", region_name=_REGION, s3_client=s3, raw_s3_bucket="raw-bucket"
        )
        job = orchestrator.plan_job(
            tenant_code="evive",
            entity_id="hubspot-company",
            window_start=date(2026, 1, 1),
            window_end=date(2026, 1, 1),
        )
        orchestrator.complete_chunk(
            "evive", "hubspot-company", job.job_id, 0, 10, "evive/hubspot/x/"
        )
        compensated = orchestrator.compensate_chunk("evive", "hubspot-company", job.job_id, 0)
        assert compensated.chunk(0).state is ChunkState.COMPENSATED
        assert compensated.chunk(0).rows_processed == 0
        assert s3.list_objects_v2(Bucket="raw-bucket", Prefix="evive/").get("KeyCount", 0) == 0

    def test_cancellation_stops_remaining_chunks(self):
        orchestrator = self._orchestrator()
        job = self._job(orchestrator)
        cancelled = orchestrator.cancel_job("evive", "hubspot-company", job.job_id)
        assert cancelled.state is JobState.CANCELLED
        with pytest.raises(Exception, match="cancelled"):
            orchestrator.next_chunk("evive", "hubspot-company", job.job_id)

    def test_reprocess_requires_a_pinned_version(self):
        orchestrator = self._orchestrator()
        with pytest.raises(ValueError, match="pinned to the new configuration"):
            self._job(
                orchestrator,
                reprocess_reason="survivorship rule change",
                reprocess_capability=ConfigCapability.SURVIVORSHIP,
            )

    def test_reprocess_job_round_trips_its_pin(self):
        orchestrator = self._orchestrator()
        job = self._job(
            orchestrator,
            reprocess_reason="survivorship rule change",
            reprocess_capability=ConfigCapability.SURVIVORSHIP,
            pinned_config_version="v3",
        )
        reloaded = orchestrator.load_job("evive", "hubspot-company", job.job_id)
        assert reloaded.is_reprocess is True
        assert reloaded.pinned_config_version == "v3"
        assert reloaded.reprocess_capability is ConfigCapability.SURVIVORSHIP

    def test_throughput_feedback_persists(self):
        orchestrator = self._orchestrator()
        job = self._job(orchestrator)
        orchestrator.record_throughput("evive", "hubspot-company", job.job_id, 1_234.0)
        assert (
            orchestrator.load_job("evive", "hubspot-company", job.job_id).observed_rows_per_second
            == 1_234
        )

    def test_missing_job_raises(self):
        orchestrator = self._orchestrator()
        with pytest.raises(BackfillJobNotFoundError):
            orchestrator.load_job("evive", "hubspot-company", "bfj-nope")

    def test_list_jobs_filters_by_entity(self):
        orchestrator = self._orchestrator()
        self._job(orchestrator)
        self._job(orchestrator, entity_id="hubspot-deal")
        assert len(orchestrator.list_jobs("evive")) == 2
        assert len(orchestrator.list_jobs("evive", "hubspot-deal")) == 1


class TestReconciliationComparators:
    def _pair(self, expected: str, observed: str) -> tuple[LayerMeasurement, LayerMeasurement]:
        return (
            LayerMeasurement(DataLayerName.SOURCE, expected),
            LayerMeasurement(DataLayerName.ANALYTICS, observed),
        )

    def test_exact_count_match(self):
        result = CountComparator().compare(*self._pair("100", "100"), "row_count")
        assert result.verdict is ReconciliationVerdict.MATCHED
        assert result.is_finding is False

    def test_count_within_tolerance(self):
        result = CountComparator(tolerance_pct=1.0).compare(*self._pair("1000", "1005"), "rows")
        assert result.verdict is ReconciliationVerdict.WITHIN_TOLERANCE

    def test_count_variance_is_a_finding(self):
        result = CountComparator(tolerance_pct=0.1).compare(*self._pair("1000", "1100"), "rows")
        assert result.verdict is ReconciliationVerdict.VARIANCE
        assert result.is_finding is True

    def test_monetary_sum_has_zero_tolerance_by_default(self):
        result = MonetarySumComparator().compare(*self._pair("1000.00", "1000.01"), "revenue")
        assert result.verdict is ReconciliationVerdict.VARIANCE
        assert "difference=0.01" in result.detail

    def test_monetary_sum_uses_decimal_arithmetic(self):
        # 0.1 + 0.2 in float is 0.30000000000000004; Decimal keeps it exact.
        result = MonetarySumComparator().compare(*self._pair("0.30", "0.30"), "revenue")
        assert result.verdict is ReconciliationVerdict.MATCHED

    def test_non_numeric_measurement_is_not_comparable(self):
        result = CountComparator().compare(*self._pair("n/a", "100"), "rows")
        assert result.verdict is ReconciliationVerdict.NOT_COMPARABLE

    def test_zero_expected_with_nonzero_observed_is_full_variance(self):
        result = CountComparator().compare(*self._pair("0", "5"), "rows")
        assert result.variance_pct == 100.0

    def test_watermark_bounds_compare_exactly(self):
        matched = WatermarkBoundsComparator().compare(
            *self._pair("2026-01-31T23:59:59", "2026-01-31T23:59:59"), "max_watermark"
        )
        mismatched = WatermarkBoundsComparator().compare(
            *self._pair("2026-01-31T23:59:59", "2026-01-30T00:00:00"), "max_watermark"
        )
        assert matched.verdict is ReconciliationVerdict.MATCHED
        assert mismatched.verdict is ReconciliationVerdict.VARIANCE


class TestSampling:
    def test_sampling_is_deterministic(self):
        records = [{"id": str(i)} for i in range(500)]
        first = deterministic_sample(records, "id", modulo=10)
        second = deterministic_sample(records, "id", modulo=10)
        assert [r["id"] for r in first] == [r["id"] for r in second]
        assert 0 < len(first) < len(records)

    def test_modulo_one_samples_everything(self):
        records = [{"id": str(i)} for i in range(5)]
        assert len(deterministic_sample(records, "id", modulo=1)) == 5

    def test_invalid_modulo_is_rejected(self):
        with pytest.raises(ValueError, match="at least 1"):
            deterministic_sample([], "id", modulo=0)

    def test_field_match_rates(self):
        source = [{"id": "1", "name": "Acme"}, {"id": "2", "name": "Beta"}]
        curated = [{"id": "1", "name": "Acme"}, {"id": "2", "name": "beta"}]
        rates = compare_key_fields(source, curated, "id", ["name"])
        assert rates[0].compared == 2
        assert rates[0].matched == 1
        assert rates[0].match_rate_pct == 50.0

    def test_unmatched_keys_are_skipped_not_counted_as_mismatch(self):
        source = [{"id": "1", "name": "Acme"}, {"id": "9", "name": "Ghost"}]
        curated = [{"id": "1", "name": "Acme"}]
        rates = compare_key_fields(source, curated, "id", ["name"])
        assert rates[0].compared == 1
        assert rates[0].match_rate_pct == 100.0

    def test_normalisation_trims_whitespace(self):
        rates = compare_key_fields(
            [{"id": "1", "name": " Acme "}], [{"id": "1", "name": "Acme"}], "id", ["name"]
        )
        assert rates[0].matched == 1


@mock_aws
class TestReconciliationReport:
    def _report(self, observed: str = "1000") -> ReconciliationReport:
        return reconcile(
            tenant_code="evive",
            entity_id="sage-intacct-arinvoice",
            period="2026-01",
            run_id=_RUN,
            measurements={
                ComparatorKind.COUNT: (
                    LayerMeasurement(DataLayerName.SOURCE, "100"),
                    LayerMeasurement(DataLayerName.ANALYTICS, "100"),
                    "invoice_count",
                ),
                ComparatorKind.MONETARY_SUM: (
                    LayerMeasurement(DataLayerName.SOURCE, "1000"),
                    LayerMeasurement(DataLayerName.ANALYTICS, observed),
                    "revenue",
                ),
            },
        )

    def test_matched_report_has_no_findings(self):
        report = self._report()
        assert report.matched is True
        assert report.findings == ()
        assert report.worst_variance_pct == 0.0

    def test_financial_variance_is_a_finding(self):
        report = self._report(observed="1100")
        assert report.matched is False
        assert report.findings[0].comparator is ComparatorKind.MONETARY_SUM

    def test_signature_is_stable_and_content_bound(self):
        assert self._report().signature() == self._report().signature()
        assert self._report().signature() != self._report(observed="1100").signature()

    def test_report_persists_with_its_signature(self):
        _table("EdlReconciliationReport", "tenant_code", "report_key")
        repository = ReconciliationReportRepository(environment="dev", region_name=_REGION)
        report = self._report()
        signature = repository.save(report)
        stored = repository.list_for_entity("evive", "sage-intacct-arinvoice")
        assert len(stored) == 1
        assert stored[0]["signature"] == signature
        assert stored[0]["matched"] is True


@mock_aws
class TestExceptionRepository:
    def _repository(self) -> DataQualityExceptionRepository:
        _table("EdlDataQualityException", "tenant_code", "exception_key")
        return DataQualityExceptionRepository(environment="dev", region_name=_REGION)

    def _exception(self, **overrides) -> QualityException:
        base = {
            "tenant_code": "evive",
            "run_id": _RUN,
            "rule_id": "completeness",
            "entity_id": "hubspot-company",
            "kind": ExceptionKind.QUALITY_VIOLATION,
            "severity": ExceptionSeverity.ERROR,
            "message": "email missing",
            "correlation_id": "corr-1",
        }
        return QualityException(**{**base, **overrides})

    def test_record_and_read_back(self):
        repository = self._repository()
        key = repository.record(self._exception())
        records = repository.list_for_run("evive", _RUN)
        assert len(records) == 1
        assert records[0]["exception_key"] == key
        assert records[0]["resolution_state"] == ResolutionState.OPEN.value

    def test_samples_are_redacted_by_default(self):
        repository = self._repository()
        repository.record(self._exception(sample_keys=("customer-000123",)))
        stored = repository.list_for_run("evive", _RUN)[0]
        assert stored["sample_keys"] != ["customer-000123"]
        assert stored["sample_keys"][0].startswith("cu")

    def test_batch_write_assigns_sequences(self):
        repository = self._repository()
        assert repository.record_many([self._exception(), self._exception()]) == 2
        keys = {r["exception_key"] for r in repository.list_for_run("evive", _RUN)}
        assert len(keys) == 2

    def test_blocking_exceptions_filter_by_severity(self):
        repository = self._repository()
        repository.record_many(
            [self._exception(), self._exception(severity=ExceptionSeverity.WARN)]
        )
        assert len(repository.blocking_exceptions("evive", _RUN)) == 1

    def test_open_listing_excludes_terminal_states(self):
        repository = self._repository()
        key = repository.record(self._exception())
        assert len(repository.list_open("evive")) == 1
        repository.transition("evive", key, ResolutionState.RESOLVED, resolution_note="fixed")
        assert repository.list_open("evive") == []

    def test_terminal_transition_requires_a_note(self):
        repository = self._repository()
        key = repository.record(self._exception())
        with pytest.raises(ValueError, match="requires a resolution note"):
            repository.transition("evive", key, ResolutionState.CLOSED)

    def test_assignment_transition_needs_no_note(self):
        repository = self._repository()
        key = repository.record(self._exception())
        repository.transition("evive", key, ResolutionState.ASSIGNED, assignee="ops@example.test")
        assert repository.list_open("evive")[0]["assignee"] == "ops@example.test"


@mock_aws
class TestQualityPolicyGate:
    def _repository(self) -> QualityPolicyRepository:
        _table("EdlQualityPolicyAttachment", "tenant_code", "entity_id")
        return QualityPolicyRepository(environment="dev", region_name=_REGION)

    def _attachment(self, **overrides) -> QualityPolicyAttachment:
        base = {
            "tenant_code": "evive",
            "entity_id": "hubspot-company",
            "policy_id": "crm-baseline",
            "policy_version": "v1",
            "required_fields": ("name",),
        }
        return QualityPolicyAttachment(**{**base, **overrides})

    def test_attach_and_read_back(self):
        repository = self._repository()
        repository.attach(self._attachment())
        loaded = repository.get("evive", "hubspot-company")
        assert loaded is not None
        assert loaded.policy_id == "crm-baseline"
        assert loaded.required_fields == ("name",)

    def test_missing_policy_blocks_promotion(self):
        repository = self._repository()
        with pytest.raises(QualityPolicyNotAttachedError, match="no attached quality policy"):
            require_quality_policy(repository, "evive", "hubspot-company")

    def test_present_policy_permits_promotion(self):
        repository = self._repository()
        repository.attach(self._attachment())
        assert require_quality_policy(repository, "evive", "hubspot-company").policy_id == (
            "crm-baseline"
        )

    def test_empty_policy_id_is_rejected(self):
        with pytest.raises(ValueError, match="policy_id must not be empty"):
            self._attachment(policy_id="")

    def test_list_attachments(self):
        repository = self._repository()
        repository.attach(self._attachment())
        repository.attach(self._attachment(entity_id="hubspot-deal"))
        assert len(repository.list_attachments("evive")) == 2

    def test_error_severity_blocks_by_default(self):
        verdict = evaluate_quality_gate(self._attachment(), [ExceptionSeverity.ERROR])
        assert verdict.permitted is False
        assert verdict.dlq_reason_code == "quality_gate_blocked"
        with pytest.raises(QualityGateBlockedError):
            enforce_quality_gate(self._attachment(), [ExceptionSeverity.ERROR])

    def test_warn_publishes_and_alerts(self):
        verdict = evaluate_quality_gate(self._attachment(), [ExceptionSeverity.WARN])
        assert verdict.permitted is True
        assert verdict.warning_count == 1

    def test_warn_mode_publishes_despite_errors(self):
        attachment = self._attachment(enforcement_mode=PolicyEnforcementMode.WARN)
        assert evaluate_quality_gate(attachment, [ExceptionSeverity.ERROR]).permitted is True


@mock_aws
class TestBrandRegistry:
    def _registry(self) -> BrandRegistry:
        _table("EdlBrandRegistry", "tenant_code", "brand_code")
        return BrandRegistry(environment="dev", region_name=_REGION)

    def test_customer_brands_register(self):
        registry = self._registry()
        for code, name in EVIVE_BRANDS:
            registry.register(Brand(tenant_code="evive", brand_code=code, display_name=name))
        assert len(registry.list_brands("evive")) == len(EVIVE_BRANDS)

    def test_unknown_brand_is_rejected(self):
        registry = self._registry()
        registry.register(Brand(tenant_code="evive", brand_code="shine", display_name="Shine"))
        with pytest.raises(UnknownBrandError, match="has not registered"):
            registry.validate_record_brand("evive", "not-a-brand")

    def test_registered_brand_is_accepted(self):
        registry = self._registry()
        registry.register(Brand(tenant_code="evive", brand_code="shine", display_name="Shine"))
        assert registry.validate_record_brand("evive", "Shine".lower()) == "shine"

    def test_absent_brand_is_permitted(self):
        registry = self._registry()
        assert registry.validate_record_brand("evive", None) is None

    def test_cache_invalidates_on_register(self):
        registry = self._registry()
        assert registry.known_brand_codes("evive") == frozenset()
        registry.register(Brand(tenant_code="evive", brand_code="shine", display_name="Shine"))
        assert registry.known_brand_codes("evive") == frozenset({"shine"})
        registry.invalidate("evive")
        assert registry.known_brand_codes("evive") == frozenset({"shine"})

    def test_malformed_brand_code_is_rejected(self):
        with pytest.raises(ValueError, match="brand code format"):
            validate_brand_code("Maid_Brigade")

    def test_stamp_does_not_mutate_the_input(self):
        record = {"id": "1"}
        stamped = stamp_brand(record, "shine")
        assert stamped["brand_code"] == "shine"
        assert "brand_code" not in record


@mock_aws
class TestDataDictionary:
    _MAPPING = {
        "rule_set_version": "v2",
        "rules": [
            {
                "canonical_field": "account_id",
                "source_field": "Id",
                "target_type": "string",
                "description": "Native source identifier.",
            },
            {"canonical_field": "account_name", "source_field": "Name"},
        ],
    }
    _SURVIVORSHIP = {
        "policy_version": "v2",
        "attribute_rules": [
            {
                "canonical_field": "account_name",
                "strategy": "source_priority",
                "source_priority": ["salesforce", "sage"],
            }
        ],
    }

    def _dictionary(self):
        return build_data_dictionary(
            tenant_code="evive",
            entity_id="salesforce-account",
            entity_type="company",
            mapping_config=self._MAPPING,
            survivorship_config=self._SURVIVORSHIP,
            classification_by_field={"account_name": "pii"},
        )

    def test_generated_from_config_not_written(self):
        dictionary = self._dictionary()
        assert dictionary.version == "v2"
        assert [f.canonical_field for f in dictionary.fields] == ["account_id", "account_name"]
        assert dictionary.fields[1].classification == "pii"

    def test_markdown_includes_survivorship_rules(self):
        rendered = self._dictionary().render_markdown()
        assert "## Conflict resolution (survivorship)" in rendered
        assert "salesforce, sage" in rendered

    def test_content_hash_changes_with_content(self):
        other = build_data_dictionary(
            tenant_code="evive",
            entity_id="salesforce-account",
            entity_type="company",
            mapping_config={"rule_set_version": "v2", "rules": []},
        )
        assert self._dictionary().content_hash() != other.content_hash()

    def test_key_is_tenant_prefixed(self):
        assert data_dictionary_s3_key("evive", "salesforce-account", "v2") == (
            "evive/data-dictionary/salesforce-account/v2.md"
        )

    def test_publish_writes_to_s3(self):
        s3 = boto3.client("s3", region_name=_REGION)
        s3.create_bucket(Bucket="curated-bucket")
        key = publish_data_dictionary(s3, "curated-bucket", self._dictionary())
        body = s3.get_object(Bucket="curated-bucket", Key=key)["Body"].read().decode()
        assert "# Data dictionary" in body


class TestProvenanceAndGoldenIds:
    def test_provenance_carries_the_rule_id(self):
        payload = build_field_provenance(
            (
                FieldProvenanceEntry(
                    canonical_field="account_name",
                    winning_source_id="salesforce",
                    rule_id="source-priority-1",
                    strategy="source_priority",
                    contributing_source_ids=("salesforce", "sage"),
                ),
            )
        )
        assert '"rule_id":"source-priority-1"' in payload
        assert '"source_id":"salesforce"' in payload

    def test_assignment_records_history(self):
        assignment = build_golden_id_assignment(
            tenant_code="evive",
            entity_type="company",
            golden_id="g-1",
            contributing_record_ids=("r1", "r2"),
            match_run_id=_RUN,
        )
        assert assignment["operation"] == "assign"
        assert assignment["contributing_record_ids"] == ["r1", "r2"]

    def test_merge_requires_previous_ids_so_it_is_reversible(self):
        with pytest.raises(ValueError, match="must name the previous golden ids"):
            build_golden_id_assignment(
                tenant_code="evive",
                entity_type="company",
                golden_id="g-1",
                contributing_record_ids=("r1",),
                match_run_id=_RUN,
                operation="merge",
            )

    def test_merge_with_previous_ids_is_accepted(self):
        assignment = build_golden_id_assignment(
            tenant_code="evive",
            entity_type="company",
            golden_id="g-3",
            contributing_record_ids=("r1", "r2"),
            match_run_id=_RUN,
            operation="merge",
            previous_golden_ids=("g-1", "g-2"),
        )
        assert assignment["previous_golden_ids"] == ["g-1", "g-2"]

    def test_unknown_operation_is_rejected(self):
        with pytest.raises(ValueError, match="must be one of"):
            build_golden_id_assignment(
                tenant_code="evive",
                entity_type="company",
                golden_id="g-1",
                contributing_record_ids=(),
                match_run_id=_RUN,
                operation="obliterate",
            )
