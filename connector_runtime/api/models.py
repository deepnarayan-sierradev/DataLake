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

from contracts.identifier_policy import validate_stable_id, validate_tenant_code


class TenantProvisionRequest(BaseModel):
    """Request body for POST /tenants."""

    model_config = {"extra": "forbid"}

    tenant_code: str = Field(
        ...,
        min_length=2,
        max_length=48,
        description="Tenant identifier slug to provision (e.g. 'acme-corp').",
    )

    @field_validator("tenant_code")
    @classmethod
    def _validate_tenant_code(cls, value: str) -> str:
        return validate_tenant_code(value)


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
