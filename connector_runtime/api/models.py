"""
Pydantic request models for the control-plane API.

Every request body reaching a control-plane handler is validated against one
of these models (extra="forbid") — or, for entity registration, against
EntityExtractionConfig directly — before any AWS API call is made (OWASP A03).
Identifier fields reuse the platform-wide patterns from
contracts.identifier_policy; validation is never re-implemented locally.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from contracts.identifier_policy import (
    ENTITY_TYPE_PATTERN,
    SAFE_COLUMN_PATTERN,
    STABLE_ID_PATTERN,
    validate_stable_id,
)


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


class SemanticQueryBody(BaseModel):
    """Request body for POST /tenants/{tenant_code}/semantic/query."""

    model_config = {"extra": "forbid"}

    entity: str = Field(..., min_length=1, max_length=64)
    metrics: list[str] = Field(..., min_length=1)
    dimensions: list[str] = Field(default_factory=list)

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


class SavedQueryCreateBody(BaseModel):
    """Request body for POST /tenants/{tenant_code}/saved-queries (created_by is server-set)."""

    model_config = {"extra": "forbid"}

    query_id: str = Field(..., min_length=2, max_length=64)
    name: str = Field(..., min_length=1, max_length=128)
    entity: str = Field(..., min_length=1, max_length=64)
    metrics: list[str] = Field(..., min_length=1)
    dimensions: list[str] = Field(default_factory=list)

    @field_validator("query_id")
    @classmethod
    def _validate_query_id(cls, value: str) -> str:
        if not STABLE_ID_PATTERN.match(value):
            raise ValueError(f"query_id {value!r} is not a valid stable identifier.")
        return value

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
