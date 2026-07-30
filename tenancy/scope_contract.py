"""
Scope-unit dimension below `tenant_code` (DL-SCOPE-01, DL-SCOPE-02, DL-SCOPE-11).

Enforcement is degenerate, never conditional: a `single` tenant has exactly one
implicit scope unit and the predicate still runs, so there is no configuration in
which isolation is skipped (DL-12 design decision D1).
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, Field, field_validator, model_validator

from contracts.identifier_policy import TENANT_CODE_PATTERN, validate_tenant_code

SCOPE_UNIT_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z_][a-z0-9_\-]{1,63}$")

IMPLICIT_SCOPE_UNIT_ID: Final[str] = "__tenant__"


class PartitionModel(StrEnum):
    """Whether a tenant is divided into scope units."""

    SINGLE = "single"
    PARTITIONED = "partitioned"


class PartitionKind(StrEnum):
    """Terminology label for a partitioned tenant's units; the mechanism is identical."""

    FRANCHISE = "franchise"
    REGION = "region"
    SUBSIDIARY = "subsidiary"
    LEGAL_ENTITY = "legal_entity"
    BUSINESS_UNIT = "business_unit"


class ResolutionScope(StrEnum):
    """Grain at which entity resolution may merge records (DL-SCOPE-12)."""

    TENANT = "tenant"
    SCOPE_UNIT = "scope_unit"


class AttributionMode(StrEnum):
    """How a row's owning scope unit is established (DL-12 design decision D2)."""

    PROVENANCE_DERIVED = "provenance_derived"
    FIELD_DERIVED = "field_derived"


class HistoryInheritancePolicy(StrEnum):
    """Whether a new owner of a scope unit inherits the prior owner's data (DL-SCOPE-11)."""

    NONE = "none"
    FULL = "full"


def validate_scope_unit_id(
    value: str, field_name: str = "scope_unit_id", *, allow_reserved: bool = False
) -> str:
    """
    Validate a scope unit identifier against the scope-unit charset.

    `allow_reserved` permits `IMPLICIT_SCOPE_UNIT_ID`, which is legitimate *inside a claim* for a
    single-partition tenant but must never be **registered** as a real unit or derived from a
    record field: the sentinel satisfies SCOPE_UNIT_ID_PATTERN, so a unit literally named
    `__tenant__` in a partitioned tenant would collapse `scope_predicate()` to match-all and
    expose every other unit's rows (DL-SCOPE-02).
    """
    if value == IMPLICIT_SCOPE_UNIT_ID and not allow_reserved:
        raise ValueError(
            f"{field_name} {value!r} is reserved for the implicit single-tenant unit and "
            "must not be registered as a scope unit."
        )
    if not SCOPE_UNIT_ID_PATTERN.match(value):
        raise ValueError(
            f"{field_name} {value!r} does not conform to the scope unit id format. "
            "Use lowercase letters, digits, hyphens, and underscores only (2-64 chars; "
            "must start with a letter or underscore). Example: 'franchisee-0042'."
        )
    return value


class ScopeUnit(BaseModel):
    """A sub-tenant isolation boundary — one franchisee, region, subsidiary, or entity."""

    model_config = {"frozen": True, "extra": "forbid"}

    tenant_code: str
    scope_unit_id: str
    partition_kind: PartitionKind
    display_name: str = Field(min_length=1, max_length=200)
    external_reference: str | None = Field(
        default=None,
        max_length=128,
        description="Franchisee number, legal entity code, or other customer-side identifier.",
    )
    parent_scope_unit_id: str | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    history_inheritance: HistoryInheritancePolicy = HistoryInheritancePolicy.NONE
    active: bool = True

    @field_validator("tenant_code")
    @classmethod
    def _validate_tenant(cls, value: str) -> str:
        return validate_tenant_code(value)

    @field_validator("scope_unit_id", "parent_scope_unit_id")
    @classmethod
    def _validate_scope_unit(cls, value: str | None) -> str | None:
        return None if value is None else validate_scope_unit_id(value)

    @model_validator(mode="after")
    def _validate_dates_and_parent(self) -> ScopeUnit:
        if self.effective_from and self.effective_to and self.effective_to < self.effective_from:
            raise ValueError(
                f"scope unit {self.scope_unit_id!r}: effective_to precedes effective_from."
            )
        if self.parent_scope_unit_id == self.scope_unit_id:
            raise ValueError(f"scope unit {self.scope_unit_id!r} cannot be its own parent.")
        return self

    def is_effective_on(self, when: date | None = None) -> bool:
        """Whether this unit is in force on `when` (defaults to today, UTC)."""
        moment = when or datetime.now(UTC).date()
        if not self.active:
            return False
        if self.effective_from and moment < self.effective_from:
            return False
        return not (self.effective_to and moment > self.effective_to)


class TenantPartitionProfile(BaseModel):
    """Tenant-level declaration of the partition model (DL-SCOPE-02)."""

    model_config = {"frozen": True, "extra": "forbid"}

    tenant_code: str
    partition_model: PartitionModel = PartitionModel.SINGLE
    partition_kind: PartitionKind | None = None
    minimum_benchmark_cohort_size: int = Field(
        default=5,
        ge=2,
        le=1000,
        description="k-anonymity floor for peer benchmarks (DL-SCOPE-16).",
    )

    @field_validator("tenant_code")
    @classmethod
    def _validate_tenant(cls, value: str) -> str:
        if not TENANT_CODE_PATTERN.match(value):
            raise ValueError(f"tenant_code {value!r} does not conform to the tenant code format.")
        return value

    @model_validator(mode="after")
    def _validate_kind_matches_model(self) -> TenantPartitionProfile:
        if self.partition_model is PartitionModel.PARTITIONED and self.partition_kind is None:
            raise ValueError(
                f"tenant {self.tenant_code!r} declares partition_model='partitioned' but no "
                "partition_kind. A partitioned tenant must declare the kind of its units."
            )
        if self.partition_model is PartitionModel.SINGLE and self.partition_kind is not None:
            raise ValueError(
                f"tenant {self.tenant_code!r} declares partition_kind while partition_model is "
                "'single'. A single-partition tenant has exactly one implicit unit."
            )
        return self

    @property
    def default_resolution_scope(self) -> ResolutionScope:
        """Separate businesses do not merge; a single tenant consolidates (DL-12 D3)."""
        if self.partition_model is PartitionModel.PARTITIONED:
            return ResolutionScope.SCOPE_UNIT
        return ResolutionScope.TENANT

    def implicit_units(self) -> tuple[str, ...]:
        """The unit set of a `single` tenant, empty for a partitioned one."""
        if self.partition_model is PartitionModel.SINGLE:
            return (IMPLICIT_SCOPE_UNIT_ID,)
        return ()
