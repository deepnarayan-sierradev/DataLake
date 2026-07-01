"""
Sage Intacct OAuth 2.0 authentication client.

Implements SageAuthProtocol for Sage Intacct using the OAuth 2.0
client_credentials grant.  Credentials are loaded exclusively from AWS
Secrets Manager via SageCredentialManager — never from constructor arguments,
environment variables, or config files.

Required secret keys (stored at {env}/sources/sage/intacct/credentials):
    base_url       — Intacct REST API base URL
                     (e.g. "https://api.intacct.com/ia/api/v1")
    token_url      — OAuth 2.0 token endpoint
                     (e.g. "https://api.intacct.com/ia/api/v1/auth/token")
    client_id      — Application client ID from the Sage Developer portal
    client_secret  — Application client secret
    company_id     — Target Intacct company ID (scopes the token)

Optional secret keys:
    sender_id      — Web Services License sender ID (required by some endpoints)
    sender_password — Web Services License sender password

Token lifecycle:
    - Tokens fetched on first use and cached in memory.
    - Proactive refresh fires when fewer than _PROACTIVE_REFRESH_SECONDS remain.
    - invalidate_token() forces an immediate refresh on the next call.
    - Token TTL is read from the token endpoint response (expires_in field).

Security (OWASP A07, A09):
    - client_secret, sender_password, and access_token are never logged.
    - All credential values are passed as opaque strings; error messages
      contain only exception type names, not values.
    - TLS verification enforced by SageHttpClient (verify=True always).
"""

from __future__ import annotations

import time
from typing import Final

from connector_runtime.adapters.sage.common.sage_credential_manager import (
    SageCredentialError,
    SageCredentialManager,
)
from connector_runtime.adapters.sage.common.sage_http_client import (
    SageAuthenticationError,
    SageHttpClient,
    SageHttpError,
)
from observability.structured_logger import get_platform_logger

_logger = get_platform_logger(__name__)

_PRODUCT_NAME: Final[str] = "intacct"

# Refresh the token this many seconds before expiry to avoid mid-extraction expiry.
_PROACTIVE_REFRESH_SECONDS: Final[int] = 300

# Fallback TTL when the token endpoint does not return expires_in.
_DEFAULT_TOKEN_TTL_SECONDS: Final[int] = 3_600

# Required keys that must be present in the Intacct credentials secret.
_REQUIRED_CREDENTIAL_KEYS: Final[frozenset[str]] = frozenset(
    {"base_url", "token_url", "client_id", "client_secret", "company_id"}
)


class IntacctCredentialError(SageCredentialError):
    """Raised when Intacct credentials are absent or invalid in Secrets Manager."""


class IntacctAuthError(Exception):
    """Raised when the Intacct OAuth 2.0 token endpoint rejects the request."""


class IntacctAuthClient:
    """
    Manages the Sage Intacct OAuth 2.0 client-credentials token lifecycle.

    Implements SageAuthProtocol (structural typing via Protocol — no inheritance).

    One instance per extraction run.  The access token is cached in-memory and
    refreshed proactively when approaching expiry.

    Usage::

        auth = IntacctAuthClient(
            credential_manager=SageCredentialManager(...),
            http_client=SageHttpClient(),
        )
        token = auth.get_access_token()   # returns or refreshes Bearer token
        base_url = auth.base_url          # resolved after first token fetch
    """

    def __init__(
        self,
        credential_manager: SageCredentialManager,
        http_client: SageHttpClient,
    ) -> None:
        self._credentials = credential_manager
        self._http = http_client

        # Token state — populated lazily on first get_access_token() call.
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0  # UNIX epoch seconds
        self._base_url: str | None = None

    @property
    def base_url(self) -> str:
        """
        Intacct REST API base URL (e.g. "https://api.intacct.com/ia/api/v1").
        Populated after the first successful get_access_token() call.
        """
        if self._base_url is None:
            raise RuntimeError(
                "base_url is not available until get_access_token() succeeds."
            )
        return self._base_url

    def get_access_token(self) -> str:
        """
        Return a valid Intacct OAuth 2.0 Bearer access token.

        Returns the cached token if it is still valid with the proactive refresh
        buffer; otherwise fetches a new token from the Intacct token endpoint.

        Raises:
            IntacctCredentialError: credentials absent or malformed in Secrets Manager.
            IntacctAuthError: the Intacct token endpoint rejected the request.
        """
        if self._access_token is not None and self._is_token_valid():
            return self._access_token
        self._refresh_token()
        if self._access_token is None:
            raise IntacctAuthError(
                "Token refresh completed but access_token was not set. "
                "This is an internal implementation error."
            )
        return self._access_token

    def invalidate_token(self) -> None:
        """
        Force the next get_access_token() call to fetch a fresh token.

        Call this when the Intacct API returns HTTP 401 to recover from
        token expiry without restarting the extraction run.
        """
        self._access_token = None
        self._token_expires_at = 0.0
        _logger.info("sage_intacct_token_invalidated")

    # ── Private ────────────────────────────────────────────────────────────────

    def _is_token_valid(self) -> bool:
        """True when the cached token has more than the proactive refresh window remaining."""
        return (self._token_expires_at - time.time()) > _PROACTIVE_REFRESH_SECONDS

    def _refresh_token(self) -> None:
        """
        Fetch a fresh access token from the Intacct OAuth 2.0 token endpoint.

        Raises:
            IntacctCredentialError: credentials cannot be loaded from Secrets Manager.
            IntacctAuthError: token endpoint returned an error or unexpected body.
        """
        try:
            creds = self._credentials.get_credentials()
        except SageCredentialError as exc:
            raise IntacctCredentialError(str(exc)) from exc

        token_url: str = creds["token_url"]
        client_id: str = creds["client_id"]
        client_secret: str = creds["client_secret"]
        company_id: str = creds["company_id"]
        base_url: str = creds["base_url"]

        # Post the client_credentials grant.  client_secret is form data —
        # never a query parameter (OWASP A07).
        try:
            response_body = self._http.post_form(
                url=token_url,
                headers={"Accept": "application/json"},
                form_data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "company_id": company_id,
                },
            )
        except SageAuthenticationError as exc:
            raise IntacctAuthError(
                "Intacct token endpoint rejected the client credentials: "
                f"{type(exc).__name__}"
            ) from None
        except SageHttpError as exc:
            raise IntacctAuthError(
                f"Intacct token request failed: {type(exc).__name__}"
            ) from None

        access_token = response_body.get("access_token")
        if not access_token:
            raise IntacctAuthError(
                "Intacct token endpoint returned a response with no access_token field."
            )

        expires_in = int(response_body.get("expires_in", _DEFAULT_TOKEN_TTL_SECONDS))
        self._access_token = access_token
        self._token_expires_at = time.time() + expires_in
        self._base_url = base_url.rstrip("/")

        _logger.info(
            "sage_intacct_token_acquired",
            expires_in_seconds=expires_in,
            # Token value intentionally NOT logged (OWASP A09).
        )

    def build_auth_headers(self) -> dict[str, str]:
        """
        Return the Authorization and content negotiation headers for an API call.

        Convenience method used by IntacctMetadataClient and by SageConnector's
        execute_extraction to avoid duplicating the header assembly in two places.
        """
        return {
            "Authorization": f"Bearer {self.get_access_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
