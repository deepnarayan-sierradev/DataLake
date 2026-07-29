"""
Pydantic request models for the control-plane API.

Every request body reaching a control-plane handler is validated against one
of these models (extra="forbid") — or, for entity registration, against
EntityExtractionConfig directly — before any AWS API call is made (OWASP A03).
Identifier fields reuse the platform-wide patterns from
contracts.identifier_policy; validation is never re-implemented locally.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, field_validator

from contracts.identifier_policy import (
    ENTITY_TYPE_PATTERN,
    SAFE_COLUMN_PATTERN,
    STABLE_ID_PATTERN,
    validate_stable_id,
)
from semantic.query_compiler import (
    DEFAULT_ROW_LIMIT,
    MAX_IN_LIST_VALUES,
    MAX_ROW_LIMIT,
    FilterOperator,
    RelativeDateRange,
    SemanticFilter,
    SemanticQueryRequest,
    TimeRangeFilter,
)
from semantic.semantic_model import TimeComparison, TimeGrain


class PipelineTriggerRequest(BaseModel):
    """Request body for POST /tenants/{tenant_code}/pipelines/trigger."""

    model_config = {"extra": "forbid"}

    source_id: str = Field(..., min_length=2, max_length=64)
    entity_id: str = Field(..., min_length=2, max_length=64)
    connector_params: dict[str, str] = Field(default_factory=dict)
    is_replay: bool = Field(default=False)

    @field_validator("source_id")
    @classmethod
    def _validate_source_id(cls, value: str) -> str:
        return validate_stable_id(value, field_name="source_id")

    @field_validator("entity_id")
    @classmethod
    def _validate_entity_id(cls, value: str) -> str:
        return validate_stable_id(value, field_name="entity_id")


class SemanticFilterBody(BaseModel):
    """One filter over a declared dimension; the value is bound, never interpolated."""

    model_config = {"extra": "forbid"}

    dimension: str = Field(..., min_length=1, max_length=64)
    operator: FilterOperator
    value: str | float | bool | None = None
    values: list[str | float | bool] = Field(default_factory=list, max_length=MAX_IN_LIST_VALUES)

    @field_validator("dimension")
    @classmethod
    def _validate_dimension(cls, value: str) -> str:
        if not SAFE_COLUMN_PATTERN.match(value):
            raise ValueError(f"{value!r} is not a valid semantic name.")
        return value

    def to_filter(self) -> SemanticFilter:
        return SemanticFilter(
            dimension=self.dimension,
            operator=self.operator,
            value=self.value,
            values=tuple(self.values),
        )


class TimeRangeBody(BaseModel):
    """A range over a declared time dimension — a named relative range or absolute bounds."""

    model_config = {"extra": "forbid"}

    time_dimension: str = Field(..., min_length=1, max_length=64)
    relative_range: RelativeDateRange | None = None
    start: date | None = None
    end: date | None = None

    @field_validator("time_dimension")
    @classmethod
    def _validate_time_dimension(cls, value: str) -> str:
        if not SAFE_COLUMN_PATTERN.match(value):
            raise ValueError(f"{value!r} is not a valid semantic name.")
        return value

    def to_filter(self) -> TimeRangeFilter:
        return TimeRangeFilter(
            time_dimension=self.time_dimension,
            relative_range=self.relative_range,
            start=self.start,
            end=self.end,
        )


class JoinedDimensionBody(BaseModel):
    """A dimension read from a joined entity; the join path itself comes from the model."""

    model_config = {"extra": "forbid"}

    entity: str = Field(..., min_length=1, max_length=64)
    dimension: str = Field(..., min_length=1, max_length=64)

    @field_validator("entity")
    @classmethod
    def _validate_entity(cls, value: str) -> str:
        if not ENTITY_TYPE_PATTERN.match(value):
            raise ValueError(f"entity {value!r} is not a valid entity name.")
        return value

    @field_validator("dimension")
    @classmethod
    def _validate_dimension(cls, value: str) -> str:
        if not SAFE_COLUMN_PATTERN.match(value):
            raise ValueError(f"{value!r} is not a valid semantic name.")
        return value


class SemanticQueryShape(BaseModel):
    """
    The structured query surface, shared by the ad-hoc route and a saved query.

    This carried only `entity`, `metrics`, and `dimensions` until 2026-07-29, while
    `SemanticQueryRequest` already supported filters, fiscal time grains, period comparisons, and
    joins. The compiler's whole feature set was therefore unreachable from the only HTTP surface —
    no dashboard could ask for "revenue last quarter, filtered to one region" — and `WAIVERS.md`
    recorded DL-SEM-07 as implemented on the strength of the compiler alone.

    Nothing here accepts SQL or a column name: every identifier is resolved against the validated
    `SemanticModel`, and every filter value travels as a bound parameter (OWASP A03).
    """

    model_config = {"extra": "forbid"}

    entity: str = Field(..., min_length=1, max_length=64)
    metrics: list[str] = Field(..., min_length=1, max_length=50)
    dimensions: list[str] = Field(default_factory=list, max_length=50)
    filters: list[SemanticFilterBody] = Field(default_factory=list, max_length=50)
    joined_dimensions: list[JoinedDimensionBody] = Field(default_factory=list, max_length=20)
    time_dimension: str | None = Field(default=None, max_length=64)
    time_grain: TimeGrain | None = None
    time_comparison: TimeComparison = TimeComparison.NONE
    time_range: TimeRangeBody | None = None
    row_limit: int = Field(default=DEFAULT_ROW_LIMIT, ge=1, le=MAX_ROW_LIMIT)

    @field_validator("entity")
    @classmethod
    def _validate_entity(cls, value: str) -> str:
        if not ENTITY_TYPE_PATTERN.match(value):
            raise ValueError(f"entity {value!r} is not a valid entity name.")
        return value

    @field_validator("metrics", "dimensions")
    @classmethod
    def _validate_names(cls, values: list[str]) -> list[str]:
        for name in values:
            if not SAFE_COLUMN_PATTERN.match(name):
                raise ValueError(f"{name!r} is not a valid semantic name.")
        return values

    @field_validator("time_dimension")
    @classmethod
    def _validate_time_dimension(cls, value: str | None) -> str | None:
        if value is not None and not SAFE_COLUMN_PATTERN.match(value):
            raise ValueError(f"{value!r} is not a valid semantic name.")
        return value

    def to_request(self) -> SemanticQueryRequest:
        """Convert to the compiler's request; validation errors surface as SemanticQueryError."""
        return SemanticQueryRequest(
            entity=self.entity,
            metrics=tuple(self.metrics),
            dimensions=tuple(self.dimensions),
            filters=tuple(body.to_filter() for body in self.filters),
            joined_dimensions=tuple(
                (body.entity, body.dimension) for body in self.joined_dimensions
            ),
            time_dimension=self.time_dimension,
            time_grain=self.time_grain,
            time_comparison=self.time_comparison,
            time_range=self.time_range.to_filter() if self.time_range else None,
            row_limit=self.row_limit,
        )


class SemanticQueryBody(SemanticQueryShape):
    """Request body for POST /tenants/{tenant_code}/semantic/query."""


class SavedQueryCreateBody(SemanticQueryShape):
    """Request body for POST /tenants/{tenant_code}/saved-queries (created_by is server-set)."""

    query_id: str = Field(..., min_length=2, max_length=64)
    name: str = Field(..., min_length=1, max_length=128)

    @field_validator("query_id")
    @classmethod
    def _validate_query_id(cls, value: str) -> str:
        if not STABLE_ID_PATTERN.match(value):
            raise ValueError(f"query_id {value!r} is not a valid stable identifier.")
        return value
