"""
Semantic query compiler (FR-2.3 / FR-2.4 / FR-2.5, DL-SEM-01, 02, 07, 09).

Turns a structured `SemanticQueryRequest` (metrics, dimensions, filters, joins, time grain —
all referenced by business name) into a parameterised SQL string. Callers never supply SQL
or column names: every identifier is resolved from the validated `SemanticModel`, filter
values are bound as parameters, and access tags are enforced before a field appears.

The compiler is also where the scope predicate is injected (DL-SCOPE-14, DL-SCOPE-17) and
where row-level security is applied (DL-SEC-11) — server-side, before every other filter,
never caller-supplied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

from contracts.platform_metrics import PlatformMetric
from observability.metric_recorder import record_platform_metric
from semantic.fiscal_calendar import FiscalCalendar, truncation_sql
from semantic.semantic_model import (
    Dimension,
    Metric,
    MetricKind,
    NullDenominatorBehaviour,
    SemanticEntity,
    SemanticModel,
    TimeComparison,
    TimeGrain,
)
from tenancy.scope_predicate import ScopePredicate

FilterOperator = Literal[
    "eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "is_null", "not_null"
]

_SQL_OPERATORS: dict[str, str] = {
    "eq": "=",
    "ne": "<>",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}

# Fixed internal view name the engine binds the entity's analytics dataset to.
_ENTITY_VIEW = "entity_data"

# Hard server-side row cap; every response is paginated and bounded.
DEFAULT_ROW_LIMIT = 10_000
MAX_ROW_LIMIT = 100_000

# An IN list longer than this is a caller mistake, not a query.
MAX_IN_LIST_VALUES = 1_000


class SemanticQueryError(Exception):
    """Raised when a semantic query cannot be compiled."""


class AccessDeniedError(SemanticQueryError):
    """Raised when the caller lacks an access tag required by a referenced field."""


class RelativeDateRange(StrEnum):
    """Named relative ranges, so a dashboard filter is not a caller-supplied expression."""

    TODAY = "today"
    YESTERDAY = "yesterday"
    LAST_7_DAYS = "last_7_days"
    LAST_30_DAYS = "last_30_days"
    LAST_90_DAYS = "last_90_days"
    MONTH_TO_DATE = "month_to_date"
    QUARTER_TO_DATE = "quarter_to_date"
    YEAR_TO_DATE = "year_to_date"
    LAST_MONTH = "last_month"
    LAST_QUARTER = "last_quarter"
    LAST_YEAR = "last_year"


@dataclass(frozen=True)
class SemanticFilter:
    """One filter over a declared dimension; values are always bound, never interpolated."""

    dimension: str
    operator: FilterOperator
    value: Any = None
    values: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        if self.operator in ("in", "not_in"):
            if not self.values:
                raise SemanticQueryError(
                    f"Filter on {self.dimension!r} uses {self.operator!r} with no values."
                )
            if len(self.values) > MAX_IN_LIST_VALUES:
                raise SemanticQueryError(
                    f"Filter on {self.dimension!r} lists {len(self.values)} values, above the "
                    f"cap of {MAX_IN_LIST_VALUES}."
                )
        elif self.operator in ("is_null", "not_null"):
            if self.value is not None or self.values:
                raise SemanticQueryError(
                    f"Filter on {self.dimension!r} uses {self.operator!r}, which takes no value."
                )
        elif self.value is None:
            raise SemanticQueryError(
                f"Filter on {self.dimension!r} uses {self.operator!r} but supplies no value."
            )


@dataclass(frozen=True)
class TimeRangeFilter:
    """A range over a declared time dimension, absolute or relative."""

    time_dimension: str
    relative_range: RelativeDateRange | None = None
    start: date | None = None
    end: date | None = None

    def __post_init__(self) -> None:
        if self.relative_range is None and not (self.start and self.end):
            raise SemanticQueryError(
                f"Time filter on {self.time_dimension!r} needs either a relative_range or both "
                "start and end."
            )


@dataclass(frozen=True)
class SemanticQueryRequest:
    """A structured request; there is no field on it that accepts SQL."""

    entity: str
    metrics: tuple[str, ...]
    dimensions: tuple[str, ...] = ()
    filters: tuple[SemanticFilter, ...] = ()
    joined_dimensions: tuple[tuple[str, str], ...] = ()
    time_dimension: str | None = None
    time_grain: TimeGrain | None = None
    time_comparison: TimeComparison = TimeComparison.NONE
    time_range: TimeRangeFilter | None = None
    row_limit: int = DEFAULT_ROW_LIMIT
    dialect: str = "athena"

    def __post_init__(self) -> None:
        if self.row_limit < 1 or self.row_limit > MAX_ROW_LIMIT:
            raise SemanticQueryError(
                f"row_limit must be between 1 and {MAX_ROW_LIMIT}; got {self.row_limit}."
            )


@dataclass(frozen=True)
class CompiledQuery:
    """The compiled statement plus its bound parameters."""

    sql: str
    parameters: list[Any] = field(default_factory=list)
    view_name: str = _ENTITY_VIEW
    scope_predicate_applied: bool = False
    referenced_columns: tuple[str, ...] = ()


class QueryCompiler:
    """The single place SQL is produced; joins and time grain are compiler features."""

    def __init__(self, model: SemanticModel) -> None:
        self._model = model
        self._calendar = FiscalCalendar(
            fiscal_year_start_month=model.fiscal_year_start_month,
            fiscal_week_start_weekday=model.fiscal_week_start_weekday,
        )

    def compile(
        self,
        request: SemanticQueryRequest,
        *,
        granted_access_tags: frozenset[str],
        # Non-optional in type as well as position. Making it positionally required (2026-07-28)
        # did not close the hole: `| None` plus an early return meant a caller could still pass
        # `None` and get tenant-wide rows with no error, no log line, and no metric — and writing
        # `None` explicitly reads as deliberate, so review waved it through. A caller with no
        # end-user claim now passes `unrestricted_predicate(...)`, which is audited (DL-SCOPE-14).
        scope_predicate: ScopePredicate,
        today: date | None = None,
    ) -> CompiledQuery:
        if not request.metrics:
            raise SemanticQueryError("A semantic query must request at least one metric.")
        entity = self._entity(request.entity)

        plan = _CompilationPlan(dialect=request.dialect)
        self._compile_dimensions(entity, request, granted_access_tags, plan)
        self._compile_time(entity, request, granted_access_tags, plan, today)
        self._compile_joins(entity, request, granted_access_tags, plan)
        self._compile_metrics(entity, request, granted_access_tags, plan)
        self._compile_filters(entity, request, granted_access_tags, plan)
        self._apply_scope_predicate(scope_predicate, plan)

        sql = f"SELECT {', '.join(plan.select_parts)} FROM {_ENTITY_VIEW}"  # noqa: S608 # nosec B608
        sql += "".join(plan.join_clauses)
        if plan.where_clauses:
            sql += f" WHERE {' AND '.join(plan.where_clauses)}"
        if plan.group_by:
            sql += f" GROUP BY {', '.join(plan.group_by)}"
        sql += f" LIMIT {int(request.row_limit)}"
        record_platform_metric(
            PlatformMetric.SEMANTIC_QUERIES_COMPILED, 1.0, EntityType=entity.entity_type
        )
        return CompiledQuery(
            sql=sql,
            parameters=plan.parameters,
            scope_predicate_applied=plan.scope_applied,
            referenced_columns=tuple(plan.referenced_columns),
        )

    # ── Stages ────────────────────────────────────────────────────────────────

    def _compile_dimensions(
        self,
        entity: SemanticEntity,
        request: SemanticQueryRequest,
        granted: frozenset[str],
        plan: _CompilationPlan,
    ) -> None:
        for dimension_name in request.dimensions:
            dimension = self._resolve_dimension(entity, dimension_name)
            self._enforce_access(dimension.access_tag, granted, dimension_name)
            plan.add_dimension(f"{_ENTITY_VIEW}.{dimension.column}", dimension.name)

    def _compile_time(
        self,
        entity: SemanticEntity,
        request: SemanticQueryRequest,
        granted: frozenset[str],
        plan: _CompilationPlan,
        today: date | None,
    ) -> None:
        if request.time_dimension is None:
            if request.time_range is not None or request.time_comparison is not TimeComparison.NONE:
                raise SemanticQueryError(
                    "A time range or comparison was requested without naming a time dimension."
                )
            return
        try:
            time_dimension = entity.time_dimension(request.time_dimension)
        except KeyError as exc:
            raise SemanticQueryError(str(exc)) from exc
        self._enforce_access(time_dimension.access_tag, granted, request.time_dimension)

        grain = request.time_grain or time_dimension.grain
        column = f"{_ENTITY_VIEW}.{time_dimension.column}"
        truncated = truncation_sql(column, grain, request.dialect)
        plan.add_expression_dimension(truncated, time_dimension.name)

        bounds = self._time_bounds(request, grain, today)
        if bounds is not None:
            start, end = bounds
            plan.add_where(f"{column} >= ?", start.isoformat())
            plan.add_where(f"{column} < ?", end.isoformat())

    def _time_bounds(
        self, request: SemanticQueryRequest, grain: TimeGrain, today: date | None
    ) -> tuple[date, date] | None:
        moment = today or datetime.now(UTC).date()
        if request.time_range is not None:
            if request.time_range.relative_range is not None:
                return self._relative_bounds(request.time_range.relative_range, moment)
            start = request.time_range.start
            end = request.time_range.end
            if start is None or end is None:  # pragma: no cover — guarded in TimeRangeFilter
                raise SemanticQueryError("An absolute time range needs both start and end.")
            if end < start:
                raise SemanticQueryError("Time range end precedes its start.")
            return start, end + timedelta(days=1)
        if request.time_comparison is not TimeComparison.NONE:
            return self._calendar.comparison_bounds(moment, grain, request.time_comparison)
        return None

    def _relative_bounds(self, relative: RelativeDateRange, moment: date) -> tuple[date, date]:
        tomorrow = moment + timedelta(days=1)
        if relative is RelativeDateRange.TODAY:
            return moment, tomorrow
        if relative is RelativeDateRange.YESTERDAY:
            return moment - timedelta(days=1), moment
        if relative is RelativeDateRange.LAST_7_DAYS:
            return moment - timedelta(days=6), tomorrow
        if relative is RelativeDateRange.LAST_30_DAYS:
            return moment - timedelta(days=29), tomorrow
        if relative is RelativeDateRange.LAST_90_DAYS:
            return moment - timedelta(days=89), tomorrow
        if relative is RelativeDateRange.MONTH_TO_DATE:
            return self._calendar.truncate(moment, TimeGrain.MONTH), tomorrow
        if relative is RelativeDateRange.QUARTER_TO_DATE:
            return self._calendar.truncate(moment, TimeGrain.QUARTER), tomorrow
        if relative is RelativeDateRange.YEAR_TO_DATE:
            return self._calendar.truncate(moment, TimeGrain.YEAR), tomorrow
        grain = {
            RelativeDateRange.LAST_MONTH: TimeGrain.MONTH,
            RelativeDateRange.LAST_QUARTER: TimeGrain.QUARTER,
            RelativeDateRange.LAST_YEAR: TimeGrain.YEAR,
        }[relative]
        return self._calendar.comparison_bounds(moment, grain, TimeComparison.PRIOR_PERIOD)

    def _compile_joins(
        self,
        entity: SemanticEntity,
        request: SemanticQueryRequest,
        granted: frozenset[str],
        plan: _CompilationPlan,
    ) -> None:
        """A caller names (target entity, dimension); the join path comes from the model."""
        for target_entity_name, dimension_name in request.joined_dimensions:
            try:
                join = entity.join_to(target_entity_name)
            except KeyError as exc:
                raise SemanticQueryError(str(exc)) from exc
            target = self._entity(target_entity_name)
            dimension = self._resolve_dimension(target, dimension_name)
            self._enforce_access(
                dimension.access_tag, granted, f"{target_entity_name}.{dimension_name}"
            )
            alias = plan.add_join(join, target_entity_name)
            plan.add_dimension(
                f"{alias}.{dimension.column}", f"{target_entity_name}_{dimension.name}"
            )

    def _compile_metrics(
        self,
        entity: SemanticEntity,
        request: SemanticQueryRequest,
        granted: frozenset[str],
        plan: _CompilationPlan,
    ) -> None:
        for metric_name in request.metrics:
            metric = self._resolve_metric(entity, metric_name)
            self._enforce_access(metric.access_tag, granted, metric_name)
            if metric.is_derived:
                plan.add_select(self._derived_metric_sql(entity, metric, granted))
                continue
            plan.add_select(self._aggregate_sql(metric))

    def _aggregate_sql(self, metric: Metric) -> str:
        column = "*" if metric.column == "*" else f"{_ENTITY_VIEW}.{metric.column}"
        distinct = "DISTINCT " if metric.aggregation == "count_distinct" else ""
        return f"{metric.sql_aggregation()}({distinct}{column}) AS {metric.name}"

    def _derived_metric_sql(
        self, entity: SemanticEntity, metric: Metric, granted: frozenset[str]
    ) -> str:
        """
        Ratio and difference metrics with explicit zero-denominator semantics (DL-SEM-09).

        `NULLIF` on the denominator is what makes a conversion rate defined once rather than
        each consumer deciding what division by zero means.
        """
        numerator = self._resolve_metric(entity, str(metric.numerator_metric))
        denominator = self._resolve_metric(entity, str(metric.denominator_metric))
        self._enforce_access(numerator.access_tag, granted, numerator.name)
        self._enforce_access(denominator.access_tag, granted, denominator.name)
        numerator_sql = self._bare_aggregate(numerator)
        denominator_sql = self._bare_aggregate(denominator)
        if metric.kind is MetricKind.DIFFERENCE:
            return f"({numerator_sql} - {denominator_sql}) AS {metric.name}"
        guarded = f"NULLIF({denominator_sql}, 0)"
        expression = f"({numerator_sql} * 1.0 / {guarded})"
        if metric.null_denominator is NullDenominatorBehaviour.ZERO:
            expression = f"COALESCE({expression}, 0)"
        return f"{expression} AS {metric.name}"

    def _bare_aggregate(self, metric: Metric) -> str:
        column = "*" if metric.column == "*" else f"{_ENTITY_VIEW}.{metric.column}"
        distinct = "DISTINCT " if metric.aggregation == "count_distinct" else ""
        return f"{metric.sql_aggregation()}({distinct}{column})"

    def _compile_filters(
        self,
        entity: SemanticEntity,
        request: SemanticQueryRequest,
        granted: frozenset[str],
        plan: _CompilationPlan,
    ) -> None:
        for query_filter in request.filters:
            dimension = self._resolve_dimension(entity, query_filter.dimension)
            self._enforce_access(dimension.access_tag, granted, query_filter.dimension)
            column = f"{_ENTITY_VIEW}.{dimension.column}"
            operator = query_filter.operator
            if operator == "is_null":
                plan.add_where(f"{column} IS NULL")
            elif operator == "not_null":
                plan.add_where(f"{column} IS NOT NULL")
            elif operator in ("in", "not_in"):
                placeholders = ", ".join("?" for _ in query_filter.values)
                keyword = "IN" if operator == "in" else "NOT IN"
                plan.add_where(f"{column} {keyword} ({placeholders})", *query_filter.values)
            else:
                plan.add_where(f"{column} {_SQL_OPERATORS[operator]} ?", query_filter.value)

    @staticmethod
    def _apply_scope_predicate(scope_predicate: ScopePredicate, plan: _CompilationPlan) -> None:
        """
        Inject the scope predicate before every other filter (DL-SCOPE-14).

        There is no early return. Every compiled statement carries a scope clause, even when that
        clause is the tautology a tenant-wide or definition-validation read produces — so
        `scope_predicate_applied` is never `False` on a path that returned rows.

        Positional binding means the predicate's named parameters are rewritten to `?` in a
        stable, sorted order — the values still travel as parameters, never inlined.
        """
        sql = scope_predicate.sql
        ordered_names = sorted(scope_predicate.parameters)
        for name in ordered_names:
            sql = sql.replace(f":{name}", "?", 1)
        plan.prepend_where(sql, *(scope_predicate.parameters[name] for name in ordered_names))
        plan.scope_applied = True

    # ── Resolution ────────────────────────────────────────────────────────────

    def _entity(self, name: str) -> SemanticEntity:
        try:
            return self._model.entity(name)
        except KeyError as exc:
            raise SemanticQueryError(str(exc)) from exc

    def _resolve_dimension(self, entity: SemanticEntity, name: str) -> Dimension:
        try:
            return entity.dimension(name)
        except KeyError as exc:
            raise SemanticQueryError(str(exc)) from exc

    def _resolve_metric(self, entity: SemanticEntity, name: str) -> Metric:
        try:
            return entity.metric(name)
        except KeyError as exc:
            raise SemanticQueryError(str(exc)) from exc

    @staticmethod
    def _enforce_access(tag: str | None, granted: frozenset[str], field_name: str) -> None:
        # OWASP A01: data-level authorization — a tagged field needs the caller to hold the tag.
        if tag is not None and tag not in granted:
            record_platform_metric(PlatformMetric.SEMANTIC_ACCESS_DENIED)
            raise AccessDeniedError(f"Access tag {tag!r} required for {field_name!r}.")


@dataclass
class _CompilationPlan:
    """Accumulates the clauses of one compiled statement in binding order."""

    dialect: str
    select_parts: list[str] = field(default_factory=list)
    group_by: list[str] = field(default_factory=list)
    join_clauses: list[str] = field(default_factory=list)
    where_clauses: list[str] = field(default_factory=list)
    parameters: list[Any] = field(default_factory=list)
    referenced_columns: list[str] = field(default_factory=list)
    scope_applied: bool = False
    _join_aliases: dict[str, str] = field(default_factory=dict)

    def add_select(self, expression: str) -> None:
        self.select_parts.append(expression)

    def add_dimension(self, column_expression: str, alias: str) -> None:
        self.select_parts.append(f"{column_expression} AS {alias}")
        self.group_by.append(column_expression)
        self.referenced_columns.append(column_expression)

    def add_expression_dimension(self, expression: str, alias: str) -> None:
        self.select_parts.append(f"{expression} AS {alias}")
        self.group_by.append(expression)

    def add_where(self, clause: str, *values: Any) -> None:
        self.where_clauses.append(clause)
        self.parameters.extend(values)

    def prepend_where(self, clause: str, *values: Any) -> None:
        self.where_clauses.insert(0, clause)
        # Parameters must lead too, or positional binding would misalign.
        self.parameters[0:0] = list(values)

    def add_join(self, join: Any, target_entity_name: str) -> str:
        alias = self._join_aliases.get(target_entity_name)
        if alias is not None:
            return alias
        alias = f"j_{len(self._join_aliases)}"
        self._join_aliases[target_entity_name] = alias
        self.join_clauses.append(
            f" {join.sql_kind()} {target_entity_name} AS {alias}"
            f" ON {_ENTITY_VIEW}.{join.local_column} = {alias}.{join.target_column}"
        )
        return alias
