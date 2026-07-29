"""Joins, time grain, filters, derived metrics, lineage, governance, cache (DL-03)."""

from __future__ import annotations

from datetime import date

import boto3
import pytest
from moto import mock_aws

from semantic.enterprise_model import (
    SOW_KPI_MAP,
    TAG_EXECUTIVE,
    TAG_FINANCE,
    build_enterprise_model,
    expand_access_tags,
    sign_metric_definition,
)
from semantic.fiscal_calendar import FiscalCalendar, truncation_sql
from semantic.kpi_validation import (
    KpiCheckOutcome,
    KpiExpectation,
    KpiValidationHarness,
    structural_expectations,
)
from semantic.metric_lineage import (
    all_metric_lineage,
    build_methodology_document,
    methodology_s3_key,
    metric_lineage,
    metrics_touching_column,
)
from semantic.model_governance import (
    MakerCheckerViolationError,
    ModelIntegrityError,
    ModelStatus,
    ModelValidationError,
    ModelVersionNotFoundError,
    SemanticModelGovernance,
    validate_model,
)
from semantic.query_compiler import (
    AccessDeniedError,
    QueryCompiler,
    RelativeDateRange,
    SemanticFilter,
    SemanticQueryError,
    SemanticQueryRequest,
    TimeRangeFilter,
)
from semantic.result_cache import ResultCacheKey, SemanticResultCache, scope_signature
from semantic.semantic_model import (
    Dimension,
    JoinKind,
    Metric,
    MetricKind,
    NullDenominatorBehaviour,
    SemanticEntity,
    SemanticJoin,
    SemanticModel,
    TimeComparison,
    TimeDimension,
    TimeGrain,
)
from tenancy.scope_contract import PartitionKind, PartitionModel, ScopeUnit, TenantPartitionProfile
from tenancy.scope_predicate import ConsumptionSurface, build_scope_claims, scope_predicate

_REGION = "us-east-1"
_ALL_TAGS = frozenset({TAG_FINANCE, TAG_EXECUTIVE, "dept_operations", "dept_sales_marketing"})


def _model() -> SemanticModel:
    orders = SemanticEntity(
        name="sales_order",
        entity_type="sales_order",
        business_owner="role:cfo",
        dimensions=(
            Dimension(name="order_status", column="order_status", business_owner="role:cfo"),
            Dimension(name="scope_unit_id", column="scope_unit_id", business_owner="role:cfo"),
        ),
        time_dimensions=(
            TimeDimension(name="booked_date", column="booked_date", grain=TimeGrain.DAY),
        ),
        metrics=(
            Metric(
                name="sales",
                aggregation="sum",
                column="gross_amount",
                business_owner="role:cfo",
                definition="Gross booked value.",
                access_tag=TAG_FINANCE,
            ),
            Metric(
                name="order_count",
                aggregation="count_distinct",
                column="order_id",
                business_owner="role:cfo",
                definition="Distinct orders.",
            ),
            Metric(
                name="average_order_value",
                aggregation="sum",
                column="*",
                kind=MetricKind.RATIO,
                numerator_metric="sales",
                denominator_metric="order_count",
                business_owner="role:cfo",
                definition="Sales over orders.",
                access_tag=TAG_FINANCE,
            ),
            Metric(
                name="margin",
                aggregation="sum",
                column="*",
                kind=MetricKind.DIFFERENCE,
                numerator_metric="sales",
                denominator_metric="order_count",
                business_owner="role:cfo",
                definition="Illustrative difference metric.",
            ),
        ),
        joins=(
            SemanticJoin(
                target_entity="franchisee",
                kind=JoinKind.LEFT,
                local_column="scope_unit_id",
                target_column="scope_unit_id",
            ),
        ),
    )
    franchisee = SemanticEntity(
        name="franchisee",
        entity_type="franchisee",
        business_owner="role:vp-franchise-development",
        dimensions=(
            Dimension(
                name="franchisee_name",
                column="franchisee_name",
                business_owner="role:vp-franchise-development",
            ),
            Dimension(
                name="scope_unit_id",
                column="scope_unit_id",
                business_owner="role:vp-franchise-development",
            ),
        ),
    )
    return SemanticModel(
        tenant_code="evive",
        model_version="v1",
        entities=(orders, franchisee),
        fiscal_year_start_month=4,
    )


_COMPILER = QueryCompiler(_model())


