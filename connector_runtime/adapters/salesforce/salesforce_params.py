"""
Pydantic params model for Salesforce connector_params validation (§2.2).

Validates the connector_params dict before any AWS API call is made,
ensuring object_name conforms to the Salesforce API object name format.

Security (OWASP A03):
  - extra="forbid" rejects unknown keys — prevents injection via extra fields.
  - Pattern constraint limits object_name to safe Salesforce API identifiers.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SalesforceConnectorParams(BaseModel):
    """Validated connector_params for the Salesforce adapter."""

    model_config = {"extra": "forbid"}

    object_name: str = Field(
        ...,
        pattern=r"^[A-Za-z][A-Za-z0-9_]{0,79}$",
        description="Salesforce API object name (e.g. 'Account', 'Contact', 'CustomObject__c').",
    )
