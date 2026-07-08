"""
Pydantic params model for NetSuite connector_params validation (§2.2).

Security (OWASP A03):
  - extra="forbid" rejects unknown keys.
  - record_type restricted to safe NetSuite API identifiers.
  - page_size bounded to NetSuite's supported range (1-10,000).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class NetSuiteConnectorParams(BaseModel):
    """Validated connector_params for the NetSuite adapter."""

    model_config = {"extra": "forbid"}

    record_type: str = Field(
        ...,
        pattern=r"^[A-Za-z][A-Za-z0-9_]{0,79}$",
        description="NetSuite record type name (e.g. 'customer', 'salesorder').",
    )
    page_size: int = Field(
        default=10_000,
        ge=1,
        le=10_000,
        description="Number of rows per SuiteQL page request (1-10,000; default 10,000).",
    )