class TestModelValidation:
    def test_duplicate_join_target_is_ambiguous_and_rejected(self):
        join = SemanticJoin(
            target_entity="franchisee", local_column="scope_unit_id", target_column="scope_unit_id"
        )
        with pytest.raises(ValueError, match="ambiguous join path"):
            SemanticEntity(name="x-entity", entity_type="x_entity", joins=(join, join))

    def test_join_to_an_unknown_entity_is_rejected_at_model_level(self):
        entity = SemanticEntity(
            name="orphan",
            entity_type="orphan",
            joins=(
                SemanticJoin(
                    target_entity="nowhere", local_column="a_column", target_column="b_column"
                ),
            ),
        )
        with pytest.raises(ValueError, match="not in the model"):
            SemanticModel(tenant_code="evive", model_version="v1", entities=(entity,))

    def test_a_name_cannot_be_both_dimension_and_time_dimension(self):
        with pytest.raises(ValueError, match="exactly one field"):
            SemanticEntity(
                name="x-entity",
                entity_type="x_entity",
                dimensions=(Dimension(name="booked_date", column="booked_date"),),
                time_dimensions=(TimeDimension(name="booked_date", column="booked_date"),),
            )

    def test_derived_metric_must_name_both_components(self):
        with pytest.raises(ValueError, match="must name both a numerator"):
            Metric(
                name="ratio_metric",
                aggregation="sum",
                column="*",
                kind=MetricKind.RATIO,
                numerator_metric="sales",
            )

    def test_aggregate_metric_must_not_name_components(self):
        with pytest.raises(ValueError, match="must not name a numerator"):
            Metric(
                name="sales", aggregation="sum", column="gross_amount", numerator_metric="x_metric"
            )

    def test_derived_metric_referencing_an_undefined_metric_is_rejected(self):
        with pytest.raises(ValueError, match="references undefined metric"):
            SemanticEntity(
                name="x-entity",
                entity_type="x_entity",
                metrics=(
                    Metric(
                        name="ratio_metric",
                        aggregation="sum",
                        column="*",
                        kind=MetricKind.RATIO,
                        numerator_metric="missing_a",
                        denominator_metric="missing_b",
                    ),
                ),
            )

    def test_unowned_fields_are_reported(self):
        model = SemanticModel(
            tenant_code="evive",
            model_version="v1",
            entities=(SemanticEntity(name="x-entity", entity_type="x_entity"),),
        )
        assert model.unowned_fields() == ["entity:x-entity"]

    def test_validate_model_flags_unowned_and_unsigned(self):
        findings = validate_model(_model())
        codes = {f.code for f in findings}
        assert "unsigned_metric" in codes


class TestJoinsAndGrain:
    def test_joined_dimension_uses_the_declared_path(self):
        request = SemanticQueryRequest(
            entity="sales_order",
            metrics=("order_count",),
            joined_dimensions=(("franchisee", "franchisee_name"),),
        )
        compiled = _COMPILER.compile(request, granted_access_tags=_ALL_TAGS, scope_predicate=None)
        assert "LEFT JOIN franchisee AS j_0" in compiled.sql
        assert "j_0.franchisee_name AS franchisee_franchisee_name" in compiled.sql

    def test_undeclared_join_is_rejected(self):
        request = SemanticQueryRequest(
            entity="franchisee",
            metrics=("franchisee_name",),
            joined_dimensions=(("sales_order", "order_status"),),
        )
        with pytest.raises(SemanticQueryError, match="No declared join"):
            _COMPILER.compile(request, granted_access_tags=_ALL_TAGS, scope_predicate=None)

    def test_time_grain_truncates_and_groups(self):
        request = SemanticQueryRequest(
            entity="sales_order",
            metrics=("order_count",),
            time_dimension="booked_date",
            time_grain=TimeGrain.MONTH,
        )
        compiled = _COMPILER.compile(request, granted_access_tags=_ALL_TAGS, scope_predicate=None)
        assert "date_trunc('month', entity_data.booked_date) AS booked_date" in compiled.sql
        assert "GROUP BY date_trunc('month', entity_data.booked_date)" in compiled.sql

    def test_absolute_time_range_binds_inclusive_start_exclusive_end(self):
        request = SemanticQueryRequest(
            entity="sales_order",
            metrics=("order_count",),
            time_dimension="booked_date",
            time_range=TimeRangeFilter(
                time_dimension="booked_date", start=date(2026, 1, 1), end=date(2026, 1, 31)
            ),
        )
        compiled = _COMPILER.compile(request, granted_access_tags=_ALL_TAGS, scope_predicate=None)
        assert compiled.parameters == ["2026-01-01", "2026-02-01"]

    def test_relative_range_resolves_against_today(self):
        request = SemanticQueryRequest(
            entity="sales_order",
            metrics=("order_count",),
            time_dimension="booked_date",
            time_range=TimeRangeFilter(
                time_dimension="booked_date", relative_range=RelativeDateRange.LAST_7_DAYS
            ),
        )
        compiled = _COMPILER.compile(
            request, granted_access_tags=_ALL_TAGS, today=date(2026, 7, 28), scope_predicate=None
        )
        assert compiled.parameters == ["2026-07-22", "2026-07-29"]

    def test_prior_year_comparison_uses_the_fiscal_calendar(self):
        request = SemanticQueryRequest(
            entity="sales_order",
            metrics=("order_count",),
            time_dimension="booked_date",
            time_grain=TimeGrain.YEAR,
            time_comparison=TimeComparison.PRIOR_YEAR,
        )
        compiled = _COMPILER.compile(
            request, granted_access_tags=_ALL_TAGS, today=date(2026, 7, 28), scope_predicate=None
        )
        # Fiscal year starts in April, so the prior fiscal year begins 2025-04-01.
        assert compiled.parameters == ["2025-04-01", "2026-04-01"]

    def test_time_range_without_a_time_dimension_is_rejected(self):
        request = SemanticQueryRequest(
            entity="sales_order",
            metrics=("order_count",),
            time_comparison=TimeComparison.PRIOR_YEAR,
        )
        with pytest.raises(SemanticQueryError, match="without naming a time dimension"):
            _COMPILER.compile(request, granted_access_tags=_ALL_TAGS, scope_predicate=None)

    def test_unknown_time_dimension_is_rejected(self):
        request = SemanticQueryRequest(
            entity="sales_order", metrics=("order_count",), time_dimension="nope"
        )
        with pytest.raises(SemanticQueryError, match="No time dimension"):
            _COMPILER.compile(request, granted_access_tags=_ALL_TAGS, scope_predicate=None)

    def test_reversed_absolute_range_is_rejected(self):
        request = SemanticQueryRequest(
            entity="sales_order",
            metrics=("order_count",),
            time_dimension="booked_date",
            time_range=TimeRangeFilter(
                time_dimension="booked_date", start=date(2026, 2, 1), end=date(2026, 1, 1)
            ),
        )
        with pytest.raises(SemanticQueryError, match="precedes its start"):
            _COMPILER.compile(request, granted_access_tags=_ALL_TAGS, scope_predicate=None)


