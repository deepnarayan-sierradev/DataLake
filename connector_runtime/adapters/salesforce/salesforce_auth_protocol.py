"""
Salesforce authentication Protocol.

Defines the structural type expected by SalesforceMetadataDiscoveryClient and
SalesforceBulkQueryJobController.  Using a Protocol rather than importing
SalesforceAuthClient directly breaks the circular-import chain while retaining
full mypy type coverage — no `type: ignore[assignment]` is needed at call sites.

The Protocol is intentionally minimal: only the two methods consumed by the
downstream clients are declared.  Adding any method here would incorrectly
widen the dependency surface.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SalesforceAuthProtocol(Protocol):
    """
    Structural type for any object that can supply a Salesforce access token
    and the resolved instance URL.

    Implemented by SalesforceAuthClient; also satisfiable by test doubles
    without importing the real client, keeping unit tests lightweight.
    """

    def get_access_token(self) -> str:
        """Return a valid access token, refreshing if necessary."""
        ...

    def invalidate_token(self) -> None:
        """Force the next get_access_token() call to fetch a new token."""
        ...

    @property
    def instance_url(self) -> str:
        """Return the Salesforce instance base URL (e.g. https://org.my.salesforce.com)."""
        ...
