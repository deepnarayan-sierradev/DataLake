"""
Saved query (FR-3.4, DL-SEM-07).

A named, reusable semantic query — the primitive the agent re-runs on request and dashboards (C4)
bind tiles to. Stores the structured request, never SQL.

"Filters are deferred to a follow-up" is what this said until 2026-07-29, while `WAIVERS.md`
recorded DL-SEM-07 ("filters on saved queries") as implemented because `SemanticFilter` exists in
the compiler. Both halves are needed for the requirement to be real: the compiler could express a
filter and the saved query could not carry one, so no saved query could ever have had one.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from contracts.identifier_policy import ENTITY_TYPE_PATTERN, SAFE_COLUMN_PATTERN, STABLE_ID_PATTERN
from semantic.query_compiler import (
    DEFAULT_ROW_LIMIT,
    MAX_ROW_LIMIT,
    SemanticFilter,
    SemanticQueryRequest,
    TimeRangeFilter,
)
from semantic.semantic_model import TimeComparison, TimeGrain


class SavedQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query_id: str
    name: str
    entity: str
    metrics: tuple[str, ...]
    dimensions: tuple[str, ...] = ()
    created_by: str
    filters: tuple[SemanticFilter, ...] = ()
    joined_dimensions: tuple[tuple[str, str], ...] = ()
    time_dimension: str | None = None
    time_grain: TimeGrain | None = None
    time_comparison: TimeComparison = TimeComparison.NONE
    time_range: TimeRangeFilter | None = None
    row_limit: int = Field(default=DEFAULT_ROW_LIMIT, ge=1, le=MAX_ROW_LIMIT)

    @field_validator("query_id")
    @classmethod
    def _valid_query_id(cls, value: str) -> str:
        if not STABLE_ID_PATTERN.match(value):
            raise ValueError(f"query_id {value!r} is not a valid stable identifier.")
        return value

    @field_validator("entity")
    @classmethod
    def _valid_entity(cls, value: str) -> str:
        if not ENTITY_TYPE_PATTERN.match(value):
            raise ValueError(f"entity {value!r} is not a valid entity name.")
        return value

    @field_validator("metrics", "dimensions")
    @classmethod
    def _valid_names(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for name in values:
            if not SAFE_COLUMN_PATTERN.match(name):
                raise ValueError(f"{name!r} is not a valid semantic name.")
        return values

    @field_validator("name")
    @classmethod
    def _non_empty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be empty.")
        return value

    def to_request(self) -> SemanticQueryRequest:
        return SemanticQueryRequest(
            entity=self.entity,
            metrics=self.metrics,
            dimensions=self.dimensions,
            filters=self.filters,
            joined_dimensions=self.joined_dimensions,
            time_dimension=self.time_dimension,
            time_grain=self.time_grain,
            time_comparison=self.time_comparison,
            time_range=self.time_range,
            row_limit=self.row_limit,
        )