class TestFilters:
    def test_in_list_binds_every_value(self):
        request = SemanticQueryRequest(
            entity="sales_order",
            metrics=("order_count",),
            filters=(
                SemanticFilter(dimension="order_status", operator="in", values=("open", "closed")),
            ),
        )
        compiled = _COMPILER.compile(request, granted_access_tags=_ALL_TAGS, scope_predicate=None)
        assert "entity_data.order_status IN (?, ?)" in compiled.sql
        assert compiled.parameters == ["open", "closed"]

    def test_not_in_is_supported(self):
        request = SemanticQueryRequest(
            entity="sales_order",
            metrics=("order_count",),
            filters=(
                SemanticFilter(dimension="order_status", operator="not_in", values=("void",)),
            ),
        )
        compiled = _COMPILER.compile(request, granted_access_tags=_ALL_TAGS, scope_predicate=None)
        assert "NOT IN (?)" in compiled.sql

    def test_null_handling_takes_no_value(self):
        request = SemanticQueryRequest(
            entity="sales_order",
            metrics=("order_count",),
            filters=(SemanticFilter(dimension="order_status", operator="is_null"),),
        )
        compiled = _COMPILER.compile(request, granted_access_tags=_ALL_TAGS, scope_predicate=None)
        assert "entity_data.order_status IS NULL" in compiled.sql
        assert compiled.parameters == []

    def test_not_null_handling(self):
        request = SemanticQueryRequest(
            entity="sales_order",
            metrics=("order_count",),
            filters=(SemanticFilter(dimension="order_status", operator="not_null"),),
        )
        compiled = _COMPILER.compile(request, granted_access_tags=_ALL_TAGS, scope_predicate=None)
        assert "IS NOT NULL" in compiled.sql

    def test_empty_in_list_is_rejected(self):
        with pytest.raises(SemanticQueryError, match="with no values"):
            SemanticFilter(dimension="order_status", operator="in", values=())

    def test_null_operator_with_a_value_is_rejected(self):
        with pytest.raises(SemanticQueryError, match="takes no value"):
            SemanticFilter(dimension="order_status", operator="is_null", value="x")

    def test_comparison_without_a_value_is_rejected(self):
        with pytest.raises(SemanticQueryError, match="supplies no value"):
            SemanticFilter(dimension="order_status", operator="eq")

    def test_oversized_in_list_is_rejected(self):
        with pytest.raises(SemanticQueryError, match="above the cap"):
            SemanticFilter(
                dimension="order_status",
                operator="in",
                values=tuple(str(i) for i in range(1_001)),
            )

    def test_row_limit_is_bounded(self):
        with pytest.raises(SemanticQueryError, match="row_limit must be between"):
            SemanticQueryRequest(entity="sales_order", metrics=("order_count",), row_limit=0)


