"""
Semantic model (FR-2.1 / FR-2.4).

Governed, declarative definitions of business entities, dimensions and metrics
mapped to physical analytics columns. Every physical column and metric
aggregation is validated here so the query compiler can build SQL from trusted,
model-declared identifiers only — callers never supply raw SQL or column names.

Access scoping (FR-2.5): a metric or dimension may carry an ``access_tag``; the
compiler enforces the caller holds that tag before it appears in a query.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from contracts.identifier_policy import ENTITY_TYPE_PATTERN, SAFE_COLUMN_PATTERN

Aggregation = Literal["sum", "count", "count_distinct", "avg", "min", "max"]


class TimeGrain(StrEnum):
    """Declared grain of a time dimension (DL-SEM-02)."""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"
    YEAR = "year"


class TimeComparison(StrEnum):
    """Derived comparison operators over a time dimension (DL-SEM-02)."""

    NONE = "none"
    PRIOR_PERIOD = "prior_period"
    PRIOR_YEAR = "prior_year"
    PERIOD_TO_DATE = "period_to_date"


class JoinKind(StrEnum):
    """Typed join between two semantic entities (DL-SEM-01)."""

    INNER = "inner"
    LEFT = "left"


class MetricKind(StrEnum):
    """Simple aggregate, or a ratio/derived expression over two metrics (DL-SEM-09)."""

    AGGREGATE = "aggregate"
    RATIO = "ratio"
    DIFFERENCE = "difference"


class NullDenominatorBehaviour(StrEnum):
    """Explicit zero-denominator semantics, so a conversion rate is defined once."""

    NULL = "null"
    ZERO = "zero"


_SQL_JOIN_KINDS: dict[str, str] = {"inner": "INNER JOIN", "left": "LEFT JOIN"}

_SQL_AGGREGATIONS: dict[str, str] = {
    "sum": "SUM",
    "count": "COUNT",
    "count_distinct": "COUNT",
    "avg": "AVG",
    "min": "MIN",
    "max": "MAX",
}


def _valid_name(value: str) -> str:
    if not SAFE_COLUMN_PATTERN.match(value):
        raise ValueError(f"{value!r} is not a valid semantic name.")
    return value


class Dimension(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    column: str
    access_tag: str | None = None
    business_owner: str | None = None
    steward: str | None = None
    classification: str = "internal"
    description: str = ""

    @field_validator("name", "column")
    @classmethod
    def _check(cls, value: str) -> str:
        return _valid_name(value)


class TimeDimension(BaseModel):
    """First-class time dimension with a declared grain (DL-SEM-02)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    column: str
    grain: TimeGrain = TimeGrain.DAY
    supports_fiscal: bool = True
    access_tag: str | None = None
    description: str = ""

    @field_validator("name", "column")
    @classmethod
    def _check(cls, value: str) -> str:
        return _valid_name(value)


