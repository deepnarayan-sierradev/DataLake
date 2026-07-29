"""
Source connection as a first-class entity (DL-SCOPE-03, DL-SCOPE-04, DL-SCOPE-06).

A connection is one instance of a connector bound to credentials and an owner, and it
is the identity component that replaces `source_id` in every composite key — twelve
franchisees on HubSpot are twelve connections of one `source_id`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, Field, field_validator, model_validator

from contracts.identifier_policy import validate_stable_id, validate_tenant_code
from tenancy.scope_contract import (
    AttributionMode,
    validate_scope_unit_id,
)


class ConnectionOwnerType(StrEnum):
    """Who owns a connection — the tenant itself, or one of its scope units."""

    TENANT = "tenant"
    SCOPE_UNIT = "scope_unit"


class ConnectionState(StrEnum):
    """Connection lifecycle (DL-SCOPE-08)."""

    PENDING = "pending"
    ACTIVE = "active"
    FAILING = "failing"
    SUSPENDED = "suspended"
    RETIRED = "retired"


# States from which a connection may still be extracted.
EXTRACTABLE_STATES: Final[frozenset[ConnectionState]] = frozenset(
    {ConnectionState.ACTIVE, ConnectionState.FAILING}
)

_VALID_TRANSITIONS: Final[dict[ConnectionState, frozenset[ConnectionState]]] = {
    ConnectionState.PENDING: frozenset({ConnectionState.ACTIVE, ConnectionState.RETIRED}),
    ConnectionState.ACTIVE: frozenset(
        {ConnectionState.FAILING, ConnectionState.SUSPENDED, ConnectionState.RETIRED}
    ),
    ConnectionState.FAILING: frozenset(
        {ConnectionState.ACTIVE, ConnectionState.SUSPENDED, ConnectionState.RETIRED}
    ),
    ConnectionState.SUSPENDED: frozenset({ConnectionState.ACTIVE, ConnectionState.RETIRED}),
    ConnectionState.RETIRED: frozenset(),
}


class ConnectionStateTransitionError(Exception):
    """Raised when a connection lifecycle transition is not permitted."""


def validate_connection_transition(current: ConnectionState, target: ConnectionState) -> None:
    """Reject an illegal lifecycle move; retirement is terminal and retains data."""
    if target not in _VALID_TRANSITIONS[current]:
        raise ConnectionStateTransitionError(
            f"Connection state transition {current.value!r} -> {target.value!r} is not "
            f"permitted. Allowed from {current.value!r}: "
            f"{sorted(s.value for s in _VALID_TRANSITIONS[current])}."
        )


def connection_credential_path(tenant_code: str, connection_id: str) -> str:
    """Per-connection read credential path (DL-SCOPE-06 supersedes the per-source path)."""
    validate_tenant_code(tenant_code)
    validate_stable_id(connection_id, "connection_id")
    return f"edl/tenants/{tenant_code}/connections/{connection_id}/credentials"


def connection_writeback_credential_path(tenant_code: str, connection_id: str) -> str:
    """Write-back credentials are a separate secret so a read deployment cannot mutate."""
    return f"{connection_credential_path(tenant_code, connection_id)}-writeback"


class SourceConnection(BaseModel):
    """One connector instance bound to credentials and an owner."""

    model_config = {"frozen": True, "extra": "forbid"}

    tenant_code: str
    connection_id: str
    source_id: str = Field(
        description="Connector type this connection instantiates; routing and display only."
    )
    display_name: str = Field(min_length=1, max_length=200)
    owner_type: ConnectionOwnerType = ConnectionOwnerType.TENANT
    owner_id: str | None = Field(
        default=None,
        description="scope_unit_id when owner_type is scope_unit; None when tenant-owned.",
    )
    state: ConnectionState = ConnectionState.PENDING
    attribution_mode: AttributionMode = AttributionMode.PROVENANCE_DERIVED
    scope_attribution_field: str | None = Field(
        default=None,
        max_length=128,
        description="Curated column carrying the scope unit for field-derived attribution.",
    )
    capability_overrides: dict[str, bool] = Field(default_factory=dict)
    rate_limit_policy: str | None = Field(
        default=None, description="Registered RateLimitPolicy name; binds per connection."
    )
    shares_source_quota: bool = Field(
        default=False,
        description="True when the provider quota is shared across all of a tenant's connections.",
    )
    last_successful_run_at: datetime | None = None
    credential_verified_at: datetime | None = None
    retired_at: datetime | None = None
    write_back_enabled: bool = False

    @field_validator("tenant_code")
    @classmethod
    def _validate_tenant(cls, value: str) -> str:
        return validate_tenant_code(value)

    @field_validator("connection_id")
    @classmethod
    def _validate_connection_id(cls, value: str) -> str:
        return validate_stable_id(value, "connection_id")

    @field_validator("source_id")
    @classmethod
    def _validate_source_id(cls, value: str) -> str:
        return validate_stable_id(value, "source_id")

    @model_validator(mode="after")
    def _validate_ownership_and_attribution(self) -> SourceConnection:
        if self.owner_type is ConnectionOwnerType.SCOPE_UNIT:
            if not self.owner_id:
                raise ValueError(
                    f"connection {self.connection_id!r}: owner_id is required when owner_type "
                    "is 'scope_unit' — provenance-derived attribution needs the owning unit."
                )
            validate_scope_unit_id(self.owner_id, "owner_id")
            if self.attribution_mode is not AttributionMode.PROVENANCE_DERIVED:
                raise ValueError(
                    f"connection {self.connection_id!r}: a scope-unit-owned connection is "
                    "provenance-derived by construction; field-derived attribution would "
                    "override the more trustworthy signal."
                )
        elif self.owner_id is not None:
            raise ValueError(
                f"connection {self.connection_id!r}: owner_id must be absent for a "
                "tenant-owned connection."
            )
        if (
            self.attribution_mode is AttributionMode.FIELD_DERIVED
            and not self.scope_attribution_field
        ):
            raise ValueError(
                f"connection {self.connection_id!r}: field-derived attribution requires "
                "scope_attribution_field."
            )
        if self.state is ConnectionState.RETIRED and self.retired_at is None:
            raise ValueError(f"connection {self.connection_id!r}: retired_at is required.")
        return self

    @property
    def credential_path(self) -> str:
        return connection_credential_path(self.tenant_code, self.connection_id)

    @property
    def writeback_credential_path(self) -> str:
        return connection_writeback_credential_path(self.tenant_code, self.connection_id)

    @property
    def is_extractable(self) -> bool:
        return self.state in EXTRACTABLE_STATES

    def owning_scope_unit_id(self) -> str | None:
        """The scope unit rows from this connection belong to, or None for tenant-level."""
        return self.owner_id if self.owner_type is ConnectionOwnerType.SCOPE_UNIT else None

    def transitioned_to(self, target: ConnectionState) -> SourceConnection:
        """Return a copy in `target` state, rejecting an illegal transition."""
        validate_connection_transition(self.state, target)
        updates: dict[str, object] = {"state": target}
        if target is ConnectionState.RETIRED:
            updates["retired_at"] = datetime.now(UTC)
        return self.model_copy(update=updates)


def default_connection_for_source(tenant_code: str, source_id: str) -> SourceConnection:
    """Migration identity: an existing single-connection source gets connection_id == source_id."""
    return SourceConnection(
        tenant_code=tenant_code,
        connection_id=source_id,
        source_id=source_id,
        display_name=source_id,
        owner_type=ConnectionOwnerType.TENANT,
        state=ConnectionState.ACTIVE,
    )