class TestDerivedMetrics:
    def test_ratio_guards_the_denominator(self):
        request = SemanticQueryRequest(entity="sales_order", metrics=("average_order_value",))
        compiled = _COMPILER.compile(request, granted_access_tags=_ALL_TAGS, scope_predicate=None)
        assert "NULLIF(COUNT(DISTINCT entity_data.order_id), 0)" in compiled.sql

    def test_zero_denominator_behaviour_can_coalesce(self):
        entity = SemanticEntity(
            name="x-entity",
            entity_type="x_entity",
            metrics=(
                Metric(name="numerator", aggregation="sum", column="a_column"),
                Metric(name="denominator", aggregation="sum", column="b_column"),
                Metric(
                    name="ratio_metric",
                    aggregation="sum",
                    column="*",
                    kind=MetricKind.RATIO,
                    numerator_metric="numerator",
                    denominator_metric="denominator",
                    null_denominator=NullDenominatorBehaviour.ZERO,
                ),
            ),
        )
        model = SemanticModel(tenant_code="evive", model_version="v1", entities=(entity,))
        compiled = QueryCompiler(model).compile(
            SemanticQueryRequest(entity="x-entity", metrics=("ratio_metric",)),
            granted_access_tags=frozenset(),
            scope_predicate=None,
        )
        assert "COALESCE(" in compiled.sql

    def test_difference_metric_subtracts(self):
        request = SemanticQueryRequest(entity="sales_order", metrics=("margin",))
        compiled = _COMPILER.compile(request, granted_access_tags=_ALL_TAGS, scope_predicate=None)
        assert " - " in compiled.sql

    def test_count_distinct_emits_distinct(self):
        request = SemanticQueryRequest(entity="sales_order", metrics=("order_count",))
        compiled = _COMPILER.compile(request, granted_access_tags=_ALL_TAGS, scope_predicate=None)
        assert "COUNT(DISTINCT entity_data.order_id)" in compiled.sql

    def test_derived_metric_inherits_component_access_tags(self):
        request = SemanticQueryRequest(entity="sales_order", metrics=("average_order_value",))
        with pytest.raises(AccessDeniedError):
            _COMPILER.compile(request, granted_access_tags=frozenset(), scope_predicate=None)


class TestScopePredicateInjection:
    def _predicate(self):
        profile = TenantPartitionProfile(
            tenant_code="evive",
            partition_model=PartitionModel.PARTITIONED,
            partition_kind=PartitionKind.FRANCHISE,
        )
        units = [
            ScopeUnit(
                tenant_code="evive",
                scope_unit_id="franchisee-0001",
                partition_kind=PartitionKind.FRANCHISE,
                display_name="One",
            )
        ]
        claims = build_scope_claims(
            "evive",
            profile,
            granted_scope_unit_ids=frozenset({"franchisee-0001"}),
            units=units,
        )
        return scope_predicate(claims, surface=ConsumptionSurface.SEMANTIC_QUERY)

    def test_predicate_leads_the_where_clause_and_its_parameters(self):
        request = SemanticQueryRequest(
            entity="sales_order",
            metrics=("order_count",),
            filters=(SemanticFilter(dimension="order_status", operator="eq", value="open"),),
        )
        compiled = _COMPILER.compile(
            request, granted_access_tags=_ALL_TAGS, scope_predicate=self._predicate()
        )
        assert compiled.scope_predicate_applied is True
        assert compiled.sql.index("scope_unit_id IN") < compiled.sql.index("order_status =")
        assert compiled.parameters == ["franchisee-0001", "open"]

    def test_no_predicate_leaves_the_flag_false(self):
        request = SemanticQueryRequest(entity="sales_order", metrics=("order_count",))
        compiled = _COMPILER.compile(request, granted_access_tags=_ALL_TAGS, scope_predicate=None)
        assert compiled.scope_predicate_applied is False


