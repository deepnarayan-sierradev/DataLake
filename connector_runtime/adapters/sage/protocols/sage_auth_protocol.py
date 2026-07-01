"""
SageAuthProtocol — structural type for any Sage product authentication client.

Implemented concretely by product-specific auth clients (e.g. IntacctAuthClient).
Consumed by SageConnector without importing the concrete class, avoiding circular
imports and keeping the generic connector layer decoupled from product modules.

Security guarantee (OWASP A07):
  - The protocol exposes only a bearer token string — never credentials.
  - Credential secrets remain inside the concrete auth client.
  - Callers receive a token via get_access_token() and nothing more sensitive.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SageAuthProtocol(Protocol):
    """
    Structural type for any object that can supply a Sage REST API bearer token
    and the resolved base URL for the target Sage environment.

    Implementors must:
      - Retrieve credentials exclusively from AWS Secrets Manager.
      - Cache the token in-memory with proactive refresh before expiry.
      - Never log or expose credential values in any method.
      - Raise typed exceptions (SageCredentialError / SageAuthError) on failure
        so the connector's classify_extraction_error() can route correctly.
    """

    @property
    def base_url(self) -> str:
        """
        Return the REST API base URL for this Sage environment.

        Populated after the first successful get_access_token() call.
        Raises RuntimeError if called before the first successful token fetch.
        """
        ...

    def get_access_token(self) -> str:
        """
        Return a valid bearer access token for the Sage REST API.

        Fetches a fresh token on first call and when fewer than the proactive
        refresh window seconds remain before the current token expires.

        The returned string is the raw token value — callers must use it as:
            Authorization: Bearer {token}

        Raises:
            SageCredentialError: credentials absent or invalid in Secrets Manager.
            SageAuthError: the Sage token endpoint rejected the request.
        """
        ...

    def invalidate_token(self) -> None:
        """
        Force the next get_access_token() call to fetch a fresh token.

        Call this when the Sage API returns HTTP 401 to recover from mid-run
        token expiry without restarting the extraction.
        """
        ...
