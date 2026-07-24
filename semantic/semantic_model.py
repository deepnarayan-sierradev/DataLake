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

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from contracts.identifier_policy import ENTITY_TYPE_PATTERN, SAFE_COLUMN_PATTERN

Aggregation = Literal["sum", "count", "count_distinct", "avg", "min", "max"]

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

    @field_validator("name", "column")
    @classmethod
    def _check(cls, value: str) -> str:
        return _valid_name(value)


class Metric(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    aggregation: Aggregation
    column: str
    access_tag: str | None = None

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


class SemanticEntity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    entity_type: str
    dimensions: tuple[Dimension, ...] = ()
    metrics: tuple[Metric, ...] = ()

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
        return self

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

    @model_validator(mode="after")
    def _unique_entities(self) -> SemanticModel:
        names = [e.name for e in self.entities]
        if len(set(names)) != len(names):
            raise ValueError("Duplicate entity names in semantic model.")
        return self

    def entity(self, name: str) -> SemanticEntity:
        for entity in self.entities:
            if entity.name == name:
                return entity
        raise KeyError(f"No entity {name!r} in semantic model.")