class TestFiscalCalendar:
    _CALENDAR = FiscalCalendar(fiscal_year_start_month=4)

    def test_fiscal_year_is_labelled_by_its_end(self):
        assert self._CALENDAR.fiscal_year_of(date(2026, 5, 1)) == 2027
        assert self._CALENDAR.fiscal_year_of(date(2026, 3, 1)) == 2026

    def test_gregorian_calendar_labels_by_calendar_year(self):
        assert FiscalCalendar().fiscal_year_of(date(2026, 5, 1)) == 2026

    def test_fiscal_period_and_quarter(self):
        assert self._CALENDAR.fiscal_period_of(date(2026, 4, 1)) == 1
        assert self._CALENDAR.fiscal_period_of(date(2027, 3, 1)) == 12
        assert self._CALENDAR.fiscal_quarter_of(date(2026, 4, 1)) == 1
        assert self._CALENDAR.fiscal_quarter_of(date(2026, 7, 1)) == 2

    def test_truncation_per_grain(self):
        moment = date(2026, 7, 15)
        assert self._CALENDAR.truncate(moment, TimeGrain.DAY) == moment
        assert self._CALENDAR.truncate(moment, TimeGrain.MONTH) == date(2026, 7, 1)
        assert self._CALENDAR.truncate(moment, TimeGrain.QUARTER) == date(2026, 7, 1)
        assert self._CALENDAR.truncate(moment, TimeGrain.YEAR) == date(2026, 4, 1)

    def test_week_truncation_honours_the_declared_week_start(self):
        monday_start = FiscalCalendar(fiscal_week_start_weekday=0)
        assert monday_start.truncate(date(2026, 7, 29), TimeGrain.WEEK) == date(2026, 7, 27)

    def test_period_bounds_are_half_open(self):
        start, end = self._CALENDAR.period_bounds(date(2026, 7, 15), TimeGrain.MONTH)
        assert (start, end) == (date(2026, 7, 1), date(2026, 8, 1))

    def test_prior_period_bounds(self):
        start, end = self._CALENDAR.comparison_bounds(
            date(2026, 7, 15), TimeGrain.MONTH, TimeComparison.PRIOR_PERIOD
        )
        assert (start, end) == (date(2026, 6, 1), date(2026, 7, 1))

    def test_period_to_date_stops_at_today(self):
        start, end = self._CALENDAR.comparison_bounds(
            date(2026, 7, 15), TimeGrain.MONTH, TimeComparison.PERIOD_TO_DATE
        )
        assert (start, end) == (date(2026, 7, 1), date(2026, 7, 16))

    def test_leap_day_shifts_to_28_february(self):
        start, _ = self._CALENDAR.comparison_bounds(
            date(2028, 2, 29), TimeGrain.DAY, TimeComparison.PRIOR_YEAR
        )
        assert start == date(2027, 2, 28)

    def test_invalid_calendar_is_rejected(self):
        with pytest.raises(ValueError, match="between 1 and 12"):
            FiscalCalendar(fiscal_year_start_month=13)
        with pytest.raises(ValueError, match="between 0"):
            FiscalCalendar(fiscal_week_start_weekday=7)

    def test_every_supported_dialect_has_a_truncation(self):
        for dialect in ("athena", "postgresql", "redshift", "mysql", "sqlserver"):
            assert truncation_sql("booked_date", TimeGrain.MONTH, dialect)

    def test_unknown_dialect_is_rejected(self):
        with pytest.raises(ValueError, match="not supported"):
            truncation_sql("booked_date", TimeGrain.DAY, "oracle")


class TestMetricLineage:
    def test_aggregate_lineage_names_its_column(self):
        lineage = metric_lineage(_model(), "sales_order", "sales")
        assert lineage.physical_columns == ("gross_amount",)
        assert lineage.derived_from_metrics == ()
        assert lineage.access_tag == TAG_FINANCE

    def test_derived_lineage_resolves_to_physical_columns(self):
        lineage = metric_lineage(_model(), "sales_order", "average_order_value")
        assert set(lineage.physical_columns) == {"gross_amount", "order_id"}
        assert set(lineage.derived_from_metrics) == {"sales", "order_count"}

    def test_lineage_json_is_api_ready(self):
        payload = metric_lineage(_model(), "sales_order", "sales").to_json()
        assert payload["metric"] == "sales"
        assert payload["physical_columns"] == ["gross_amount"]

    def test_impact_analysis_finds_affected_metrics(self):
        affected = metrics_touching_column(_model(), "gross_amount")
        assert "sales_order.sales" in affected
        assert "sales_order.average_order_value" in affected

    def test_all_lineage_covers_every_metric(self):
        model = _model()
        assert len(all_metric_lineage(model)) == sum(len(e.metrics) for e in model.entities)

    def test_methodology_is_generated_from_the_model(self):
        rendered = build_methodology_document(_model()).render_markdown()
        assert "# Calculation methodology" in rendered
        assert "Gross booked value." in rendered
        assert "_unsigned_" in rendered

    def test_methodology_key_is_tenant_prefixed(self):
        assert methodology_s3_key("evive", "v1") == "evive/semantic-methodology/v1.md"


