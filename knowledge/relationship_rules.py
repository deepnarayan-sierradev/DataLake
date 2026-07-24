"""
Relationship rules (FR-1.2).

Declarative, versioned definitions of edges between analytics-layer entity
types (e.g. contract -> company). A rule is a deterministic key join: the
``from_field`` on the source entity's golden record equals the ``to_field`` on
the target entity's golden record. Rules are authored per tenant and validated
here so the resolver can build set-based SQL from trusted identifiers only
(OWASP A03 — every field name is allowlisted before it reaches a query).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

from contracts.identifier_policy import ENTITY_TYPE_PATTERN, SAFE_COLUMN_PATTERN


class RelationshipRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    relationship_type: str
    from_entity_type: str
    to_entity_type: str
    from_field: str
    to_field: str

    @field_validator("relationship_type", "from_entity_type", "to_entity_type")
    @classmethod
    def _validate_type(cls, value: str) -> str:
        if not ENTITY_TYPE_PATTERN.match(value):
            raise ValueError(f"{value!r} is not a valid entity/relationship type.")
        return value

    @field_validator("from_field", "to_field")
    @classmethod
    def _validate_column(cls, value: str) -> str:
        if not SAFE_COLUMN_PATTERN.match(value):
            raise ValueError(f"{value!r} is not a valid column name.")
        return value


class RelationshipRuleSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tenant_code: str
    rule_set_version: str
    rules: tuple[RelationshipRule, ...]
