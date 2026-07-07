"""
Pydantic params model for Sage connector_params validation (§2.2).

Security (OWASP A03):
  - extra="forbid" rejects unknown keys.
  - sage_product restricted to known product slugs.
  - object_path restricted to safe API path format (no path traversal).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SageConnectorParams(BaseModel):
    """Validated connector_params for the Sage connector."""

    model_config = {"extra": "forbid"}

    sage_product: str = Field(
        ...,
        pattern=r"^[a-z][a-z0-9\-]{0,31}$",
        description="Sage product identifier (e.g. 'intacct', 'x3', '200').",
    )
    object_path: str = Field(
        ...,
        pattern=r"^[A-Za-z][A-Za-z0-9_\-/]{0,127}$",
        description=(
            "Sage API object path (e.g. 'accounts-receivable/customer', 'BPCUSTOMER'). "
            "No path traversal sequences allowed."
        ),
    )