@mock_aws
class TestModelGovernance:
    def _governance(self) -> SemanticModelGovernance:
        boto3.client("s3", region_name=_REGION).create_bucket(Bucket="curated-bucket")
        client = boto3.client("dynamodb", region_name=_REGION)
        tables = (
            ("EdlSemanticModel", "model_version"),
            ("EdlSemanticApproval", "approval_key"),
        )
        for name, sk in tables:
            client.create_table(
                TableName=name,
                KeySchema=[
                    {"AttributeName": "tenant_code", "KeyType": "HASH"},
                    {"AttributeName": sk, "KeyType": "RANGE"},
                ],
                AttributeDefinitions=[
                    {"AttributeName": "tenant_code", "AttributeType": "S"},
                    {"AttributeName": sk, "AttributeType": "S"},
                ],
                BillingMode="PAY_PER_REQUEST",
            )
        return SemanticModelGovernance(
            environment="dev", region_name=_REGION, s3_bucket="curated-bucket"
        )

    def _signed_model(self) -> SemanticModel:
        model = _model()
        for entity in model.entities:
            for metric in entity.metrics:
                model = sign_metric_definition(
                    model,
                    entity.name,
                    metric.name,
                    signed_by=str(metric.business_owner),
                    signed_at="2026-07-28T00:00:00+00:00",
                )
        return model

    def test_unsigned_model_cannot_publish_without_allow_draft(self):
        governance = self._governance()
        with pytest.raises(ModelValidationError, match="failed validation"):
            governance.publish(_model(), published_by="alice")

    def test_unsigned_model_publishes_as_a_draft(self):
        governance = self._governance()
        record = governance.publish(_model(), published_by="alice", allow_draft=True)
        assert record.status is ModelStatus.DRAFT

    def test_signed_model_publishes_then_approves_then_activates(self):
        governance = self._governance()
        model = self._signed_model()
        governance.publish(model, published_by="alice")
        approved = governance.approve("evive", "v1", approved_by="bob")
        assert approved.status is ModelStatus.APPROVED
        active = governance.activate("evive", "v1")
        assert active.status is ModelStatus.ACTIVE
        assert governance.active_version("evive") == "v1"

    def test_self_approval_is_refused(self):
        governance = self._governance()
        governance.publish(self._signed_model(), published_by="alice")
        with pytest.raises(MakerCheckerViolationError, match="own publisher"):
            governance.approve("evive", "v1", approved_by="alice")

    def test_draft_cannot_be_activated(self):
        governance = self._governance()
        governance.publish(_model(), published_by="alice", allow_draft=True)
        with pytest.raises(MakerCheckerViolationError, match="only an approved version"):
            governance.activate("evive", "v1")

    def test_load_verifies_the_body_hash(self):
        governance = self._governance()
        model = self._signed_model()
        governance.publish(model, published_by="alice")
        governance.approve("evive", "v1", approved_by="bob")
        governance.activate("evive", "v1")
        assert governance.load_model("evive").model_version == "v1"

    def test_tampered_body_fails_closed(self):
        governance = self._governance()
        governance.publish(self._signed_model(), published_by="alice")
        boto3.client("s3", region_name=_REGION).put_object(
            Bucket="curated-bucket",
            Key="evive/semantic-models/v1.json",
            Body=b'{"tenant_code":"evive","model_version":"v1","entities":[]}',
        )
        with pytest.raises(ModelIntegrityError, match="possibly-tampered"):
            governance.load_model("evive", "v1")

    def test_rollback_requires_maker_checker(self):
        governance = self._governance()
        governance.publish(self._signed_model(), published_by="alice")
        with pytest.raises(MakerCheckerViolationError):
            governance.rollback("evive", "v1", requested_by="alice", approved_by="alice")

    def test_rollback_repoints_the_active_version(self):
        governance = self._governance()
        model = self._signed_model()
        governance.publish(model, published_by="alice")
        governance.approve("evive", "v1", approved_by="bob")
        governance.activate("evive", "v1")
        v2 = model.model_copy(update={"model_version": "v2"})
        governance.publish(v2, published_by="alice")
        governance.approve("evive", "v2", approved_by="bob")
        governance.activate("evive", "v2")
        assert governance.active_version("evive") == "v2"
        governance.rollback("evive", "v1", requested_by="alice", approved_by="bob")
        assert governance.active_version("evive") == "v1"

    def test_version_listing_excludes_the_pointer(self):
        governance = self._governance()
        governance.publish(self._signed_model(), published_by="alice")
        governance.approve("evive", "v1", approved_by="bob")
        governance.activate("evive", "v1")
        assert [r.model_version for r in governance.list_versions("evive")] == ["v1"]

    def test_missing_version_raises(self):
        governance = self._governance()
        with pytest.raises(ModelVersionNotFoundError):
            governance.get_version("evive", "v9")

    def test_load_without_an_active_version_raises(self):
        governance = self._governance()
        with pytest.raises(ModelVersionNotFoundError, match="no active semantic model"):
            governance.load_model("evive")


