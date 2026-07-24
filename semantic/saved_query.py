"""
Saved query (FR-3.4).

A named, reusable semantic query — the primitive the agent re-runs on request
and dashboards (C4) bind tiles to. Stores the structured request (entity +
metrics + dimensions), never SQL. Filters are deferred to a follow-up.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

from contracts.identifier_policy import ENTITY_TYPE_PATTERN, SAFE_COLUMN_PATTERN, STABLE_ID_PATTERN
from semantic.query_compiler import SemanticQueryRequest


class SavedQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query_id: str
    name: str
    entity: str
    metrics: tuple[str, ...]
    dimensions: tuple[str, ...] = ()
    created_by: str

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
            entity=self.entity, metrics=self.metrics, dimensions=self.dimensions
        )
