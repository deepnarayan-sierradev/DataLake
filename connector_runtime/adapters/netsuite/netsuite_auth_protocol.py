"""
NetSuite authentication Protocol.

Defines the structural type expected by NetSuiteMetadataAdapter and
NetSuiteIncrementalQueryPlanner.  Using a Protocol rather than importing
NetSuiteAuthClient directly breaks the circular-import chain while retaining
full mypy type coverage.

The Protocol is intentionally minimal: only the attributes and methods consumed
by downstream clients are declared.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class NetSuiteAuthProtocol(Protocol):
    """
    Structural type for any object that can supply NetSuite TBA OAuth 1.0a
    signed request headers and the resolved account ID.

    Implemented by NetSuiteAuthClient; also satisfiable by test doubles
    without importing the real client, keeping unit tests lightweight.
    """

    @property
    def account_id(self) -> str:
        """Return the NetSuite account ID (e.g. '1234567' or 'TSTDRV1234567')."""
        ...

    def get_auth_headers(self, method: str, url: str) -> dict[str, str]:
        """
        Return an Authorization header dict for the given HTTP request.

        Each call produces a fresh signature — OAuth 1.0a TBA is per-request.

        Args:
            method: HTTP method in upper-case (GET, POST, PATCH, …).
            url: Full request URL including query string if present.

        Returns:
            Dict with a single 'Authorization' key containing the signed
            OAuth 1.0a header value.
        """
        ...