class TestResultCache:
    def _key(self, tags=frozenset({"dept_finance"}), predicate=None) -> ResultCacheKey:
        return ResultCacheKey.build(
            tenant_code="evive",
            model_version="v1",
            sql="SELECT 1",
            parameters=[],
            granted_access_tags=tags,
            predicate=predicate,
        )

    def test_hit_and_miss_accounting(self):
        cache = SemanticResultCache()
        assert cache.get(self._key()) is None
        cache.put(self._key(), [{"x": 1}])
        assert cache.get(self._key()) == [{"x": 1}]
        assert cache.hits == 1
        assert cache.misses == 1
        assert cache.hit_rate_pct == 50.0

    def test_different_access_tags_never_share_an_entry(self):
        cache = SemanticResultCache()
        cache.put(self._key(tags=frozenset({"dept_finance"})), [{"revenue": 1}])
        assert cache.get(self._key(tags=frozenset({"dept_operations"}))) is None

    def test_different_scope_never_shares_an_entry(self):
        profile = TenantPartitionProfile(
            tenant_code="evive",
            partition_model=PartitionModel.PARTITIONED,
            partition_kind=PartitionKind.FRANCHISE,
        )
        units = [
            ScopeUnit(
                tenant_code="evive",
                scope_unit_id=f"franchisee-{i:04d}",
                partition_kind=PartitionKind.FRANCHISE,
                display_name=f"F{i}",
            )
            for i in (1, 2)
        ]
        first = scope_predicate(
            build_scope_claims(
                "evive", profile, granted_scope_unit_ids=frozenset({"franchisee-0001"}), units=units
            )
        )
        second = scope_predicate(
            build_scope_claims(
                "evive", profile, granted_scope_unit_ids=frozenset({"franchisee-0002"}), units=units
            )
        )
        cache = SemanticResultCache()
        cache.put(self._key(predicate=first), [{"revenue": 1}])
        assert cache.get(self._key(predicate=second)) is None
        assert scope_signature(first) != scope_signature(second)
        assert scope_signature(None) == "none"

    def test_partition_change_invalidates(self):
        cache = SemanticResultCache()
        cache.put(self._key(), [{"x": 1}], partition_marker="2026-07-28")
        assert cache.get(self._key(), partition_marker="2026-07-29") is None

    def test_model_publish_invalidates_the_version(self):
        cache = SemanticResultCache()
        cache.put(self._key(), [{"x": 1}])
        assert cache.invalidate_model_version("evive", "v1") == 1
        assert cache.get(self._key()) is None

    def test_tenant_invalidation(self):
        cache = SemanticResultCache()
        cache.put(self._key(), [{"x": 1}])
        assert cache.invalidate_tenant("evive") == 1
        assert cache.size == 0

    def test_age_backstop_evicts(self):
        cache = SemanticResultCache(max_age_seconds=-1)
        cache.put(self._key(), [{"x": 1}])
        assert cache.get(self._key()) is None

    def test_bounded_size_evicts_the_oldest(self):
        cache = SemanticResultCache(max_entries=1)
        cache.put(self._key(), [{"x": 1}])
        cache.put(ResultCacheKey.build("evive", "v1", "SELECT 2", [], frozenset()), [{"y": 2}])
        assert cache.size == 1

    def test_clear_empties_the_cache(self):
        cache = SemanticResultCache()
        cache.put(self._key(), [{"x": 1}])
        cache.clear()
        assert cache.size == 0
        assert cache.hit_rate_pct == 0.0


