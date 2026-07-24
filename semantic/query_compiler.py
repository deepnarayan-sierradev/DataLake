"""
Semantic query compiler (FR-2.3 / FR-2.4 / FR-2.5).

Turns a structured SemanticQueryRequest (metrics, dimensions, filters — all
referenced by business name) into a parameterized SQL string against the
entity's physical view. Callers never supply SQL or column names: every
identifier is resolved from the validated SemanticModel, filter values are
bound as parameters (never interpolated), and access tags are enforced before
a field appears in the query. Unknown metric/dimension names raise (FR-2.4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from semantic.semantic_model import Dimension, Metric, SemanticEntity, SemanticModel

FilterOperator = Literal["eq", "ne", "gt", "gte", "lt", "lte"]

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


class SemanticQueryError(Exception):
    """Raised when a semantic query cannot be compiled."""


class AccessDeniedError(SemanticQueryError):
    """Raised when the caller lacks an access tag required by a referenced field."""


@dataclass(frozen=True)
class SemanticFilter:
    dimension: str
    operator: FilterOperator
    value: Any


@dataclass(frozen=True)
class SemanticQueryRequest:
    entity: str
    metrics: tuple[str, ...]
    dimensions: tuple[str, ...] = ()
    filters: tuple[SemanticFilter, ...] = ()


@dataclass(frozen=True)
class CompiledQuery:
    sql: str
    parameters: list[Any] = field(default_factory=list)
    view_name: str = _ENTITY_VIEW


class QueryCompiler:
    def __init__(self, model: SemanticModel) -> None:
        self._model = model

    def compile(
        self, request: SemanticQueryRequest, *, granted_access_tags: frozenset[str]
    ) -> CompiledQuery:
        if not request.metrics:
            raise SemanticQueryError("A semantic query must request at least one metric.")
        try:
            entity = self._model.entity(request.entity)
        except KeyError as exc:
            raise SemanticQueryError(str(exc)) from exc

        select_parts: list[str] = []
        group_by: list[str] = []
        for dimension_name in request.dimensions:
            dimension = self._resolve_dimension(entity, dimension_name)
            self._enforce_access(dimension.access_tag, granted_access_tags, dimension_name)
            select_parts.append(f"{dimension.column} AS {dimension.name}")
            group_by.append(dimension.column)
        for metric_name in request.metrics:
            metric = self._resolve_metric(entity, metric_name)
            self._enforce_access(metric.access_tag, granted_access_tags, metric_name)
            select_parts.append(f"{metric.sql_aggregation()}({metric.column}) AS {metric.name}")

        where_clause, parameters = self._build_where(entity, request.filters, granted_access_tags)
        columns_sql = ", ".join(select_parts)
        sql = f"SELECT {columns_sql} FROM {_ENTITY_VIEW}"  # noqa: S608  # nosec B608
        if where_clause:
            sql += f" WHERE {where_clause}"
        if group_by:
            sql += f" GROUP BY {', '.join(group_by)}"
        return CompiledQuery(sql=sql, parameters=parameters)

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
            raise AccessDeniedError(f"Access tag {tag!r} required for {field_name!r}.")

    def _build_where(
        self, entity: SemanticEntity, filters: tuple[SemanticFilter, ...], granted: frozenset[str]
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        for query_filter in filters:
            dimension = self._resolve_dimension(entity, query_filter.dimension)
            self._enforce_access(dimension.access_tag, granted, query_filter.dimension)
            operator = _SQL_OPERATORS[query_filter.operator]
            clauses.append(f"{dimension.column} {operator} ?")
            parameters.append(query_filter.value)
        return " AND ".join(clauses), parameters
