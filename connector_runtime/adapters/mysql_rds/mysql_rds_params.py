"""
Pydantic params model for MySQL RDS connector_params validation (§2.2).

Security (OWASP A03):
  - extra="forbid" rejects unknown keys.
  - Pattern constraint limits table_name to safe SQL identifiers.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MySqlRdsConnectorParams(BaseModel):
    """Validated connector_params for the MySQL RDS adapter."""

    model_config = {"extra": "forbid"}

    table_name: str = Field(
        ...,
        pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$",
        description="MySQL table name to extract (e.g. 'orders', 'customers').",
    )