class TestEnterpriseModelAndKpiHarness:
    def test_every_sow_kpi_resolves(self):
        model = build_enterprise_model("evive")
        for _, (entity_name, metric_name) in SOW_KPI_MAP.items():
            assert model.entity(entity_name).metric(metric_name)

    def test_every_field_is_owned(self):
        assert build_enterprise_model("evive").unowned_fields() == []

    def test_definitions_start_unsigned(self):
        model = build_enterprise_model("evive")
        assert len(model.unsigned_metrics()) == sum(len(e.metrics) for e in model.entities)

    def test_ap_bills_are_finance_tagged_so_sales_cannot_query_them(self):
        model = build_enterprise_model("evive")
        assert model.entity("ap_bill").metric("payables_amount").access_tag == TAG_FINANCE
        compiler = QueryCompiler(model)
        request = SemanticQueryRequest(entity="ap_bill", metrics=("payables_amount",))
        with pytest.raises(AccessDeniedError):
            compiler.compile(request, granted_access_tags=frozenset(), scope_predicate=None)

    def test_executive_tier_expands_to_every_department(self):
        expanded = expand_access_tags(frozenset({TAG_EXECUTIVE}))
        assert TAG_FINANCE in expanded
        assert "dept_operations" in expanded

    def test_non_executive_tags_do_not_expand(self):
        assert expand_access_tags(frozenset({"dept_operations"})) == frozenset({"dept_operations"})

    def test_signing_requires_the_declared_owner(self):
        model = build_enterprise_model("evive")
        with pytest.raises(ValueError, match="cannot sign its definition"):
            sign_metric_definition(
                model, "ar_invoice", "revenue", signed_by="role:intern", signed_at="now"
            )

    def test_signing_marks_the_metric_signed(self):
        model = build_enterprise_model("evive")
        signed = sign_metric_definition(
            model,
            "ar_invoice",
            "revenue",
            signed_by="role:controller",
            signed_at="2026-07-28T00:00:00+00:00",
        )
        assert signed.entity("ar_invoice").metric("revenue").is_signed is True

    def test_signing_an_unknown_metric_raises(self):
        with pytest.raises(KeyError):
            sign_metric_definition(
                build_enterprise_model("evive"), "ar_invoice", "nope", signed_by="a", signed_at="b"
            )

    def test_structural_harness_compiles_every_kpi(self):
        model = build_enterprise_model("evive")
        harness = KpiValidationHarness(model, structural_expectations(model))
        report = harness.run(granted_access_tags=None)
        assert report.passed is True
        assert report.compile_only_count == len(SOW_KPI_MAP)
        assert "compile-only" in report.render_summary()

    def test_value_check_passes_within_tolerance(self):
        model = build_enterprise_model("evive")
        expectation = KpiExpectation(
            kpi_name="Revenue",
            entity="ar_invoice",
            metric="revenue",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            expected_value="1000.00",
            tolerance_pct=1.0,
            time_dimension="recognition_date",
            required_access_tags=frozenset({TAG_FINANCE}),
        )
        harness = KpiValidationHarness(
            model, [expectation], executor=lambda sql, params: [{"revenue": "1005.00"}]
        )
        report = harness.run(granted_access_tags=None)
        assert report.passed is True
        assert report.value_checked_count == 1

    def test_value_check_fails_outside_tolerance(self):
        model = build_enterprise_model("evive")
        expectation = KpiExpectation(
            kpi_name="Revenue",
            entity="ar_invoice",
            metric="revenue",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            expected_value="1000.00",
            time_dimension="recognition_date",
            required_access_tags=frozenset({TAG_FINANCE}),
        )
        harness = KpiValidationHarness(
            model, [expectation], executor=lambda sql, params: [{"revenue": "1500.00"}]
        )
        report = harness.run(granted_access_tags=None)
        assert report.passed is False
        assert report.failures[0].outcome is KpiCheckOutcome.FAILED
        assert "exceeds tolerance" in report.failures[0].detail

    def test_compile_failure_is_reported_distinctly(self):
        model = build_enterprise_model("evive")
        expectation = KpiExpectation(
            kpi_name="Nonexistent",
            entity="ar_invoice",
            metric="not_a_metric",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
        )
        report = KpiValidationHarness(model, [expectation]).run(granted_access_tags=None)
        assert report.failures[0].outcome is KpiCheckOutcome.COMPILE_FAILED

    def test_executor_failure_is_reported(self):
        model = build_enterprise_model("evive")
        expectation = KpiExpectation(
            kpi_name="Revenue",
            entity="ar_invoice",
            metric="revenue",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            expected_value="1000",
            time_dimension="recognition_date",
            required_access_tags=frozenset({TAG_FINANCE}),
        )

        def boom(sql, params):
            raise RuntimeError("athena down")

        harness = KpiValidationHarness(model, [expectation], executor=boom)
        report = harness.run(granted_access_tags=None)
        assert "execution failed" in report.failures[0].detail

    def test_no_rows_is_a_failure_not_a_pass(self):
        model = build_enterprise_model("evive")
        expectation = KpiExpectation(
            kpi_name="Revenue",
            entity="ar_invoice",
            metric="revenue",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 31),
            expected_value="1000",
            time_dimension="recognition_date",
            required_access_tags=frozenset({TAG_FINANCE}),
        )
        harness = KpiValidationHarness(model, [expectation], executor=lambda s, p: [])
        report = harness.run(granted_access_tags=None)
        assert report.passed is False

    def test_json_report_is_machine_readable(self):
        model = build_enterprise_model("evive")
        harness = KpiValidationHarness(model, structural_expectations(model))
        payload = harness.run(granted_access_tags=None).to_json()
        assert '"kpi": "Revenue"' in payload

    def test_fiscal_year_start_is_tenant_configuration(self):
        model = build_enterprise_model("evive", fiscal_year_start_month=4)
        assert model.fiscal_year_start_month == 4