class SemanticJoin(BaseModel):
    """Declared join path from one entity to another (DL-SEM-01)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_entity: str
    kind: JoinKind = JoinKind.LEFT
    local_column: str
    target_column: str

    @field_validator("target_entity")
    @classmethod
    def _check_target(cls, value: str) -> str:
        if not ENTITY_TYPE_PATTERN.match(value):
            raise ValueError(f"{value!r} is not a valid entity name.")
        return value

    @field_validator("local_column", "target_column")
    @classmethod
    def _check_columns(cls, value: str) -> str:
        return _valid_name(value)

    def sql_kind(self) -> str:
        return _SQL_JOIN_KINDS[self.kind.value]


class Metric(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    aggregation: Aggregation
    column: str
    access_tag: str | None = None
    kind: MetricKind = MetricKind.AGGREGATE
    numerator_metric: str | None = None
    denominator_metric: str | None = None
    null_denominator: NullDenominatorBehaviour = NullDenominatorBehaviour.NULL
    business_owner: str | None = None
    steward: str | None = None
    classification: str = "internal"
    definition: str = ""
    definition_signed_by: str | None = None
    definition_signed_at: str | None = None
    unit: str = ""

    @field_validator("name")
    @classmethod
    def _check_name(cls, value: str) -> str:
        return _valid_name(value)

    @field_validator("column")
    @classmethod
    def _check_column(cls, value: str) -> str:
        if value == "*":
            return value
        return _valid_name(value)

    def sql_aggregation(self) -> str:
        return _SQL_AGGREGATIONS[self.aggregation]

    @model_validator(mode="after")
    def _validate_derived_metric(self) -> Metric:
        if self.kind is MetricKind.AGGREGATE:
            if self.numerator_metric or self.denominator_metric:
                raise ValueError(
                    f"metric {self.name!r}: an aggregate metric must not name a numerator or "
                    "denominator; declare kind='ratio' or 'difference' instead."
                )
            return self
        if not (self.numerator_metric and self.denominator_metric):
            raise ValueError(
                f"metric {self.name!r}: a {self.kind.value} metric must name both a numerator "
                "and a denominator metric (DL-SEM-09)."
            )
        return self

    @property
    def is_derived(self) -> bool:
        return self.kind is not MetricKind.AGGREGATE

    @property
    def is_signed(self) -> bool:
        """A KPI is not done until its named business owner has signed the definition."""
        return bool(self.definition and self.definition_signed_by and self.business_owner)


class SemanticEntity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    entity_type: str
    dimensions: tuple[Dimension, ...] = ()
    metrics: tuple[Metric, ...] = ()
    time_dimensions: tuple[TimeDimension, ...] = ()
    joins: tuple[SemanticJoin, ...] = ()
    business_owner: str | None = None
    steward: str | None = None
    classification: str = "internal"
    definition: str = ""

    @field_validator("name", "entity_type")
    @classmethod
    def _check_type(cls, value: str) -> str:
        if not ENTITY_TYPE_PATTERN.match(value):
            raise ValueError(f"{value!r} is not a valid entity name.")
        return value

    @model_validator(mode="after")
    def _unique_names(self) -> SemanticEntity:
        dim_names = [d.name for d in self.dimensions]
        metric_names = [m.name for m in self.metrics]
        if len(set(dim_names)) != len(dim_names):
            raise ValueError(f"Duplicate dimension names in entity {self.name!r}.")
        if len(set(metric_names)) != len(metric_names):
            raise ValueError(f"Duplicate metric names in entity {self.name!r}.")
        time_names = [t.name for t in self.time_dimensions]
        if len(set(time_names)) != len(time_names):
            raise ValueError(f"Duplicate time dimension names in entity {self.name!r}.")
        overlap = set(dim_names) & set(time_names)
        if overlap:
            raise ValueError(
                f"Entity {self.name!r}: {sorted(overlap)} declared as both a dimension and a "
                "time dimension; a name must resolve to exactly one field."
            )
        join_targets = [j.target_entity for j in self.joins]
        if len(set(join_targets)) != len(join_targets):
            raise ValueError(
                f"Entity {self.name!r} declares more than one join to the same target entity; "
                "an ambiguous join path is rejected, not guessed."
            )
        for derived in self.metrics:
            if not derived.is_derived:
                continue
            available = set(metric_names)
            missing = {derived.numerator_metric, derived.denominator_metric} - available
            if missing:
                raise ValueError(
                    f"Metric {derived.name!r} references undefined metric(s) "
                    f"{sorted(m for m in missing if m)} in entity {self.name!r}."
                )
        return self

    def time_dimension(self, name: str) -> TimeDimension:
        for candidate in self.time_dimensions:
            if candidate.name == name:
                return candidate
        raise KeyError(f"No time dimension {name!r} in entity {self.name!r}.")

    def join_to(self, target_entity: str) -> SemanticJoin:
        for candidate in self.joins:
            if candidate.target_entity == target_entity:
                return candidate
        raise KeyError(f"No declared join from {self.name!r} to {target_entity!r}.")

    def dimension(self, name: str) -> Dimension:
        for dimension in self.dimensions:
            if dimension.name == name:
                return dimension
        raise KeyError(f"No dimension {name!r} in entity {self.name!r}.")

    def metric(self, name: str) -> Metric:
        for metric in self.metrics:
            if metric.name == name:
                return metric
        raise KeyError(f"No metric {name!r} in entity {self.name!r}.")


class SemanticModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_code: str
    model_version: str
    entities: tuple[SemanticEntity, ...]
    fiscal_year_start_month: int = Field(default=1, ge=1, le=12)
    fiscal_week_start_weekday: int = Field(default=0, ge=0, le=6)

    @model_validator(mode="after")
    def _unique_entities(self) -> SemanticModel:
        names = [e.name for e in self.entities]
        if len(set(names)) != len(names):
            raise ValueError("Duplicate entity names in semantic model.")
        known = set(names)
        for entity in self.entities:
            for join in entity.joins:
                if join.target_entity not in known:
                    raise ValueError(
                        f"Entity {entity.name!r} joins to {join.target_entity!r}, which is not "
                        "in the model. Join paths are validated at publish (DL-SEM-01)."
                    )
        return self

    def unowned_fields(self) -> list[str]:
        """Every entity, dimension, or metric with no business owner (DL-SEM-06)."""
        unowned: list[str] = []
        for entity in self.entities:
            if not entity.business_owner:
                unowned.append(f"entity:{entity.name}")
            unowned.extend(
                f"dimension:{entity.name}.{d.name}"
                for d in entity.dimensions
                if not d.business_owner
            )
            unowned.extend(
                f"metric:{entity.name}.{m.name}" for m in entity.metrics if not m.business_owner
            )
        return unowned

    def unsigned_metrics(self) -> list[str]:
        """Metrics whose definition the named owner has not signed (DL-SEM-04)."""
        return [
            f"{entity.name}.{metric.name}"
            for entity in self.entities
            for metric in entity.metrics
            if not metric.is_signed
        ]

    def entity(self, name: str) -> SemanticEntity:
        for entity in self.entities:
            if entity.name == name:
                return entity
        raise KeyError(f"No entity {name!r} in semantic model.")
